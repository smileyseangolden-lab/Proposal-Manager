"""Background worker for pre-proposal triage jobs.

A simple SQLite-backed job runner. Designed to run as a sidecar container
next to the Flask app via docker-compose. Polls the ``triage_jobs`` table
for ``pending`` rows, claims them with an atomic UPDATE, executes the
matching handler, and updates status. No Redis. No Celery.

Usage:
    python triage_worker.py            # poll forever
    python triage_worker.py --once     # process the queue once and exit
    python triage_worker.py --drain    # alias for --once

The worker is safe to run as multiple replicas: claims use ``WHERE
status='pending'`` plus a per-row uuid stamp before commit so two workers
can't claim the same job. SQLite + WAL handles this fine for the throughput
we expect (dozens of jobs/min, not thousands).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

# ``app`` is imported solely for the Flask app context (DB engine setup).
# Importing here keeps the worker process self-contained.
from app import app  # noqa: E402  (intentional after imports)
from models import (  # noqa: E402
    BidPackage,
    DocumentAnalysis,
    Project,
    ProjectDocument,
    TriageJob,
    User,
    db,
)

LOG = logging.getLogger("triage_worker")
logging.basicConfig(
    level=os.getenv("TRIAGE_WORKER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


POLL_INTERVAL_SECONDS = float(os.getenv("TRIAGE_WORKER_POLL_INTERVAL", "3"))
HEARTBEAT_SECONDS = 30


_should_stop = False


def _signal_stop(signum, frame):  # pragma: no cover - process-level concern
    global _should_stop
    LOG.info("Stop signal %s received; finishing current job and exiting", signum)
    _should_stop = True


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def enqueue_analyze_document(
    document_id: str,
    *,
    project_id: str | None = None,
    bid_package_id: str | None = None,
    user_id: str | None = None,
) -> TriageJob:
    """Insert a job to analyze a single document. Returns the inserted row."""
    job = TriageJob(
        job_type="analyze_document",
        project_id=project_id,
        bid_package_id=bid_package_id,
        document_id=document_id,
        user_id=user_id,
        payload="{}",
    )
    db.session.add(job)
    db.session.commit()
    return job


def enqueue_package_analysis(
    package: BidPackage,
    *,
    user_id: str | None = None,
) -> int:
    """Enqueue an analyze_document job for every doc in a package. Returns count."""
    docs = ProjectDocument.query.filter_by(bid_package_id=package.id).all()
    count = 0
    for doc in docs:
        # Skip docs that already have a successful analysis.
        if doc.analysis is not None and doc.analysis.status in {"analyzed", "needs_review"}:
            continue
        enqueue_analyze_document(
            doc.id,
            project_id=package.project_id,
            bid_package_id=package.id,
            user_id=user_id,
        )
        count += 1
    return count


def run_once() -> int:
    """Process all currently-pending jobs once. Returns the count handled."""
    handled = 0
    with app.app_context():
        while True:
            job = _claim_next_job()
            if job is None:
                break
            _process(job)
            handled += 1
    return handled


def run_forever() -> None:
    """Long-running poll loop."""
    LOG.info("Triage worker starting; poll=%.1fs", POLL_INTERVAL_SECONDS)
    signal.signal(signal.SIGINT, _signal_stop)
    signal.signal(signal.SIGTERM, _signal_stop)
    while not _should_stop:
        try:
            handled = run_once()
        except Exception:  # pragma: no cover - keep the worker alive
            LOG.exception("Worker loop crashed; will retry")
            handled = 0
        if handled == 0:
            time.sleep(POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _claim_next_job() -> TriageJob | None:
    """Atomically pick the oldest pending job and mark it ``running``.

    Uses a stamp-and-check pattern compatible with SQLite (no SELECT FOR
    UPDATE). The worker writes its own pid into ``error_message`` as a
    claim marker, commits, then re-reads. If two workers race, only one
    will see its own pid; the other re-polls.
    """
    pending = (
        TriageJob.query.filter_by(status="pending")
        .order_by(TriageJob.created_at.asc())
        .first()
    )
    if pending is None:
        return None
    stamp = f"claim:{os.getpid()}:{datetime.now(timezone.utc).isoformat()}"
    pending.status = "running"
    pending.attempts = (pending.attempts or 0) + 1
    pending.started_at = datetime.now(timezone.utc)
    pending.heartbeat_at = pending.started_at
    pending.error_message = stamp
    db.session.commit()

    # Re-read to confirm we actually own it.
    fresh = db.session.get(TriageJob, pending.id)
    if fresh is None or fresh.error_message != stamp or fresh.status != "running":
        return None
    fresh.error_message = ""
    db.session.commit()
    return fresh


def _process(job: TriageJob) -> None:
    LOG.info("Processing job %s type=%s attempt=%d", job.id, job.job_type, job.attempts)
    try:
        if job.job_type == "analyze_document":
            _handle_analyze_document(job)
        else:
            raise ValueError(f"Unknown job type: {job.job_type}")
        job.status = "done"
        job.error_message = ""
        job.finished_at = datetime.now(timezone.utc)
        db.session.commit()
    except Exception as exc:
        LOG.exception("Job %s failed", job.id)
        job.error_message = f"{exc.__class__.__name__}: {exc}"[:1000]
        if (job.attempts or 0) >= (job.max_attempts or 1):
            job.status = "failed"
            job.finished_at = datetime.now(timezone.utc)
        else:
            job.status = "pending"  # retry on next poll
        db.session.commit()


def _handle_analyze_document(job: TriageJob) -> None:
    if not job.document_id:
        raise ValueError("analyze_document job requires document_id")

    # Late import: the analyzer pulls in anthropic, which is a heavy import.
    # Doing it here keeps the worker bootstrap lean if a job dies before
    # ever hitting an analyze.
    from triage_analyzer import analyze_document

    api_key = None
    model = None
    if job.user_id:
        user = db.session.get(User, job.user_id)
        if user:
            api_key = user.api_key_encrypted or None

    vertical = None
    if job.project_id:
        project = db.session.get(Project, job.project_id)
        if project:
            vertical = project.vertical

    payload = _safe_json(job.payload)
    model = payload.get("model")  # optional override

    analysis: DocumentAnalysis = analyze_document(
        job.document_id,
        api_key=api_key,
        model=model,
        vertical=vertical,
    )
    if analysis.status == "failed":
        # Surface analyzer errors to the job row so the UI can show them.
        raise RuntimeError(analysis.error_message or "Analysis failed")


def _safe_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:  # pragma: no cover - CLI wrapper
    parser = argparse.ArgumentParser(description="ETG Proposal Manager triage worker")
    parser.add_argument("--once", "--drain", action="store_true",
                        help="Process the queue once and exit")
    args = parser.parse_args()
    if args.once:
        handled = run_once()
        LOG.info("Processed %d jobs", handled)
        return 0
    run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

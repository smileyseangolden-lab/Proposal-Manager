"""In-process background job runner backed by the BackgroundJob table.

AI generation and revision take minutes — far longer than a web request
should hold a gunicorn worker or survive a load balancer timeout. Routes
enqueue a job row and redirect to a progress page that polls job status;
daemon worker threads claim queued jobs (atomically, via UPDATE ... WHERE
status='queued') and run them inside an app context.

Runs correctly under multiple gunicorn workers: each process runs its own
small thread pool and the row-claim update ensures a job is executed once.
Set JOBS_INLINE=true (or app TESTING) to execute jobs synchronously.
"""

import json
import logging
import threading
import time
import traceback
from datetime import datetime, timezone

from models import BackgroundJob, db

logger = logging.getLogger(__name__)

_HANDLERS: dict[str, callable] = {}
_WORKERS_STARTED = False
_WORKER_COUNT = 2
_POLL_SECONDS = 1.5
# A job stuck in "running" longer than this is assumed orphaned (its worker
# died / was killed on deploy) and is reaped to "failed" so it stops polling.
_STALE_JOB_SECONDS = 60 * 30

_app = None  # set by init_app


def register(kind: str):
    """Decorator: register a job handler. Handler signature: fn(payload: dict,
    job: BackgroundJob) -> dict (stored as job.result)."""
    def wrap(fn):
        _HANDLERS[kind] = fn
        return fn
    return wrap


def init_app(app):
    global _app
    _app = app


def _inline() -> bool:
    from config.settings import JOBS_INLINE
    return JOBS_INLINE or bool(_app and _app.config.get("TESTING"))


def enqueue(kind: str, payload: dict, user_id: str, org_id: str = None) -> BackgroundJob:
    job = BackgroundJob(
        kind=kind,
        payload=json.dumps(payload),
        user_id=user_id,
        org_id=org_id,
        status="queued",
    )
    db.session.add(job)
    db.session.commit()

    if _inline():
        _run_job(job.id)
    else:
        start_workers()
    return job


def set_progress(job: BackgroundJob, phase: str, message: str = ""):
    job.phase = phase
    job.message = message
    db.session.commit()


def _claim(job_id: str = None) -> str | None:
    """Atomically claim one queued job. Returns the job id or None."""
    q = BackgroundJob.query.filter_by(status="queued")
    if job_id:
        q = q.filter_by(id=job_id)
    job = q.order_by(BackgroundJob.created_at.asc()).first()
    if not job:
        return None
    updated = (
        BackgroundJob.query.filter_by(id=job.id, status="queued")
        .update({"status": "running", "started_at": datetime.now(timezone.utc)})
    )
    db.session.commit()
    return job.id if updated else None


def _run_job(job_id: str):
    job = db.session.get(BackgroundJob, job_id)
    if not job:
        return
    if job.status == "queued":
        claimed = _claim(job_id)
        if not claimed:
            return
        job = db.session.get(BackgroundJob, job_id)

    handler = _HANDLERS.get(job.kind)
    # Attribute any LLM calls this job makes to its org/user so token usage is
    # metered and the monthly AI budget is enforced (same as request-scoped calls).
    try:
        import proposal_agent
        proposal_agent.set_call_attribution(
            org_id=job.org_id, user_id=job.user_id, kind=job.kind, job_id=job.id
        )
    except Exception:
        pass
    try:
        if handler is None:
            raise RuntimeError(f"No handler registered for job kind '{job.kind}'")
        payload = json.loads(job.payload or "{}")
        result = handler(payload, job) or {}
        job = db.session.get(BackgroundJob, job_id)
        job.status = "done"
        job.result = json.dumps(result)
        job.finished_at = datetime.now(timezone.utc)
        db.session.commit()
    except Exception as e:
        logger.error("Job %s (%s) failed:\n%s", job_id, job.kind, traceback.format_exc())
        db.session.rollback()
        job = db.session.get(BackgroundJob, job_id)
        job.status = "failed"
        job.error = str(e)
        job.finished_at = datetime.now(timezone.utc)
        db.session.commit()
    finally:
        try:
            import proposal_agent
            proposal_agent.set_call_attribution()
        except Exception:
            pass


def reap_stale_jobs():
    """Fail jobs stuck in 'running' past the staleness threshold (their worker
    died mid-run, e.g. on a deploy). Without this they poll forever."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_STALE_JOB_SECONDS)
    stale = BackgroundJob.query.filter(
        BackgroundJob.status == "running",
        BackgroundJob.started_at.isnot(None),
        BackgroundJob.started_at < cutoff,
    ).all()
    for job in stale:
        job.status = "failed"
        job.error = "Job timed out or its worker stopped; please retry."
        job.finished_at = datetime.now(timezone.utc)
    if stale:
        db.session.commit()
        logger.warning("Reaped %d stale running job(s)", len(stale))


def _worker_loop():
    while True:
        try:
            with _app.app_context():
                reap_stale_jobs()
                job_id = _claim()
                if job_id:
                    _run_job(job_id)
                    db.session.remove()
                    continue
                db.session.remove()
        except Exception:
            logger.exception("Job worker loop error")
        time.sleep(_POLL_SECONDS)


def start_workers():
    """Start the daemon worker threads (idempotent)."""
    global _WORKERS_STARTED
    if _WORKERS_STARTED or _app is None or _inline():
        return
    _WORKERS_STARTED = True
    for i in range(_WORKER_COUNT):
        t = threading.Thread(target=_worker_loop, name=f"job-worker-{i}", daemon=True)
        t.start()

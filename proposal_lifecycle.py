"""Lifecycle state machine and helpers for the multi-stakeholder review workflow.

States:
    draft                 — AI just produced v1; only the owner has touched it
    in_review             — sent to internal reviewers
    revision_requested    — at least one reviewer requested changes
    internally_approved   — all required reviewers approved the current version
    submitted_to_customer — delivered to the customer
    customer_feedback     — customer replied with requested changes
    customer_approved     — customer accepted → project → won
    customer_declined     — customer declined → project → lost
    won                   — final
    lost                  — final
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from models import (
    Proposal,
    ProposalApproval,
    ProposalReviewer,
    ProposalStatusHistory,
    ProposalVersion,
    Project,
    RevisionRequest,
    db,
)

# Canonical set of states
STATES = (
    "draft",
    "in_review",
    "revision_requested",
    "internally_approved",
    "submitted_to_customer",
    "customer_feedback",
    "customer_approved",
    "customer_declined",
    "won",
    "lost",
)

# Allowed forward transitions (from -> set of tos)
_ALLOWED: dict[str, set[str]] = {
    "draft": {"in_review"},
    "in_review": {"revision_requested", "internally_approved", "draft"},
    "revision_requested": {"in_review", "internally_approved", "draft"},
    "internally_approved": {"submitted_to_customer", "in_review"},
    "submitted_to_customer": {"customer_feedback", "customer_approved", "customer_declined"},
    "customer_feedback": {"in_review", "submitted_to_customer", "customer_approved", "customer_declined"},
    "customer_approved": {"won"},
    "customer_declined": {"lost"},
    "won": set(),
    "lost": set(),
}

# Friendly labels for templates
LABELS = {
    "draft": "Draft",
    "in_review": "In Review",
    "revision_requested": "Revision Requested",
    "internally_approved": "Internally Approved",
    "submitted_to_customer": "Submitted to Customer",
    "customer_feedback": "Customer Feedback",
    "customer_approved": "Customer Approved",
    "customer_declined": "Customer Declined",
    "won": "Won",
    "lost": "Lost",
}


class LifecycleError(Exception):
    """Raised when a lifecycle transition is invalid."""


def can_transition(from_status: str, to_status: str) -> bool:
    if from_status == to_status:
        return True  # no-op is always allowed
    return to_status in _ALLOWED.get(from_status, set())


def transition(proposal: Proposal, to_status: str, actor_id: Optional[str],
               note: str = "") -> ProposalStatusHistory:
    """Perform a state transition with validation and history logging."""
    if to_status not in STATES:
        raise LifecycleError(f"Unknown state: {to_status}")
    from_status = proposal.review_status or "draft"
    if not can_transition(from_status, to_status):
        raise LifecycleError(
            f"Cannot transition from '{from_status}' to '{to_status}'."
        )

    entry = ProposalStatusHistory(
        proposal_id=proposal.id,
        from_status=from_status,
        to_status=to_status,
        actor_id=actor_id,
        note=note,
    )
    proposal.review_status = to_status
    db.session.add(entry)

    # Sync project status one-directionally
    project = db.session.get(Project, proposal.project_id)
    if project:
        _sync_project_status(project, to_status)

    return entry


def _sync_project_status(project: Project, proposal_status: str):
    """Map proposal lifecycle onto existing Project.status field.

    Only updates the project status when the proposal lifecycle clearly
    implies a project state. Never reverses the project back to active once
    it's won/lost.
    """
    mapping = {
        "submitted_to_customer": "submitted",
        "customer_feedback": "submitted",
        "customer_approved": "won",
        "won": "won",
        "customer_declined": "lost",
        "lost": "lost",
    }
    target = mapping.get(proposal_status)
    if not target:
        return
    # Don't move backwards from won/lost
    if project.status in ("won", "lost") and target not in ("won", "lost"):
        return
    project.status = target
    if target == "submitted" and not project.submitted_at:
        project.submitted_at = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Approval rollup
# ---------------------------------------------------------------------------


def latest_version(proposal_id: str) -> Optional[ProposalVersion]:
    return (
        ProposalVersion.query.filter_by(proposal_id=proposal_id)
        .order_by(ProposalVersion.version_number.desc())
        .first()
    )


def approval_state(proposal: Proposal) -> dict:
    """Compute approval rollup for the CURRENT latest version of the proposal.

    Returns dict with keys:
        required_count, approved_count, requested_changes_count,
        pending_count, pending_reviewers (list of ProposalReviewer),
        approved_reviewers, all_approved (bool).
    """
    version = latest_version(proposal.id)
    reviewers: Iterable[ProposalReviewer] = ProposalReviewer.query.filter_by(
        proposal_id=proposal.id
    ).all()

    required = [r for r in reviewers if r.is_required]

    if version is None:
        return {
            "required_count": len(required),
            "approved_count": 0,
            "requested_changes_count": 0,
            "pending_count": len(required),
            "pending_reviewers": list(required),
            "approved_reviewers": [],
            "requested_changes_reviewers": [],
            "all_approved": False,
            "version_id": None,
            "version_number": None,
        }

    approved: list[ProposalReviewer] = []
    changes: list[ProposalReviewer] = []
    pending: list[ProposalReviewer] = []
    for r in required:
        decision = (
            ProposalApproval.query.filter_by(
                proposal_id=proposal.id,
                version_id=version.id,
                user_id=r.user_id,
            )
            .order_by(ProposalApproval.decided_at.desc())
            .first()
        )
        if decision is None:
            pending.append(r)
        elif decision.decision == "approved":
            approved.append(r)
        else:
            changes.append(r)

    return {
        "required_count": len(required),
        "approved_count": len(approved),
        "requested_changes_count": len(changes),
        "pending_count": len(pending),
        "pending_reviewers": pending,
        "approved_reviewers": approved,
        "requested_changes_reviewers": changes,
        "all_approved": len(required) > 0 and len(approved) == len(required),
        "version_id": version.id,
        "version_number": version.version_number,
    }


def auto_advance_after_decision(proposal: Proposal, actor_id: Optional[str]) -> Optional[str]:
    """After a reviewer records a decision, see if the proposal should auto-advance.

    Returns the new status if advanced, else None.
    """
    state = approval_state(proposal)
    current = proposal.review_status

    if state["requested_changes_count"] > 0 and current == "in_review":
        transition(proposal, "revision_requested", actor_id,
                   note="Auto: a reviewer requested changes.")
        return "revision_requested"

    if state["all_approved"] and current in ("in_review", "revision_requested"):
        transition(proposal, "internally_approved", actor_id,
                   note="Auto: all required reviewers approved.")
        return "internally_approved"

    return None


# ---------------------------------------------------------------------------
# Pending revision request helpers
# ---------------------------------------------------------------------------


def pending_requests(proposal_id: str) -> list[RevisionRequest]:
    return (
        RevisionRequest.query.filter_by(proposal_id=proposal_id, status="pending")
        .order_by(RevisionRequest.created_at.asc())
        .all()
    )

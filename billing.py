"""Plans, limits, usage metering, and Stripe integration.

Phase 1 ships the plan/limit/metering scaffolding used by generation gating.
Phase 5 layers Stripe checkout/portal/webhooks on top. Stripe is optional:
when STRIPE_SECRET_KEY is unset the app runs in "free plan only" mode and never
calls out to Stripe.
"""

import os
from datetime import datetime, timezone

# Plan catalog. limits: projects (-1 = unlimited), seats, generations/month.
PLANS = {
    "free": {
        "name": "Free",
        "price_id": "",
        "monthly_price": 0,
        "limits": {"seats": 2, "generations_per_month": 5, "projects": 10},
        "features": ["Up to 2 seats", "5 AI proposals / month", "Core workflow"],
    },
    "pro": {
        "name": "Pro",
        "price_id": os.getenv("STRIPE_PRICE_PRO", ""),
        "monthly_price": 49,
        "limits": {"seats": 10, "generations_per_month": 100, "projects": -1},
        "features": ["Up to 10 seats", "100 AI proposals / month",
                     "Customer portal & PDF", "Structured pricing"],
    },
    "business": {
        "name": "Business",
        "price_id": os.getenv("STRIPE_PRICE_BUSINESS", ""),
        "monthly_price": 149,
        "limits": {"seats": -1, "generations_per_month": -1, "projects": -1},
        "features": ["Unlimited seats", "Unlimited AI proposals",
                     "Integrations", "Priority support"],
    },
}

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")


def stripe_enabled() -> bool:
    return bool(STRIPE_SECRET_KEY)


def plan_for(org) -> dict:
    return PLANS.get((org.plan if org else "free") or "free", PLANS["free"])


def limits_for(org) -> dict:
    return plan_for(org)["limits"]


def _month_key(dt=None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def generations_this_month(org_id) -> int:
    """Count successful generations for the org in the current calendar month."""
    from models import BackgroundJob
    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        BackgroundJob.query.filter(
            BackgroundJob.org_id == org_id,
            BackgroundJob.kind == "generate_proposal",
            BackgroundJob.status == "done",
            BackgroundJob.finished_at >= start,
        ).count()
    )


def seats_used(org_id) -> int:
    from models import User
    return User.query.filter_by(org_id=org_id).count()


def check_generation(org_id) -> tuple[bool, str]:
    """Return (allowed, message). Enforces the monthly generation limit."""
    from models import Organization, db
    org = db.session.get(Organization, org_id) if org_id else None
    limit = limits_for(org)["generations_per_month"]
    if limit < 0:
        return True, ""
    used = generations_this_month(org_id)
    if used >= limit:
        plan_name = plan_for(org)["name"]
        return False, (
            f"You've reached your {plan_name} plan limit of {limit} AI proposals "
            f"this month. Upgrade your plan to generate more."
        )
    return True, ""


def record_generation(org_id):
    """Metering hook. Generations are counted from completed job rows, so this
    is currently a no-op placeholder kept for future per-token accounting."""
    return None


def can_add_seat(org_id, pending_invites=0) -> tuple[bool, str]:
    """Seat check that also counts outstanding (unaccepted) invitations so an
    org can't over-provision by inviting past its limit."""
    from models import Organization, db
    org = db.session.get(Organization, org_id) if org_id else None
    limit = limits_for(org)["seats"]
    if limit < 0:
        return True, ""
    if seats_used(org_id) + pending_invites >= limit:
        return False, (
            f"Your plan allows {limit} seats (members + pending invites). "
            f"Upgrade to add more teammates."
        )
    return True, ""


def projects_used(org_id) -> int:
    from models import Project
    return Project.query.filter_by(org_id=org_id).count()


def can_add_project(org_id) -> tuple[bool, str]:
    from models import Organization, db
    org = db.session.get(Organization, org_id) if org_id else None
    limit = limits_for(org)["projects"]
    if limit < 0:
        return True, ""
    if projects_used(org_id) >= limit:
        plan_name = plan_for(org)["name"]
        return False, (
            f"Your {plan_name} plan allows {limit} projects. "
            f"Upgrade your plan to create more."
        )
    return True, ""

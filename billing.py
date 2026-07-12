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
        "limits": {"seats": 2, "generations_per_month": 5, "projects": 10,
                   "ai_tokens_per_month": 500_000},
        "features": ["Up to 2 seats", "5 AI proposals / month", "Core workflow"],
    },
    "pro": {
        "name": "Pro",
        "price_id": os.getenv("STRIPE_PRICE_PRO", ""),
        "monthly_price": 49,
        "limits": {"seats": 10, "generations_per_month": 100, "projects": -1,
                   "ai_tokens_per_month": 10_000_000},
        "features": ["Up to 10 seats", "100 AI proposals / month",
                     "Customer portal & PDF", "Structured pricing"],
    },
    "business": {
        "name": "Business",
        "price_id": os.getenv("STRIPE_PRICE_BUSINESS", ""),
        "monthly_price": 149,
        "limits": {"seats": -1, "generations_per_month": -1, "projects": -1,
                   "ai_tokens_per_month": -1},
        "features": ["Unlimited seats", "Unlimited AI proposals",
                     "Integrations", "Priority support"],
    },
}

# Approximate Anthropic list prices in USD per 1M tokens: (input, output).
# Used only for internal cost estimates / budgeting, matched by substring.
MODEL_PRICING = {
    "opus": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku": (0.80, 4.0),
}
_DEFAULT_PRICING = (15.0, 75.0)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimated USD cost of one call from token counts."""
    m = (model or "").lower()
    rate = _DEFAULT_PRICING
    for key, r in MODEL_PRICING.items():
        if key in m:
            rate = r
            break
    return round((input_tokens or 0) / 1_000_000 * rate[0]
                 + (output_tokens or 0) / 1_000_000 * rate[1], 6)

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")


def stripe_enabled() -> bool:
    return bool(STRIPE_SECRET_KEY)


# Subscription states in which paid limits are revoked until payment recovers.
_DELINQUENT_STATUSES = ("past_due", "unpaid", "canceled", "incomplete_expired")


def plan_for(org) -> dict:
    """The org's nominal plan (for display)."""
    return PLANS.get((org.plan if org else "free") or "free", PLANS["free"])


def effective_plan(org) -> dict:
    """The plan whose LIMITS currently apply. A paid org that is delinquent
    (past_due / unpaid) is soft-locked to free-tier limits until its payment
    recovers; plan_for() still reports the nominal plan for the billing page."""
    if org and (getattr(org, "billing_status", "") or "") in _DELINQUENT_STATUSES:
        return PLANS["free"]
    return plan_for(org)


def limits_for(org) -> dict:
    return effective_plan(org)["limits"]


def _month_key(dt=None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def generations_this_month(org_id) -> int:
    """Count generations for the org in the current calendar month.

    Counts queued + running + done (everything except failed), keyed on
    created_at, so in-flight jobs count against the limit too. This closes the
    burst/parallel bypass where only completed jobs were counted, letting a user
    enqueue many generations before any finished."""
    from models import BackgroundJob
    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        BackgroundJob.query.filter(
            BackgroundJob.org_id == org_id,
            BackgroundJob.kind == "generate_proposal",
            BackgroundJob.status.in_(("queued", "running", "done")),
            BackgroundJob.created_at >= start,
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
    """Metering hook. Generations are counted from completed job rows; per-token
    accounting is handled separately by record_llm_usage()."""
    return None


def record_llm_usage(org_id, user_id, kind, model, input_tokens, output_tokens, job_id=None):
    """Persist one LLM call's token usage + estimated cost.

    Best-effort: writes via an autonomous transaction (its own connection) so it
    never flushes or interferes with the caller's ORM session, and never raises
    into the request/generation path."""
    try:
        import logging
        import uuid

        from models import LlmUsage, db

        cost = estimate_cost(model, input_tokens, output_tokens)
        stmt = LlmUsage.__table__.insert().values(
            id=uuid.uuid4().hex,
            org_id=org_id,
            user_id=user_id,
            job_id=job_id,
            kind=(kind or "")[:50],
            provider="anthropic",
            model=(model or "")[:100],
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            est_cost_usd=cost,
            created_at=datetime.now(timezone.utc),
        )
        with db.engine.begin() as conn:
            conn.execute(stmt)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("record_llm_usage failed")


def tokens_this_month(org_id) -> int:
    """Total input+output tokens billed to an org in the current calendar month."""
    from sqlalchemy import func

    from models import LlmUsage, db
    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = (
        db.session.query(
            func.coalesce(func.sum(LlmUsage.input_tokens + LlmUsage.output_tokens), 0)
        )
        .filter(LlmUsage.org_id == org_id, LlmUsage.created_at >= start)
        .scalar()
    )
    return int(total or 0)


def platform_mrr() -> float:
    """Monthly recurring revenue across all tenants, computed from the plan
    catalog: sum of monthly_price over orgs on a paid plan in good standing.
    (Stripe is the source-of-truth upgrade path; this is the pragmatic v1.)"""
    from models import Organization
    total = 0.0
    orgs = Organization.query.filter(
        Organization.plan.isnot(None), Organization.plan != "free"
    ).all()
    for org in orgs:
        if (org.billing_status or "") in _DELINQUENT_STATUSES:
            continue
        total += PLANS.get(org.plan, PLANS["free"]).get("monthly_price", 0)
    return round(total, 2)


def plan_distribution() -> dict:
    """Count of orgs per plan key, e.g. {'free': 3, 'pro': 1}."""
    from sqlalchemy import func

    from models import Organization, db
    rows = db.session.query(Organization.plan, func.count()).group_by(Organization.plan).all()
    return {(p or "free"): n for p, n in rows}


def check_ai_budget(org_id) -> tuple[bool, str]:
    """Return (allowed, message). Enforces the monthly AI token budget so a
    single org can't run up unbounded pay-per-use LLM cost."""
    from models import Organization, db
    org = db.session.get(Organization, org_id) if org_id else None
    limit = limits_for(org).get("ai_tokens_per_month", -1)
    if limit is None or limit < 0:
        return True, ""
    if tokens_this_month(org_id) >= limit:
        plan_name = plan_for(org)["name"]
        return False, (
            f"You've reached your {plan_name} plan's monthly AI usage limit. "
            f"Upgrade your plan to continue generating."
        )
    return True, ""


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

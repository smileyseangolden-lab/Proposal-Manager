"""Platform-Admin: a cross-tenant "owner" dashboard for the SaaS operator.

This is a NEW capability distinct from the tenant admin (`User.is_admin`, which
is scoped to one organization). Access is gated to platform owners only — the
`User.platform_owner` column or the PLATFORM_OWNER_EMAILS allowlist — and every
query here is deliberately UN-scoped (spans all tenants). Read-only.
"""

from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_user, logout_user
from sqlalchemy import func

import billing
from config.settings import PLATFORM_OWNER_EMAILS
from models import (
    ActivityLog, BackgroundJob, ClarificationItem, CompanyStandard, LlmUsage,
    Organization, Project, Proposal, ProposalShare, User, UserVerticalTemplate,
    db,
)

bp = Blueprint("platform_admin", __name__, url_prefix="/platform-admin")


def is_platform_owner(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "platform_owner", False):
        return True
    return (user.email or "").strip().lower() in PLATFORM_OWNER_EMAILS


def require_platform_owner(fn):
    """Gate a view to platform owners only. Returns 404 (not 403) for everyone
    else — including tenant admins — so the dashboard's existence is never
    advertised to a probing user."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_platform_owner(current_user):
            abort(404)
        return fn(*args, **kwargs)
    return wrapper


def _since(days: int):
    return datetime.now(timezone.utc) - timedelta(days=days)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if is_platform_owner(current_user):
        return redirect(url_for("platform_admin.overview"))
    if request.method == "POST":
        ident = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter(
            db.or_(User.email.ilike(ident), User.username == ident)
        ).first()
        # Same generic failure whether the credentials are wrong OR the account
        # simply isn't a platform owner — don't reveal who the owners are.
        if user and user.check_password(password) and is_platform_owner(user):
            login_user(user)
            return redirect(url_for("platform_admin.overview"))
        flash("Invalid credentials.", "error")
    return render_template("platform/login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    logout_user()
    return redirect(url_for("platform_admin.login"))


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

@bp.route("/")
@require_platform_owner
def overview():
    paid_statuses = ("active", "trialing")
    stats = {
        "organizations": Organization.query.count(),
        "users": User.query.count(),
        "proposals": Proposal.query.count(),
        "projects": Project.query.count(),
        "ai_reviews": BackgroundJob.query.filter_by(
            kind="generate_proposal", status="done").count(),
        "paid_subscribers": Organization.query.filter(
            Organization.plan != "free",
            Organization.billing_status.in_(paid_statuses)).count(),
    }
    mrr = billing.platform_mrr()
    dist = billing.plan_distribution()

    ops = {
        "envelopes": ProposalShare.query.count(),
        "signed": ProposalShare.query.filter_by(decision="accepted").count(),
        "pending_sig": ProposalShare.query.filter(
            ProposalShare.decision == "", ProposalShare.revoked_at.is_(None)).count(),
        "clauses": CompanyStandard.query.count(),
        "templates": UserVerticalTemplate.query.count(),
        "obligations": ClarificationItem.query.count(),
    }

    since30 = _since(30)
    llm = db.session.query(
        func.count(LlmUsage.id),
        func.coalesce(func.sum(LlmUsage.input_tokens + LlmUsage.output_tokens), 0),
        func.coalesce(func.sum(LlmUsage.est_cost_usd), 0.0),
    ).filter(LlmUsage.created_at >= since30).first()

    return render_template(
        "platform/overview.html",
        active_tab="overview",
        stats=stats, mrr=mrr, arr=round(mrr * 12, 2),
        plans=billing.PLANS, dist=dist, ops=ops,
        llm_requests=int(llm[0] or 0), llm_tokens=int(llm[1] or 0),
        llm_cost=round(float(llm[2] or 0), 2),
    )


@bp.route("/accounts")
@require_platform_owner
def accounts():
    # Aggregate per-org counts in bulk (no N+1) then join in Python.
    def _counts(model, col):
        rows = db.session.query(col, func.count()).group_by(col).all()
        return {k: v for k, v in rows}

    seats = _counts(User, User.org_id)
    projects = _counts(Project, Project.org_id)
    gens = dict(
        db.session.query(BackgroundJob.org_id, func.count())
        .filter(BackgroundJob.kind == "generate_proposal", BackgroundJob.status == "done")
        .group_by(BackgroundJob.org_id).all()
    )
    ai_cost = dict(
        db.session.query(LlmUsage.org_id, func.coalesce(func.sum(LlmUsage.est_cost_usd), 0.0))
        .group_by(LlmUsage.org_id).all()
    )

    rows = []
    for org in Organization.query.order_by(Organization.created_at.desc()).all():
        plan = billing.PLANS.get(org.plan or "free", billing.PLANS["free"])
        rows.append({
            "org": org,
            "plan_name": plan["name"],
            "mrr": 0 if (org.plan or "free") == "free"
                   or (org.billing_status or "") in billing._DELINQUENT_STATUSES
                   else plan.get("monthly_price", 0),
            "seats": seats.get(org.id, 0),
            "projects": projects.get(org.id, 0),
            "generations": gens.get(org.id, 0),
            "ai_cost": round(float(ai_cost.get(org.id, 0) or 0), 2),
        })
    return render_template("platform/accounts.html", active_tab="accounts", rows=rows)


@bp.route("/revenue")
@require_platform_owner
def revenue():
    mrr = billing.platform_mrr()
    dist = billing.plan_distribution()
    breakdown = []
    for key, plan in billing.PLANS.items():
        count = Organization.query.filter(
            Organization.plan == key,
            Organization.billing_status.in_(("active", "trialing")),
        ).count() if key != "free" else dist.get("free", 0)
        breakdown.append({
            "key": key, "name": plan["name"], "price": plan.get("monthly_price", 0),
            "count": count, "mrr": 0 if key == "free" else plan.get("monthly_price", 0) * count,
        })
    return render_template(
        "platform/revenue.html", active_tab="revenue",
        mrr=mrr, arr=round(mrr * 12, 2), breakdown=breakdown,
    )


@bp.route("/ai-costs")
@require_platform_owner
def ai_costs():
    since30 = _since(30)
    totals = db.session.query(
        func.count(LlmUsage.id),
        func.coalesce(func.sum(LlmUsage.input_tokens), 0),
        func.coalesce(func.sum(LlmUsage.output_tokens), 0),
        func.coalesce(func.sum(LlmUsage.est_cost_usd), 0.0),
    ).filter(LlmUsage.created_at >= since30).first()

    by_model = db.session.query(
        LlmUsage.model, func.count(LlmUsage.id),
        func.coalesce(func.sum(LlmUsage.est_cost_usd), 0.0),
    ).filter(LlmUsage.created_at >= since30).group_by(LlmUsage.model).all()

    by_org_raw = db.session.query(
        LlmUsage.org_id, func.coalesce(func.sum(LlmUsage.est_cost_usd), 0.0),
        func.coalesce(func.sum(LlmUsage.input_tokens + LlmUsage.output_tokens), 0),
    ).filter(LlmUsage.created_at >= since30).group_by(LlmUsage.org_id).order_by(
        func.sum(LlmUsage.est_cost_usd).desc()).limit(20).all()
    org_names = {o.id: o.name for o in Organization.query.all()}
    by_org = [{"name": org_names.get(oid, "—"), "cost": round(float(c or 0), 2),
               "tokens": int(t or 0)} for oid, c, t in by_org_raw]

    has_data = int(totals[0] or 0) > 0
    return render_template(
        "platform/ai_costs.html", active_tab="ai-costs", has_data=has_data,
        requests=int(totals[0] or 0), input_tokens=int(totals[1] or 0),
        output_tokens=int(totals[2] or 0), cost=round(float(totals[3] or 0), 2),
        by_model=[{"model": m or "—", "count": n, "cost": round(float(c or 0), 2)} for m, n, c in by_model],
        by_org=by_org,
    )


@bp.route("/health")
@require_platform_owner
def health():
    counts = dict(
        db.session.query(BackgroundJob.status, func.count())
        .group_by(BackgroundJob.status).all()
    )
    total = sum(counts.values()) or 1
    recent_failed = (
        BackgroundJob.query.filter_by(status="failed")
        .order_by(BackgroundJob.finished_at.desc()).limit(15).all()
    )
    recent_activity = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(20).all()
    return render_template(
        "platform/health.html", active_tab="health",
        counts=counts,
        failure_rate=round(counts.get("failed", 0) / total * 100, 1),
        recent_failed=recent_failed, recent_activity=recent_activity,
    )

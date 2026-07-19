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
import platform_config
from config.settings import PLATFORM_OWNER_EMAILS
from models import (
    ActivityLog, BackgroundJob, ChatbotMessage, ClarificationItem, CompanyStandard,
    LlmUsage, Organization, PlatformAuditLog, PlatformSetting, Project, Proposal,
    ProposalShare, User, UserVerticalTemplate, db,
)

bp = Blueprint("platform_admin", __name__, url_prefix="/platform-admin")


def _audit(action: str, detail: str = "", target: str = ""):
    """Record a platform-owner action for the Audit tab (best-effort)."""
    try:
        db.session.add(PlatformAuditLog(
            actor_user_id=getattr(current_user, "id", None),
            actor_email=getattr(current_user, "email", "") or "",
            action=action, detail=detail[:2000], target=target[:200],
            ip=(request.headers.get("X-Forwarded-For", request.remote_addr) or "")[:64],
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


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

# Brute-force protection for the operator login — the highest-value credential
# in the system. In-process per-IP window plus the same DB-backed per-account
# lockout the tenant login uses (survives multiple gunicorn workers).
_PA_ATTEMPTS: dict[str, list[float]] = {}
_PA_MAX_ATTEMPTS = 8
_PA_WINDOW_SECONDS = 300
_PA_LOCK_THRESHOLD = 10
_PA_LOCK_SECONDS = 15 * 60


def _pa_rate_limited(key: str) -> bool:
    import time
    now = time.time()
    attempts = [t for t in _PA_ATTEMPTS.get(key, []) if now - t < _PA_WINDOW_SECONDS]
    _PA_ATTEMPTS[key] = attempts
    return len(attempts) >= _PA_MAX_ATTEMPTS


@bp.route("/login", methods=["GET", "POST"])
def login():
    if is_platform_owner(current_user):
        return redirect(url_for("platform_admin.overview"))
    if request.method == "POST":
        import time
        ident = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        rl_key = f"{request.remote_addr}:{ident.lower()}"
        if _pa_rate_limited(rl_key):
            flash("Too many attempts. Please wait a few minutes and try again.", "error")
            return render_template("platform/login.html")

        user = User.query.filter(
            db.or_(User.email.ilike(ident), User.username == ident)
        ).first()

        now = datetime.now(timezone.utc)
        if user and user.lockout_until:
            lock = user.lockout_until if user.lockout_until.tzinfo else user.lockout_until.replace(tzinfo=timezone.utc)
            if lock > now:
                flash("Invalid credentials.", "error")
                return render_template("platform/login.html")

        # Same generic failure whether the credentials are wrong OR the account
        # simply isn't a platform owner — don't reveal who the owners are.
        if user and user.check_password(password) and is_platform_owner(user):
            if user.failed_login_count or user.lockout_until:
                user.failed_login_count = 0
                user.lockout_until = None
                db.session.commit()
            login_user(user)
            return redirect(url_for("platform_admin.overview"))

        _PA_ATTEMPTS.setdefault(rl_key, []).append(time.time())
        if user:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= _PA_LOCK_THRESHOLD:
                user.lockout_until = now + timedelta(seconds=_PA_LOCK_SECONDS)
                user.failed_login_count = 0
            db.session.commit()
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


def _monthly_series(model, date_col, months=6):
    """Count rows per calendar month for the last `months` months (Python-side
    bucketing so it works identically on SQLite and Postgres)."""
    now = datetime.now(timezone.utc)
    buckets, labels = {}, []
    y, m = now.year, now.month
    keys = []
    for _ in range(months):
        key = f"{y:04d}-{m:02d}"
        keys.append(key); labels.append(key); buckets[key] = 0
        m -= 1
        if m == 0:
            m = 12; y -= 1
    keys.reverse(); labels.reverse()
    start = datetime(y, m if m else 1, 1, tzinfo=timezone.utc)
    for (dt,) in db.session.query(date_col).filter(date_col >= start).all():
        if not dt:
            continue
        key = dt.strftime("%Y-%m")
        if key in buckets:
            buckets[key] += 1
    return [{"label": k, "count": buckets[k]} for k in keys]


@bp.route("/growth")
@require_platform_owner
def growth():
    orgs = _monthly_series(Organization, Organization.created_at)
    users = _monthly_series(User, User.created_at)
    gens = _monthly_series(BackgroundJob, BackgroundJob.created_at)
    maxv = max([1] + [r["count"] for r in orgs + users + gens])
    return render_template("platform/growth.html", active_tab="growth",
                           orgs=orgs, users=users, gens=gens, maxv=maxv)


@bp.route("/chatbot")
@require_platform_owner
def chatbot():
    total = ChatbotMessage.query.count()
    by_answer = dict(
        db.session.query(ChatbotMessage.answered_by, func.count())
        .group_by(ChatbotMessage.answered_by).all()
    )
    recent = (ChatbotMessage.query.order_by(ChatbotMessage.created_at.desc())
              .limit(100).all())
    org_names = {o.id: o.name for o in Organization.query.all()}
    rows = [{"msg": r, "org": org_names.get(r.org_id, "—")} for r in recent]
    return render_template("platform/chatbot.html", active_tab="chatbot",
                           total=total, ai=by_answer.get("ai", 0),
                           fallback=by_answer.get("fallback", 0), rows=rows)


@bp.route("/requests")
@require_platform_owner
def requests_tab():
    # Surfaces chatbot questions that fell through to the keyword fallback — i.e.
    # things users asked that the assistant couldn't answer well — as a proxy for
    # feature/support requests worth reviewing.
    unmet = (ChatbotMessage.query.filter_by(answered_by="fallback")
             .order_by(ChatbotMessage.created_at.desc()).limit(100).all())
    return render_template("platform/requests.html", active_tab="requests", rows=unmet)


@bp.route("/audit")
@require_platform_owner
def audit():
    logs = (PlatformAuditLog.query.order_by(PlatformAuditLog.created_at.desc())
            .limit(200).all())
    return render_template("platform/audit.html", active_tab="audit", logs=logs)


@bp.route("/api")
@require_platform_owner
def api_tab():
    # Read-only summary of the platform's API/LLM configuration.
    active_model = platform_config.get("llm_model", "claude-opus-4-8")
    model_usage = dict(
        db.session.query(User.llm_model, func.count()).group_by(User.llm_model).all()
    )
    return render_template("platform/api.html", active_tab="api",
                           active_model=active_model,
                           anthropic_set=bool(platform_config.get("anthropic_api_key")),
                           model_usage=model_usage)


# ---------------------------------------------------------------------------
# Controls — the only tab with write actions. Every action is audited.
# ---------------------------------------------------------------------------

_CONTROL_GROUPS = [
    ("llm", "LLM / AI", ["llm_model", "anthropic_api_key"]),
    ("payment", "Payments (Stripe)",
     ["stripe_secret_key", "stripe_publishable_key", "stripe_webhook_secret",
      "stripe_price_pro", "stripe_price_business"]),
    ("email", "Email (SMTP)",
     ["smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_use_tls",
      "mail_from", "mail_from_name"]),
]


@bp.route("/controls")
@require_platform_owner
def controls():
    groups = []
    for gid, title, keys in _CONTROL_GROUPS:
        fields = []
        for k in keys:
            spec = platform_config.SETTINGS.get(k, (None, False, k, gid))
            fields.append({
                "key": k, "label": spec[2], "secret": spec[1],
                "value": "" if spec[1] else platform_config.get(k),
                "is_set": bool(platform_config.get(k)),
            })
        groups.append({"id": gid, "title": title, "fields": fields})
    orgs = Organization.query.order_by(Organization.name).all()
    owners = User.query.filter_by(platform_owner=True).all()
    return render_template("platform/controls.html", active_tab="controls",
                           groups=groups, orgs=orgs, plans=billing.PLANS, owners=owners)


@bp.route("/controls/settings", methods=["POST"])
@require_platform_owner
def controls_settings():
    group = request.form.get("group", "")
    keys = next((ks for gid, _t, ks in _CONTROL_GROUPS if gid == group), [])
    changed = []
    for k in keys:
        if k not in request.form:
            continue
        val = request.form.get(k, "").strip()
        # For secrets, an empty submit means "leave unchanged"; the sentinel
        # "__clear__" clears it. Non-secrets are set to whatever was submitted.
        if platform_config.is_secret(k):
            if val == "":
                continue
            if val == "__clear__":
                val = ""
            platform_config.set_value(k, val, updated_by=current_user.id)
            changed.append(k)
        else:
            platform_config.set_value(k, val, updated_by=current_user.id)
            changed.append(k)
    if changed:
        _audit("update_settings", f"{group}: {', '.join(changed)}", target=group)
        flash(f"Saved {group} settings.", "success")
    else:
        flash("No changes.", "success")
    return redirect(url_for("platform_admin.controls"))


@bp.route("/controls/owner", methods=["POST"])
@require_platform_owner
def controls_owner():
    email = request.form.get("email", "").strip().lower()
    action = request.form.get("action", "grant")
    user = User.query.filter(User.email.ilike(email)).first() if email else None
    if not user:
        flash("No user found with that email.", "error")
        return redirect(url_for("platform_admin.controls"))
    user.platform_owner = (action == "grant")
    db.session.commit()
    _audit("owner_" + action, f"{email}", target=email)
    flash(f"Platform owner {'granted to' if action == 'grant' else 'revoked from'} {email}.", "success")
    return redirect(url_for("platform_admin.controls"))


@bp.route("/controls/plan", methods=["POST"])
@require_platform_owner
def controls_plan():
    org_id = request.form.get("org_id", "")
    plan = request.form.get("plan", "")
    org = db.session.get(Organization, org_id) if org_id else None
    if not org or plan not in billing.PLANS:
        flash("Invalid organization or plan.", "error")
        return redirect(url_for("platform_admin.controls"))
    old = org.plan
    org.plan = plan
    org.billing_status = "active" if plan != "free" else ""
    db.session.commit()
    _audit("plan_override", f"{org.name}: {old} -> {plan}", target=org.id)
    flash(f"{org.name} plan set to {billing.PLANS[plan]['name']}.", "success")
    return redirect(url_for("platform_admin.controls"))

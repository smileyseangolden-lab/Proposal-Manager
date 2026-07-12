"""Flask web application for the Proposal Manager Agent.

Full-featured intranet application with:
- User authentication (signup / login)
- Per-user settings (LLM, API key, company, font)
- Project-based workflow with multi-file upload
- Industry vertical selection (manual or auto-detect)
- Interactive Q&A during proposal generation
- Rate/price sheet upload (Excel)
- User dashboard with stats
- Admin panel with company-wide tracking
"""

import difflib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import markdown as md
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from PIL import Image
from werkzeug.utils import secure_filename

from config.settings import (
    ALLOWED_EXTENSIONS,
    APP_COMPANY,
    APP_FOOTER,
    APP_NAME,
    APP_SHORT_NAME,
    DATABASE_URL,
    FLASK_DEBUG,
    FLASK_SECRET_KEY,
    GENERATED_DIR,
    INSECURE_SECRETS,
    IS_PRODUCTION,
    MAX_UPLOAD_SIZE_MB,
    SELF_HOSTED,
    TRUST_PROXY_HOPS,
    UPLOADS_DIR,
    VERTICALS,
)
import billing
import crypto_util
import htmlsafe
import jobs
import proposal_agent
import storage
from document_parser import parse_document
from models import (
    ActivityLog,
    BackgroundJob,
    ClarificationItem,
    CompanyStandard,
    DocumentTag,
    EquipmentItem,
    EstimateLineItem,
    Notification,
    Organization,
    OrgInvitation,
    Project,
    ProjectDocument,
    ProjectScope,
    Proposal,
    ProposalApproval,
    ProposalComment,
    ProposalCorrection,
    ProposalEstimate,
    ProposalQuestion,
    ProposalReviewer,
    ProposalRevisionBatch,
    ProposalShare,
    ProposalStatusHistory,
    ProposalVersion,
    ProcessedWebhookEvent,
    ReviewComment,
    ReviewCycle,
    RevisionRequest,
    RevisionTemplate,
    ScopeItem,
    ShareView,
    StaffRole,
    TravelExpenseRate,
    User,
    UserRateSheet,
    UserToken,
    UserVerticalTemplate,
    VerticalClarificationTemplate,
    db,
)
from proposal_agent import (
    analyze_addendum_impact,
    draft_estimate,
    draft_scope_of_work,
    extract_rates_from_sheet,
    extract_standards,
    friendly_api_error,
    generate_proposal,
    parse_customer_email,
    preflight_check_proposal,
    regenerate_section,
    revise_proposal,
)
from proposal_export import (
    markdown_to_docx,
    markdown_to_pdf,
    markdown_to_redline_docx,
    markdown_to_rfi_docx,
)
from proposal_lifecycle import (
    LABELS as LIFECYCLE_LABELS,
    STATES as LIFECYCLE_STATES,
    LifecycleError,
    approval_state,
    auto_advance_after_decision,
    latest_version,
    pending_requests,
    transition as lifecycle_transition,
)
from rate_sheet_parser import parse_rate_sheet

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _verify_production_secrets():
    """Fail closed in production (APP_ENV=production) when security-critical
    secrets are unset or left at a known public default.

    Prevents two audited blockers:
      - a forgeable session-signing key (FLASK_SECRET_KEY) → auth bypass, and
      - tenant API keys encrypted under a guessable key (APP_ENCRYPTION_KEY),
        which must be a DISTINCT random value, never derived from the session key.
    Dev/test (APP_ENV unset) keep working with the built-in fallbacks.
    """
    if not IS_PRODUCTION:
        return
    problems = []
    if (FLASK_SECRET_KEY or "").strip() in INSECURE_SECRETS:
        problems.append("FLASK_SECRET_KEY is unset or a known default")
    enc = os.getenv("APP_ENCRYPTION_KEY", "").strip()
    if enc in INSECURE_SECRETS:
        problems.append(
            "APP_ENCRYPTION_KEY is unset or weak (set a distinct random key; "
            "it must not fall back to FLASK_SECRET_KEY)"
        )
    if problems:
        raise RuntimeError(
            "Refusing to start in production with insecure secrets: "
            + "; ".join(problems)
            + '. Generate strong values, e.g. '
            + '`python -c "import secrets; print(secrets.token_urlsafe(48))"`.'
        )


_verify_production_secrets()

# Structured logging to stdout (captured by the platform's log aggregation).
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# Whether to surface reset/verification links in the UI when email is
# unconfigured. NEVER true in production — otherwise /forgot-password would hand
# a valid reset token to any unauthenticated requester (account takeover).
_EXPOSE_DEV_LINKS = FLASK_DEBUG and not IS_PRODUCTION

app = Flask(__name__, template_folder="web_templates", static_folder="static")
app.secret_key = FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE_MB * 1024 * 1024
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
if DATABASE_URL.startswith("postgresql"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}

# Secure session cookies. SESSION_COOKIE_SECURE defaults on unless explicitly
# disabled (set to false for plain-HTTP intranet deployments).
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 14  # 14 days

# Behind a reverse proxy, trust its forwarding headers so request.remote_addr is
# the real client IP (used for login rate limiting), not the proxy's.
if TRUST_PROXY_HOPS > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=TRUST_PROXY_HOPS, x_proto=TRUST_PROXY_HOPS, x_host=TRUST_PROXY_HOPS
    )

# Ensure data directory exists
(Path(__file__).resolve().parent / "data").mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
GENERATED_DIR.mkdir(parents=True, exist_ok=True)

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message_category = "error"

RATE_SHEET_EXTENSIONS = {"xlsx", "xls"}
TEMPLATE_EXTENSIONS = {"pdf", "docx", "doc"}
INGEST_RATE_EXTENSIONS = {"xlsx", "xls", "csv", "pdf", "docx", "doc"}
INGEST_STANDARDS_EXTENSIONS = {"pdf", "docx", "doc", "txt", "md"}
LOGO_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}
LOGO_MAX_DIMENSION = 600  # px — resize so the longest side is at most this
LOGO_PNG_OPTIMIZE = True
# Reject images whose decoded pixel count exceeds this — a few-MB "bomb" can
# otherwise decode to hundreds of MB and exhaust memory. Also lower Pillow's
# global guard as defense in depth.
LOGO_MAX_PIXELS = 40_000_000  # 40 MP — far larger than any real logo
Image.MAX_IMAGE_PIXELS = LOGO_MAX_PIXELS


@app.context_processor
def inject_branding():
    return dict(
        app_name=APP_NAME,
        app_short_name=APP_SHORT_NAME,
        app_company=APP_COMPANY,
        app_footer=APP_FOOTER,
    )


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, user_id)


@app.route("/healthz")
def healthz():
    """Readiness probe for load balancers: verifies the DB is reachable."""
    from sqlalchemy import text as _sql_text
    try:
        db.session.execute(_sql_text("SELECT 1"))
        return {"status": "ok"}, 200
    except Exception:
        app.logger.exception("healthz DB check failed")
        return {"status": "error"}, 503


@app.route("/livez")
def livez():
    """Liveness probe: the process is up (no dependencies checked)."""
    return {"status": "ok"}, 200


# ---------------------------------------------------------------------------
# CSRF protection (session double-submit token)
# ---------------------------------------------------------------------------

# Endpoints exempt from CSRF: external webhooks (signed separately) and any
# read-only JSON polled by same-origin fetch (which sends the header anyway).
_CSRF_EXEMPT_ENDPOINTS = {"billing_webhook"}


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_csrf():
    return dict(csrf_token=_csrf_token)


@app.before_request
def _csrf_protect():
    if app.testing:
        return
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    if request.endpoint in _CSRF_EXEMPT_ENDPOINTS:
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRFToken", "")
    expected = session.get("_csrf_token", "")
    if not expected or not hmac.compare_digest(str(sent), str(expected)):
        abort(400, description="Invalid or missing CSRF token. Please reload and try again.")


@app.before_request
def _tag_ai_usage():
    """Attribute any LLM calls made while serving this request to the current
    org/user, so token usage is metered and the monthly AI budget is enforced.
    Background jobs set their own attribution in the job runner."""
    try:
        if getattr(current_user, "is_authenticated", False):
            proposal_agent.set_call_attribution(
                org_id=getattr(current_user, "org_id", None),
                user_id=current_user.id,
                kind=(request.endpoint or ""),
            )
        else:
            proposal_agent.set_call_attribution()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Login rate limiting (in-process sliding window per IP+username)
# ---------------------------------------------------------------------------

_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_LOGIN_MAX_ATTEMPTS = 8
_LOGIN_WINDOW_SECONDS = 300

# Cross-worker, per-account lockout (DB-backed): after this many consecutive
# failures the account is locked for this long.
_ACCOUNT_LOCK_THRESHOLD = 10
_ACCOUNT_LOCK_SECONDS = 15 * 60


def _aware(dt):
    """Treat a naive datetime (as SQLite returns) as UTC for safe comparison."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _login_rate_limited(key: str) -> bool:
    now = time.time()
    attempts = [t for t in _LOGIN_ATTEMPTS.get(key, []) if now - t < _LOGIN_WINDOW_SECONDS]
    _LOGIN_ATTEMPTS[key] = attempts
    return len(attempts) >= _LOGIN_MAX_ATTEMPTS


def _record_login_attempt(key: str):
    _LOGIN_ATTEMPTS.setdefault(key, []).append(time.time())


with app.app_context():
    # Cross-worker schema bootstrap lock. Under gunicorn, every worker imports
    # this module and runs migrations. A file lock serializes the bootstrap so
    # only one worker per host touches the schema at a time; ensure_schema()
    # itself also tolerates already-exists races (relevant for Postgres where
    # workers may run on different hosts).
    import fcntl as _fcntl
    from migrations import ensure_schema
    _data_dir = Path(__file__).resolve().parent / "data"
    _data_dir.mkdir(exist_ok=True)
    _lock_path = _data_dir / ".schema.lock"
    with open(_lock_path, "w") as _lock_fh:
        _fcntl.flock(_lock_fh, _fcntl.LOCK_EX)
        try:
            ensure_schema()
        finally:
            _fcntl.flock(_lock_fh, _fcntl.LOCK_UN)

# Background job runner (AI generation/revision run out-of-request)
jobs.init_app(app)

# AI cost metering + budget enforcement. Every LLM call flows through the metered
# client in proposal_agent, which records token usage here and blocks calls once
# an org exceeds its monthly AI budget. See security audit (LLM spend control).
proposal_agent.set_usage_sink(billing.record_llm_usage)
proposal_agent.set_budget_checker(billing.check_ai_budget)

# Reap any jobs orphaned by a previous process (e.g. a deploy mid-generation)
# so they stop polling forever. Workers themselves start lazily on first enqueue
# and re-run the reaper each poll.
with app.app_context():
    try:
        jobs.reap_stale_jobs()
    except Exception:
        app.logger.warning("Startup job reap failed", exc_info=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _allowed_file(filename: str, extensions: set = None) -> bool:
    exts = extensions or ALLOWED_EXTENSIONS
    return "." in filename and filename.rsplit(".", 1)[1].lower() in exts


def _log_activity(action: str, detail: str = "", project_id: str = None):
    _log_activity_for(current_user, action, detail, project_id)


def _log_activity_for(user, action: str, detail: str = "", project_id: str = None):
    """Request-context-free variant used by background jobs."""
    log = ActivityLog(
        user_id=user.id,
        action=action,
        detail=detail,
        project_id=project_id,
    )
    db.session.add(log)
    db.session.commit()


def _notify(user_id: str, category: str, title: str, message: str = "", link: str = ""):
    """Create an in-app notification for a user."""
    n = Notification(user_id=user_id, category=category, title=title, message=message, link=link)
    db.session.add(n)
    db.session.commit()


def _notify_role(role: str, category: str, title: str, message: str = "", link: str = "", exclude_user_id: str = None):
    """Send a notification to all users with a given role in the current org."""
    _notify_role_org(current_user.org_id, role, category, title, message, link, exclude_user_id)


def _notify_role_org(org_id, role, category, title, message="", link="", exclude_user_id=None):
    """Request-context-free variant used by background jobs."""
    users = User.query.filter_by(role=role, org_id=org_id).all()
    for u in users:
        if exclude_user_id and u.id == exclude_user_id:
            continue
        db.session.add(Notification(user_id=u.id, category=category, title=title, message=message, link=link))
    db.session.commit()


def _notify_via_integrations(org_id, text, event="", payload=None):
    """Fan a message out to an org's configured Slack / outbound webhook."""
    if not org_id:
        return
    org = db.session.get(Organization, org_id)
    if not org:
        return
    import integrations
    if org.slack_webhook_url:
        integrations.notify_slack(org.slack_webhook_url, text)
    if org.outbound_webhook_url and event:
        integrations.notify_webhook(org.outbound_webhook_url, event, payload or {})


# ---------------------------------------------------------------------------
# Organization (tenant) helpers
# ---------------------------------------------------------------------------

def _org_id():
    """The current user's organization id."""
    return current_user.org_id


def _org_users_query():
    """All members of the current user's organization."""
    return User.query.filter_by(org_id=current_user.org_id)


def _org_proposal_users():
    """Org members who can be assigned proposals (proposal + admin roles)."""
    return (
        _org_users_query()
        .filter(User.role.in_(["proposal", "admin"]))
        .order_by(User.display_name)
        .all()
    )


def _same_org(user_or_org_id) -> bool:
    org_id = getattr(user_or_org_id, "org_id", user_or_org_id)
    return bool(org_id) and org_id == current_user.org_id


# ---------------------------------------------------------------------------
# API key encryption (Phase 2) — thin wrappers over crypto_util
# ---------------------------------------------------------------------------

def encrypt_api_key(plaintext: str) -> str:
    import crypto_util
    return crypto_util.encrypt(plaintext)


def decrypt_api_key(stored: str) -> str:
    import crypto_util
    return crypto_util.decrypt(stored)


# ---------------------------------------------------------------------------
# Billing gates (Phase 5) — thin wrappers so routes stay readable
# ---------------------------------------------------------------------------

def billing_check_generation(org_id):
    return billing.check_generation(org_id)


def billing_check_ai_budget(org_id):
    return billing.check_ai_budget(org_id)


def billing_record_generation(org_id):
    return billing.record_generation(org_id)


def billing_check_seat(org_id, pending_invites=0):
    return billing.can_add_seat(org_id, pending_invites)


def billing_check_project(org_id):
    return billing.can_add_project(org_id)


@app.context_processor
def inject_notifications():
    if hasattr(current_user, "id") and current_user.is_authenticated:
        unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
        return dict(unread_notifications=unread)
    return dict(unread_notifications=0)


def _setup_progress(user) -> dict:
    """Compute workspace-setup wizard progress for a user.

    Returns dict with 'steps' (list of {key,title,desc,done,url_endpoint,anchor}),
    'done' count, 'total' count, and 'complete' bool.
    """
    steps = [
        {
            "key": "company",
            "title": "Add your company profile",
            "desc": "Company name used to brand generated proposals.",
            "endpoint": "settings",
            "anchor": "",
            "done": bool((user.company_name or "").strip()),
        },
        {
            "key": "logo",
            "title": "Upload your company logo",
            "desc": "Placed on proposal cover pages and headers.",
            "endpoint": "posture",
            "anchor": "#branding",
            "done": bool(user.company_logo_path),
        },
        {
            "key": "template",
            "title": "Upload a proposal template",
            "desc": "The AI follows your structure when drafting.",
            "endpoint": "posture",
            "anchor": "#templates",
            "done": UserVerticalTemplate.query.filter_by(org_id=user.org_id).count() > 0,
        },
        {
            "key": "standards",
            "title": "Add company standards & terms",
            "desc": "Mission, certifications, T&Cs auto-injected into proposals.",
            "endpoint": "posture",
            "anchor": "#standards",
            "done": CompanyStandard.query.filter_by(org_id=user.org_id).count() > 0,
        },
        {
            "key": "staff",
            "title": "Define staff rates",
            "desc": "Hourly sell rates for labor cost estimates.",
            "endpoint": "posture",
            "anchor": "#staff-rates",
            "done": StaffRole.query.filter_by(org_id=user.org_id).count() > 0
                    or UserRateSheet.query.filter_by(org_id=user.org_id).count() > 0,
        },
        {
            "key": "equipment",
            "title": "Add products & equipment pricing",
            "desc": "Price list used for Bill of Materials estimates.",
            "endpoint": "posture",
            "anchor": "#equipment",
            "done": EquipmentItem.query.filter_by(org_id=user.org_id).count() > 0,
        },
        {
            "key": "travel",
            "title": "Set travel & expense rates",
            "desc": "Per diem, mileage, airfare used in travel estimates.",
            "endpoint": "posture",
            "anchor": "#travel",
            "done": TravelExpenseRate.query.filter_by(org_id=user.org_id).count() > 0,
        },
        {
            "key": "project",
            "title": "Create your first project",
            "desc": "Upload an RFP/RFQ to start a proposal session.",
            "endpoint": "new_project",
            "anchor": "",
            "done": Project.query.filter_by(user_id=user.id).count() > 0,
        },
        {
            "key": "proposal",
            "title": "Generate your first proposal",
            "desc": "Let the AI draft against your posture.",
            "endpoint": "proposals_list",
            "anchor": "",
            "done": Proposal.query.join(Project).filter(Project.user_id == user.id).count() > 0,
        },
    ]
    done = sum(1 for s in steps if s["done"])
    return {
        "steps": steps,
        "done": done,
        "total": len(steps),
        "complete": done == len(steps),
        "percent": round(done / len(steps) * 100),
    }


@app.context_processor
def inject_nav_context():
    """Sidebar counts + setup wizard progress for the app shell."""
    if not (hasattr(current_user, "id") and current_user.is_authenticated):
        return dict(setup_progress=None, nav_active_projects=0)
    active_count = Project.query.filter(
        db.or_(
            Project.user_id == current_user.id,
            Project.assigned_to == current_user.id,
        ),
        Project.status == "active",
    ).count()
    return dict(
        setup_progress=_setup_progress(current_user),
        nav_active_projects=active_count,
    )


def _can_access_project(project) -> bool:
    """Check if current user can access a project (owner, assignee, or an
    admin of the *same organization*)."""
    if not project:
        return False
    if project.user_id == current_user.id or project.assigned_to == current_user.id:
        return True
    return current_user.is_admin and _same_org(project.org_id or _owner_org_id(project))


def _owner_org_id(project) -> str:
    """Org id for a project, falling back to its owner's org for legacy rows."""
    if project.org_id:
        return project.org_id
    owner = db.session.get(User, project.user_id)
    return owner.org_id if owner else None


def _save_upload(file, subdir: str = "") -> tuple[str, str, int]:
    """Save an uploaded file. Returns (safe_name, full_path, file_size)."""
    safe = secure_filename(file.filename)
    unique = f"{uuid.uuid4().hex[:8]}_{safe}"
    dest_dir = UPLOADS_DIR / subdir if subdir else UPLOADS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / unique
    file.save(str(dest))
    size = dest.stat().st_size
    storage.sync_up(dest)
    return safe, str(dest), size


def _send_stored_file(path, **kwargs):
    """send_file wrapper that fetches the file from object storage first if the
    local copy is missing (ephemeral disk / multi-instance safety)."""
    storage.ensure_local(path)
    return send_file(str(path), **kwargs)


def _logo_docx_kwargs(user, company_name_override: str = "") -> dict:
    """Build the logo-related kwargs passed into markdown_to_docx so they
    respect the user's branding preferences."""
    if not getattr(user, "company_logo_path", None):
        return {"font_name": user.font_preference or "Calibri"}
    if not getattr(user, "company_logo_use_in_proposals", False):
        return {"font_name": user.font_preference or "Calibri"}
    logo_path = user.company_logo_path
    if not Path(logo_path).exists():
        return {"font_name": user.font_preference or "Calibri"}
    return {
        "logo_path": logo_path,
        "logo_placement": user.company_logo_placement or "top_left",
        "logo_on_cover": bool(getattr(user, "company_logo_show_on_cover", False)),
        "company_name": company_name_override or (user.company_name or ""),
        "font_name": user.font_preference or "Calibri",
    }


def _process_and_save_logo(file, user_id: str) -> tuple[str, str, int]:
    """Load an uploaded image, auto-orient, resize to a reasonable size,
    and save as an optimized PNG. Transparency is preserved where possible.

    Returns (original_filename, saved_full_path, saved_file_size).
    Raises ValueError on an unreadable/invalid image.
    """
    original_name = secure_filename(file.filename or "logo")
    dest_dir = UPLOADS_DIR / f"logos/{user_id}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    unique = f"logo_{uuid.uuid4().hex[:8]}.png"
    dest = dest_dir / unique

    try:
        img = Image.open(file.stream)
        w, h = img.size  # lazy — no pixel decode yet
    except Exception as e:
        raise ValueError(f"Could not read image: {e}")
    if w * h > LOGO_MAX_PIXELS:
        raise ValueError("Image is too large to process.")
    try:
        img.load()
    except Exception as e:
        raise ValueError(f"Could not read image: {e}")

    # Respect EXIF orientation
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    # Convert palette / CMYK to RGBA so transparency is preserved and colors render correctly
    if img.mode not in ("RGB", "RGBA", "LA", "L"):
        img = img.convert("RGBA")
    elif img.mode == "L":
        img = img.convert("RGBA")

    # Resize so the longest dimension is at most LOGO_MAX_DIMENSION
    max_dim = max(img.width, img.height)
    if max_dim > LOGO_MAX_DIMENSION:
        ratio = LOGO_MAX_DIMENSION / float(max_dim)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    img.save(str(dest), format="PNG", optimize=LOGO_PNG_OPTIMIZE)
    size = dest.stat().st_size
    return original_name, str(dest), size


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

def _valid_invitation(token: str) -> OrgInvitation | None:
    if not token:
        return None
    inv = OrgInvitation.query.filter_by(token=token).first()
    if not inv or inv.accepted_at or inv.revoked_at:
        return None
    if inv.expires_at:
        expires = inv.expires_at.replace(tzinfo=None) if inv.expires_at.tzinfo else inv.expires_at
        if expires < datetime.utcnow():
            return None
    return inv


@app.route("/invite/<token>")
def accept_invite(token):
    """Landing page for an org invitation — signup form scoped to the org."""
    inv = _valid_invitation(token)
    if not inv:
        flash("This invitation link is invalid, expired, or already used.", "error")
        return redirect(url_for("signup"))
    org = db.session.get(Organization, inv.org_id)
    return render_template("signup.html", invitation=inv, invite_org=org)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html", invitation=None, invite_org=None)

    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    display_name = request.form.get("display_name", "").strip()
    company_name = request.form.get("company_name", "").strip()
    invite_token = request.form.get("invite_token", "").strip()

    invitation = _valid_invitation(invite_token) if invite_token else None
    if invite_token and not invitation:
        flash("This invitation link is invalid, expired, or already used.", "error")
        return redirect(url_for("signup"))

    if not username or not email or not password:
        flash("All fields are required.", "error")
        return redirect(url_for("signup"))

    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("signup"))

    if User.query.filter_by(username=username).first():
        flash("Username already taken.", "error")
        return redirect(url_for("signup"))

    user = User(
        username=username,
        email=email,
        display_name=display_name or username,
        company_name=company_name,
    )
    user.set_password(password)

    if invitation:
        # Join the inviting organization with the invited role.
        # Bind the account to the INVITED email, not whatever was typed in the
        # form — otherwise a forwarded invite link could be claimed under an
        # arbitrary address (and marked verified below).
        org = db.session.get(Organization, invitation.org_id)
        user.email = invitation.email
        user.org_id = org.id
        user.company_name = org.name
        user.role = invitation.role if invitation.role in ("admin", "sales", "proposal") else "proposal"
        user.is_admin = user.role == "admin"
        invitation.accepted_at = datetime.now(timezone.utc)
        db.session.add(user)
        db.session.flush()
        invitation.accepted_user_id = user.id
    else:
        # Fresh signup creates a new workspace; the creator is its admin
        from datetime import timedelta
        org = Organization(
            name=company_name or f"{display_name or username}'s Workspace",
            plan="free",
            trial_ends_at=datetime.utcnow() + timedelta(days=14),
        )
        db.session.add(org)
        db.session.flush()
        user.org_id = org.id
        user.is_admin = True
        user.role = "admin"
        db.session.add(user)

    db.session.commit()
    session.permanent = True
    login_user(user)
    _log_activity("signup", f"User {username} created account")

    # Kick off email verification (best-effort; never blocks signup)
    if invitation:
        # Invited users arrived via a link sent to their address — trust it
        user.email_verified = True
        db.session.commit()
    else:
        try:
            _send_verification_email(user)
        except Exception:
            pass

    flash("Account created successfully." if not invitation
          else f"Welcome to {org.name}!", "success")
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    rl_key = f"{request.remote_addr}:{username.lower()}"
    if _login_rate_limited(rl_key):
        flash("Too many login attempts. Please wait a few minutes and try again.", "error")
        return redirect(url_for("login"))

    user = User.query.filter_by(username=username).first()

    # Cross-worker, per-account lockout (survives multiple gunicorn workers and
    # catches password spraying against one account regardless of source IP).
    now = datetime.now(timezone.utc)
    if user and user.lockout_until and _aware(user.lockout_until) > now:
        flash("This account is temporarily locked after too many failed attempts. "
              "Please wait a few minutes and try again.", "error")
        return redirect(url_for("login"))

    if not user or not user.check_password(password):
        _record_login_attempt(rl_key)
        if user:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= _ACCOUNT_LOCK_THRESHOLD:
                user.lockout_until = now + timedelta(seconds=_ACCOUNT_LOCK_SECONDS)
                user.failed_login_count = 0
            db.session.commit()
        flash("Invalid username or password.", "error")
        return redirect(url_for("login"))

    _LOGIN_ATTEMPTS.pop(rl_key, None)
    if user.failed_login_count or user.lockout_until:
        user.failed_login_count = 0
        user.lockout_until = None
        db.session.commit()
    session.permanent = True  # apply PERMANENT_SESSION_LIFETIME idle expiry
    login_user(user)
    _log_activity("login")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Email verification & password reset
# ---------------------------------------------------------------------------

def _issue_token(user, purpose: str, hours: int = 24) -> UserToken:
    from datetime import timedelta
    tok = UserToken(
        user_id=user.id,
        purpose=purpose,
        expires_at=datetime.utcnow() + timedelta(hours=hours),
    )
    db.session.add(tok)
    db.session.commit()
    return tok


def _consume_token(token_str: str, purpose: str):
    tok = UserToken.query.filter_by(token=token_str, purpose=purpose).first()
    if not tok or tok.used_at:
        return None
    if tok.expires_at:
        exp = tok.expires_at.replace(tzinfo=None) if tok.expires_at.tzinfo else tok.expires_at
        if exp < datetime.utcnow():
            return None
    return tok


def _send_verification_email(user):
    tok = _issue_token(user, "verify", hours=72)
    link = url_for("verify_email", token=tok.token, _external=True)
    import mailer
    sent = mailer.send_email(
        to=user.email,
        subject=f"Verify your email for {APP_NAME}",
        body=(f"Welcome to {APP_NAME}!\n\nPlease verify your email address:\n{link}\n\n"
              f"This link expires in 72 hours."),
    )
    return link, sent


@app.route("/verify-email/<token>")
def verify_email(token):
    tok = _consume_token(token, "verify")
    if not tok:
        flash("This verification link is invalid or has expired.", "error")
        return redirect(url_for("login"))
    user = db.session.get(User, tok.user_id)
    user.email_verified = True
    tok.used_at = datetime.now(timezone.utc)
    db.session.commit()
    flash("Email verified. Thanks!", "success")
    return redirect(url_for("dashboard") if current_user.is_authenticated else url_for("login"))


@app.route("/resend-verification", methods=["POST"])
@login_required
def resend_verification():
    if current_user.email_verified:
        flash("Your email is already verified.", "success")
        return redirect(request.referrer or url_for("dashboard"))
    link, sent = _send_verification_email(current_user)
    if sent:
        flash("Verification email sent.", "success")
    else:
        flash(f"Verify your email here: {link}", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")
    email = request.form.get("email", "").strip().lower()
    # Always show the same message to avoid leaking which emails exist
    generic = "If an account exists for that email, a reset link has been sent."
    user = User.query.filter(User.email.ilike(email)).first() if email else None
    if user:
        tok = _issue_token(user, "reset", hours=2)
        link = url_for("reset_password", token=tok.token, _external=True)
        import mailer
        sent = mailer.send_email(
            to=user.email,
            subject=f"Reset your {APP_NAME} password",
            body=(f"A password reset was requested for your account.\n\n"
                  f"Reset your password:\n{link}\n\nThis link expires in 2 hours. "
                  f"If you didn't request this, you can ignore this email."),
        )
        if not sent and _EXPOSE_DEV_LINKS:
            # Local-dev only: surface the link so the flow is testable without
            # SMTP. Gated so production never discloses a reset token to an
            # unauthenticated requester.
            flash(f"[dev] Password reset link: {link}", "success")
    flash(generic, "success")
    return redirect(url_for("login"))


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    tok = _consume_token(token, "reset")
    if not tok:
        flash("This password reset link is invalid or has expired.", "error")
        return redirect(url_for("forgot_password"))
    if request.method == "GET":
        return render_template("reset_password.html", token=token)
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("reset_password", token=token))
    if password != confirm:
        flash("Passwords do not match.", "error")
        return redirect(url_for("reset_password", token=token))
    user = db.session.get(User, tok.user_id)
    user.set_password(password)
    tok.used_at = datetime.now(timezone.utc)
    db.session.commit()
    flash("Password updated. You can now sign in.", "success")
    return redirect(url_for("login"))


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    _log_activity("logout")
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Phase flow (chevron stepper)
# ---------------------------------------------------------------------------

def compute_phases(project, proposal, doc_count=None, scope_status=None) -> list[dict]:
    """Map a project + latest proposal onto the 8-phase proposal flow.

    Phases: Upload → Scope of Work → Draft Proposal → Internal Review →
    Send to Customer → Negotiate → Awarded/Not Awarded → Storage.

    Returns a list of {key, label, state} where state is one of
    'complete', 'current', 'pending', 'lost'.
    """
    if doc_count is None:
        doc_count = ProjectDocument.query.filter_by(project_id=project.id).count()

    rs = (proposal.review_status or "draft") if proposal else None
    won = project.status == "won" or rs in ("won", "customer_approved")
    lost = project.status == "lost" or rs in ("lost", "customer_declined")
    archived = project.status == "archived"

    upload_done = doc_count > 0
    scope_done = scope_status == "approved" or proposal is not None
    draft_done = proposal is not None
    review_done = rs in ("internally_approved", "submitted_to_customer", "customer_feedback",
                         "customer_approved", "customer_declined", "won", "lost")
    review_current = rs in ("draft", "in_review", "revision_requested")
    send_done = rs in ("submitted_to_customer", "customer_feedback",
                       "customer_approved", "customer_declined", "won", "lost")
    send_current = rs == "internally_approved"
    neg_done = rs in ("customer_approved", "customer_declined", "won", "lost")
    neg_current = rs in ("submitted_to_customer", "customer_feedback")

    def state(done, current):
        if done:
            return "complete"
        if current:
            return "current"
        return "pending"

    if won:
        award_label, award_state = "Awarded", "complete"
    elif lost:
        award_label, award_state = "Not Awarded", "lost"
    else:
        award_label, award_state = "Award", "pending"

    return [
        {"key": "upload", "label": "Upload",
         "state": state(upload_done, not upload_done)},
        {"key": "scope", "label": "Scope of Work",
         "state": state(scope_done, upload_done and not scope_done)},
        {"key": "draft", "label": "Draft Proposal",
         "state": state(draft_done, scope_done and not draft_done)},
        {"key": "review", "label": "Internal Review",
         "state": state(review_done, bool(proposal) and review_current)},
        {"key": "send", "label": "Send to Customer",
         "state": state(send_done, send_current)},
        {"key": "negotiate", "label": "Negotiate",
         "state": state(neg_done, neg_current)},
        {"key": "award", "label": award_label, "state": award_state},
        {"key": "storage", "label": "Storage",
         "state": "complete" if archived else ("current" if (won or lost) else "pending")},
    ]


def _inline_redline_markdown(old_md: str, new_md: str) -> str:
    """Produce a word-level inline diff of two markdown documents, wrapping
    additions in <ins> and removals in <del> so the rendered HTML shows a
    readable redline. Newlines are kept outside the tags so markdown block
    structure survives."""
    def tokenize(text):
        return re.findall(r"[^\s]+|\n|[ \t]+", text)

    def wrap(tokens, tag):
        out = []
        buf = []
        for t in tokens:
            if t == "\n":
                if buf:
                    out.append(f"<{tag}>{''.join(buf)}</{tag}>")
                    buf = []
                out.append("\n")
            else:
                buf.append(t)
        if buf:
            out.append(f"<{tag}>{''.join(buf)}</{tag}>")
        return "".join(out)

    old_tokens = tokenize(old_md)
    new_tokens = tokenize(new_md)
    sm = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    parts = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            parts.append("".join(new_tokens[j1:j2]))
        elif op == "insert":
            parts.append(wrap(new_tokens[j1:j2], "ins"))
        elif op == "delete":
            parts.append(wrap(old_tokens[i1:i2], "del"))
        else:  # replace
            parts.append(wrap(old_tokens[i1:i2], "del"))
            parts.append(wrap(new_tokens[j1:j2], "ins"))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _project_focus_entry(p, now_naive):
    """Build a 'pick up where you left off' entry for an active project.

    Determines the single next action based on the latest proposal's
    lifecycle state (or the pre-generation state of the project).
    """
    latest_prop = (
        Proposal.query.filter_by(project_id=p.id)
        .order_by(Proposal.generated_at.desc())
        .first()
    )
    doc_count = ProjectDocument.query.filter_by(project_id=p.id).count()
    pending_questions = ProposalQuestion.query.filter_by(
        project_id=p.id, status="pending"
    ).count()

    action_label = "Open"
    action_url = url_for("project_upload", project_id=p.id)
    chip = ("Active", "chip-sea")
    sub = ""

    if pending_questions > 0:
        action_label = "Answer questions"
        action_url = url_for("project_questions", project_id=p.id)
        chip = ("Your move", "chip-gold")
        sub = f"{pending_questions} clarification question(s) awaiting answers"
    elif latest_prop is None:
        scope = ProjectScope.query.filter_by(project_id=p.id).first()
        if doc_count == 0:
            action_label = "Upload documents"
            chip = ("Needs documents", "chip-neutral")
            sub = "No RFP/RFQ uploaded yet"
        elif scope is None:
            action_label = "Draft scope of work"
            chip = ("Your move", "chip-gold")
            sub = f"{doc_count} document(s) ready · scope not drafted yet"
        elif scope.status == "draft":
            action_label = "Approve scope"
            action_url = url_for("project_scope", project_id=p.id)
            chip = ("Your move", "chip-gold")
            sub = "AI scope drafted · awaiting your approval"
        else:
            action_label = "Generate proposal"
            chip = ("Your move", "chip-gold")
            sub = "Scope approved · awaiting AI draft"
    else:
        rs = latest_prop.review_status or "draft"
        pending_req = RevisionRequest.query.filter_by(
            proposal_id=latest_prop.id, status="pending"
        ).count()
        if rs == "draft":
            action_label = "Send for review"
            action_url = url_for("view_proposal", proposal_id=latest_prop.id)
            chip = ("Your move", "chip-gold")
            sub = "AI draft ready · not yet in internal review"
        elif rs in ("in_review", "revision_requested"):
            state = approval_state(latest_prop)
            if pending_req > 0:
                action_label = "Apply feedback"
                action_url = url_for("apply_feedback", proposal_id=latest_prop.id)
                chip = ("Your move", "chip-gold")
                sub = f"{pending_req} pending revision request(s)"
            else:
                action_label = "Open"
                action_url = url_for("view_proposal", proposal_id=latest_prop.id)
                chip = ("In review", "chip-sea")
                sub = f"{state['approved_count']} of {state['required_count']} reviewers approved"
        elif rs == "internally_approved":
            action_label = "Submit to customer"
            action_url = url_for("view_proposal", proposal_id=latest_prop.id)
            chip = ("Approved", "chip-ok")
            sub = "Internally approved · ready to send"
        elif rs == "submitted_to_customer":
            action_label = "Open"
            action_url = url_for("view_proposal", proposal_id=latest_prop.id)
            chip = ("Their move", "chip-violet")
            sub = "With the customer · awaiting response"
        elif rs == "customer_feedback":
            action_label = "Apply feedback"
            action_url = url_for("apply_feedback", proposal_id=latest_prop.id)
            chip = ("Customer feedback", "chip-violet")
            sub = f"{pending_req} customer request(s) to apply" if pending_req else "Customer replied with feedback"
        else:
            return None  # terminal states don't belong in the focus list

    overdue = False
    due_str = ""
    if p.due_date:
        due = p.due_date.replace(tzinfo=None) if p.due_date.tzinfo else p.due_date
        days = (due - now_naive).days
        if due < now_naive:
            overdue = True
            due_str = f"{abs(days)} day(s) overdue"
        elif days <= 7:
            due_str = f"due in {days} day(s)"
        else:
            due_str = "due " + due.strftime("%b %d")

    return {
        "kind": "project",
        "title": p.name,
        "url": url_for("project_upload", project_id=p.id),
        "client": p.client_name or "",
        "code": (latest_prop.document_type if latest_prop else "RFP") + "-" + p.id[:6].upper(),
        "chip": chip,
        "sub": sub,
        "due_str": due_str,
        "overdue": overdue,
        "value": p.dollar_amount or 0,
        "action_label": action_label,
        "action_url": action_url,
        "assigned_to_me": p.assigned_to == current_user.id,
        "with_customer": bool(latest_prop and latest_prop.review_status in ("submitted_to_customer", "customer_feedback")),
    }


@app.route("/quick-start", methods=["POST"])
@login_required
def quick_start():
    """Create a project directly from dropped RFP/RFQ files (dashboard hero)."""
    files = request.files.getlist("documents")
    valid = [
        f for f in files
        if f and f.filename and _allowed_file(f.filename, ALLOWED_EXTENSIONS | {"xlsx", "xls"})
    ]
    if not valid:
        flash("Please drop a PDF, DOCX, TXT, MD, or XLSX file to start a proposal session.", "error")
        return redirect(url_for("dashboard"))

    # Derive a readable project name from the first file
    stem = Path(valid[0].filename).stem.replace("_", " ").replace("-", " ").strip()
    project_name = (stem[:280] or "New Proposal") + " Proposal"

    ok, msg = billing_check_project(current_user.org_id)
    if not ok:
        flash(msg, "error")
        return redirect(url_for("billing_page"))

    project = Project(user_id=current_user.id, org_id=current_user.org_id, name=project_name)
    db.session.add(project)
    db.session.flush()

    for f in valid:
        safe, path, size = _save_upload(f, f"projects/{project.id}")
        db.session.add(ProjectDocument(
            project_id=project.id,
            filename=f"{uuid.uuid4().hex[:8]}_{safe}",
            original_filename=safe,
            file_type="rfp",
            file_path=path,
            file_size=size,
        ))

    db.session.commit()
    _log_activity("project_quick_start", f"Quick-started project from {len(valid)} file(s)", project.id)
    _notify_role(
        "proposal", "rfp_uploaded",
        f"New proposal session: {project.name}",
        f"{current_user.display_name or current_user.username} started a session with {len(valid)} document(s).",
        link=f"/projects/{project.id}/upload",
        exclude_user_id=current_user.id,
    )
    flash(f"Session started — {len(valid)} document(s) uploaded. Review and generate when ready.", "success")
    return redirect(url_for("project_upload", project_id=project.id))


@app.route("/")
@login_required
def dashboard():
    from sqlalchemy import func

    user_role = getattr(current_user, "role", None) or ("admin" if current_user.is_admin else "proposal")

    # Projects owned by user OR assigned to user
    my_project_filter = db.or_(
        Project.user_id == current_user.id,
        Project.assigned_to == current_user.id,
    )
    active_projects = Project.query.filter(my_project_filter, Project.status == "active").order_by(Project.updated_at.desc()).all()
    past_projects = Project.query.filter(
        my_project_filter,
        Project.status.in_(["submitted", "won", "lost", "archived"]),
    ).order_by(Project.updated_at.desc()).all()

    # Stats
    total = Project.query.filter(my_project_filter).count()
    won = Project.query.filter(my_project_filter, Project.status == "won").count()
    lost = Project.query.filter(my_project_filter, Project.status == "lost").count()
    decided = won + lost
    win_rate = round((won / decided) * 100) if decided > 0 else 0
    loss_rate = round((lost / decided) * 100) if decided > 0 else 0

    avg_dollar = db.session.query(func.avg(Project.dollar_amount)).filter(
        my_project_filter, Project.dollar_amount > 0
    ).scalar() or 0
    total_dollar = db.session.query(func.sum(Project.dollar_amount)).filter(
        my_project_filter, Project.dollar_amount > 0
    ).scalar() or 0

    total_proposals = Proposal.query.join(Project).filter(my_project_filter).count()

    stats = {
        "total_projects": total,
        "total_proposals": total_proposals,
        "won": won,
        "lost": lost,
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        "avg_dollar": avg_dollar,
        "total_dollar": total_dollar,
    }

    # Sales-focused extras: pipeline by status, recent proposals across team
    pipeline_by_status = {}
    if user_role == "sales":
        for status in ["active", "submitted", "won", "lost"]:
            cnt = Project.query.filter(my_project_filter, Project.status == status).count()
            val = db.session.query(func.sum(Project.dollar_amount)).filter(
                my_project_filter, Project.status == status, Project.dollar_amount > 0
            ).scalar() or 0
            pipeline_by_status[status] = {"count": cnt, "value": val}

    # Proposal-focused extras: docs needing proposals, recent generations
    pending_docs_projects = []
    recent_proposals = []
    if user_role == "proposal":
        # Projects with docs but no proposals
        my_projects = Project.query.filter(my_project_filter, Project.status == "active").all()
        for p in my_projects:
            doc_count = ProjectDocument.query.filter_by(project_id=p.id).count()
            prop_count = Proposal.query.filter_by(project_id=p.id).count()
            if doc_count > 0 and prop_count == 0:
                pending_docs_projects.append({"project": p, "doc_count": doc_count})
        recent_proposals = Proposal.query.join(Project).filter(
            my_project_filter
        ).order_by(Proposal.generated_at.desc()).limit(5).all()

    # Assigned to me (for proposal users)
    assigned_to_me = Project.query.filter_by(assigned_to=current_user.id, status="active").order_by(Project.updated_at.desc()).all()

    # Notifications
    notifications = Notification.query.filter_by(
        user_id=current_user.id, is_read=False
    ).order_by(Notification.created_at.desc()).limit(10).all()

    # Proposal users list (for sales assignment dropdown)
    proposal_users = _org_proposal_users()

    # Upcoming deadlines (next 7 days) and overdue — from active projects only
    from datetime import timedelta
    now_naive = datetime.utcnow()
    upcoming_end = now_naive + timedelta(days=7)
    upcoming_deadlines = []
    overdue_projects = []
    for p in active_projects:
        if not p.due_date:
            continue
        due = p.due_date.replace(tzinfo=None) if p.due_date.tzinfo else p.due_date
        if due < now_naive:
            overdue_projects.append(p)
        elif due <= upcoming_end:
            upcoming_deadlines.append(p)
    upcoming_deadlines.sort(key=lambda p: p.due_date)
    overdue_projects.sort(key=lambda p: p.due_date)

    # Close reason category labels (for the close-details form in past projects)
    close_category_labels = {
        "price": "Price",
        "scope": "Scope",
        "schedule": "Schedule / Timing",
        "relationship": "Relationship / Incumbent",
        "technical": "Technical Approach",
        "compliance": "Compliance / Requirements",
        "other": "Other",
    }

    # --- Part 3: Review widgets ----------------------------------------------

    # "Pending My Review" — proposals where I'm an assigned reviewer and I
    # haven't yet recorded a decision on the latest version.
    my_reviews_pending: list[dict] = []
    my_assignments = ProposalReviewer.query.filter_by(user_id=current_user.id).all()
    for r in my_assignments:
        prop = db.session.get(Proposal, r.proposal_id)
        if not prop:
            continue
        if prop.review_status not in ("in_review", "revision_requested"):
            continue
        version = latest_version(prop.id)
        if not version:
            continue
        decision = ProposalApproval.query.filter_by(
            proposal_id=prop.id, version_id=version.id, user_id=current_user.id
        ).first()
        if decision is not None:
            continue
        proj = db.session.get(Project, prop.project_id)
        my_reviews_pending.append({
            "proposal": prop,
            "project": proj,
            "reviewer": r,
            "version_number": version.version_number,
            "deadline": r.deadline,
            "overdue": bool(r.deadline) and r.deadline < datetime.now(timezone.utc),
        })

    # "Out for Review" — proposals I own that are currently in_review /
    # revision_requested / internally_approved (awaiting customer send).
    out_for_review: list[dict] = []
    owned_props = (
        Proposal.query.join(Project)
        .filter(Project.user_id == current_user.id,
                Proposal.review_status.in_(("in_review", "revision_requested", "internally_approved")))
        .all()
    )
    for prop in owned_props:
        state = approval_state(prop)
        proj = db.session.get(Project, prop.project_id)
        out_for_review.append({
            "proposal": prop,
            "project": proj,
            "state": state,
            "pending_req_count": RevisionRequest.query.filter_by(
                proposal_id=prop.id, status="pending"
            ).count(),
        })

    # "Awaiting Customer Response" — proposals I own that are out to customer
    awaiting_customer = (
        Proposal.query.join(Project)
        .filter(Project.user_id == current_user.id,
                Proposal.review_status.in_(("submitted_to_customer", "customer_feedback")))
        .all()
    )
    awaiting_customer_items = []
    for prop in awaiting_customer:
        proj = db.session.get(Project, prop.project_id)
        awaiting_customer_items.append({"proposal": prop, "project": proj})

    # --- "Pick up where you left off" focus list -----------------------------
    focus_items = []

    # Reviews waiting on my decision come first — they block teammates.
    for item in my_reviews_pending:
        proj = item["project"]
        focus_items.append({
            "kind": "review",
            "title": proj.name if proj else "Proposal review",
            "url": url_for("proposal_review_page", proposal_id=item["proposal"].id),
            "client": proj.client_name if proj else "",
            "code": f"REV-{item['proposal'].id[:6].upper()}",
            "chip": ("Review requested", "chip-gold"),
            "sub": f"You're the {item['reviewer'].review_role.title()} reviewer on v{item['version_number']}",
            "due_str": ("due " + item["deadline"].strftime("%b %d")) if item["deadline"] else "",
            "overdue": item["overdue"],
            "value": (proj.dollar_amount or 0) if proj else 0,
            "action_label": "Start review",
            "action_url": url_for("proposal_review_page", proposal_id=item["proposal"].id),
            "assigned_to_me": True,
            "with_customer": False,
        })

    for p in active_projects:
        entry = _project_focus_entry(p, now_naive)
        if entry:
            focus_items.append(entry)

    # Critical (overdue) first, then largest exposure
    focus_items.sort(key=lambda i: (not i["overdue"], -(i["value"] or 0)))
    combined_exposure = sum(i["value"] or 0 for i in focus_items)

    # Greeting
    hour = datetime.now().hour
    if hour < 12:
        greeting_word = "Good morning"
    elif hour < 17:
        greeting_word = "Good afternoon"
    else:
        greeting_word = "Good evening"
    first_name = (current_user.display_name or current_user.username).split(" ")[0]
    today_str = datetime.now().strftime("%A, %B %-d") if os.name != "nt" else datetime.now().strftime("%A, %B %d")

    return render_template(
        "dashboard.html",
        focus_items=focus_items,
        combined_exposure=combined_exposure,
        greeting_word=greeting_word,
        first_name=first_name,
        today_str=today_str,
        overdue_count=len(overdue_projects),
        due_soon_count=len(upcoming_deadlines),
        active_projects=active_projects,
        past_projects=past_projects,
        stats=stats,
        user_role=user_role,
        pipeline_by_status=pipeline_by_status,
        pending_docs_projects=pending_docs_projects,
        recent_proposals=recent_proposals,
        assigned_to_me=assigned_to_me,
        notifications=notifications,
        proposal_users=proposal_users,
        upcoming_deadlines=upcoming_deadlines,
        overdue_projects=overdue_projects,
        close_category_labels=close_category_labels,
        my_reviews_pending=my_reviews_pending,
        out_for_review=out_for_review,
        awaiting_customer_items=awaiting_customer_items,
        lifecycle_labels=LIFECYCLE_LABELS,
    )


# ---------------------------------------------------------------------------
# Proposals list page
# ---------------------------------------------------------------------------

BOARD_COLUMNS = [
    ("scoping", "Upload & Scope"),
    ("drafting", "Drafting"),
    ("review", "Internal Review"),
    ("customer", "With Customer"),
    ("won", "Won"),
    ("lost", "Lost"),
    ("storage", "Storage"),
]

PROPOSAL_FILTERS = [
    ("all", "All", ""),
    ("mine", "My Proposals", ""),
    ("overdue", "Overdue", "pf-red"),
    ("unassigned", "Unassigned", "pf-gold"),
    ("awaiting_generation", "Awaiting Generation", "pf-gold"),
    ("in_review", "In Review", ""),
    ("revisions", "Revisions Requested", "pf-gold"),
    ("awaiting_customer", "Awaiting Customer", "pf-violet"),
    ("won", "Won", "pf-ok"),
    ("lost", "Lost", "pf-red"),
]


def _build_proposal_rows():
    """Load all accessible projects enriched with their latest proposal state."""
    if current_user.is_admin:
        projects = Project.query.filter_by(org_id=current_user.org_id).order_by(
            Project.updated_at.desc()
        ).all()
    else:
        projects = Project.query.filter(_my_projects_filter()).order_by(
            Project.updated_at.desc()
        ).all()

    now_naive = datetime.utcnow()
    rows = []
    for p in projects:
        latest = (
            Proposal.query.filter_by(project_id=p.id)
            .order_by(Proposal.generated_at.desc())
            .first()
        )
        doc_count = ProjectDocument.query.filter_by(project_id=p.id).count()
        pending_req = 0
        if latest:
            pending_req = RevisionRequest.query.filter_by(
                proposal_id=latest.id, status="pending"
            ).count()

        overdue = False
        if p.due_date and p.status == "active":
            due = p.due_date.replace(tzinfo=None) if p.due_date.tzinfo else p.due_date
            overdue = due < now_naive

        health = latest.confidence_score if latest else None
        if health is not None and health >= 80:
            health_class = "health-good"
        elif health is not None and health >= 60:
            health_class = "health-fair"
        elif health is not None:
            health_class = "health-poor"
        else:
            health_class = ""

        if latest:
            status_label = LIFECYCLE_LABELS.get(latest.review_status, latest.review_status)
            status_class = f"badge-review badge-review-{latest.review_status}"
        else:
            status_label = "Awaiting Generation" if doc_count else "Needs Documents"
            status_class = "badge-status-open" if doc_count else "badge-status-draft"
        if p.status in ("won", "lost", "archived", "submitted") and not latest:
            status_label = p.status.title()
            status_class = f"badge-status-{p.status}"

        # Board column (Kanban view)
        rs = latest.review_status if latest else None
        if p.status == "archived":
            board_col = "storage"
        elif p.status == "won" or rs in ("won", "customer_approved"):
            board_col = "won"
        elif p.status == "lost" or rs in ("lost", "customer_declined"):
            board_col = "lost"
        elif rs in ("submitted_to_customer", "customer_feedback"):
            board_col = "customer"
        elif rs in ("in_review", "revision_requested", "internally_approved"):
            board_col = "review"
        elif rs == "draft":
            board_col = "drafting"
        else:
            board_col = "scoping"

        rows.append({
            "project": p,
            "proposal": latest,
            "doc_count": doc_count,
            "pending_req": pending_req,
            "overdue": overdue,
            "health": health,
            "health_class": health_class,
            "status_label": status_label,
            "status_class": status_class,
            "board_col": board_col,
        })
    return rows


def _filter_proposal_rows(rows, flt):
    uid = current_user.id
    if flt == "mine":
        return [r for r in rows if r["project"].user_id == uid]
    if flt == "overdue":
        return [r for r in rows if r["overdue"]]
    if flt == "unassigned":
        return [r for r in rows if r["project"].status == "active" and not r["project"].assigned_to]
    if flt == "awaiting_generation":
        return [r for r in rows if r["project"].status == "active" and r["doc_count"] > 0 and not r["proposal"]]
    if flt == "in_review":
        return [r for r in rows if r["proposal"] and r["proposal"].review_status in ("in_review", "revision_requested")]
    if flt == "revisions":
        return [r for r in rows if (r["proposal"] and r["proposal"].review_status == "revision_requested") or r["pending_req"] > 0]
    if flt == "awaiting_customer":
        return [r for r in rows if r["proposal"] and r["proposal"].review_status in ("submitted_to_customer", "customer_feedback")]
    if flt == "won":
        return [r for r in rows if r["project"].status == "won"]
    if flt == "lost":
        return [r for r in rows if r["project"].status == "lost"]
    return rows


def _apply_row_query_filters(rows):
    """Apply search / status / vertical filters + sorting from query params."""
    q = request.args.get("q", "").strip().lower()
    f_status = request.args.get("status", "")
    f_vertical = request.args.get("vertical", "")
    sort = request.args.get("sort", "updated")
    direction = request.args.get("dir", "desc")

    if q:
        rows = [r for r in rows
                if q in (r["project"].name or "").lower()
                or q in (r["project"].client_name or "").lower()]
    if f_status:
        rows = [r for r in rows if r["project"].status == f_status]
    if f_vertical:
        rows = [r for r in rows if r["project"].vertical == f_vertical]

    keymap = {
        "title": lambda r: (r["project"].name or "").lower(),
        "client": lambda r: (r["project"].client_name or "").lower(),
        "vertical": lambda r: (r["project"].vertical_label or "").lower(),
        "value": lambda r: r["project"].dollar_amount or 0,
        "status": lambda r: r["status_label"],
        "due": lambda r: r["project"].due_date.replace(tzinfo=None) if r["project"].due_date else datetime.max,
        "health": lambda r: r["health"] if r["health"] is not None else -1,
        "updated": lambda r: r["project"].updated_at or datetime.min,
    }
    keyfn = keymap.get(sort, keymap["updated"])
    rows.sort(key=keyfn, reverse=(direction != "asc"))
    return rows


@app.route("/proposals")
@login_required
def proposals_list():
    flt = request.args.get("filter", "all")
    if flt not in {f[0] for f in PROPOSAL_FILTERS}:
        flt = "all"

    all_rows = _build_proposal_rows()
    counts = {key: len(_filter_proposal_rows(all_rows, key)) for key, _, _ in PROPOSAL_FILTERS}
    rows = _apply_row_query_filters(_filter_proposal_rows(all_rows, flt))

    proposal_users = _org_proposal_users()

    close_category_labels = {
        "price": "Price", "scope": "Scope", "schedule": "Schedule / Timing",
        "relationship": "Relationship / Incumbent", "technical": "Technical Approach",
        "compliance": "Compliance / Requirements", "other": "Other",
    }

    return render_template(
        "proposals.html",
        rows=rows,
        counts=counts,
        active_filter=flt,
        filters=PROPOSAL_FILTERS,
        board_columns=BOARD_COLUMNS,
        verticals=VERTICALS,
        proposal_users=proposal_users,
        close_category_labels=close_category_labels,
        q=request.args.get("q", ""),
        f_status=request.args.get("status", ""),
        f_vertical=request.args.get("vertical", ""),
        sort=request.args.get("sort", "updated"),
        direction=request.args.get("dir", "desc"),
        view=request.args.get("view", "table"),
    )


@app.route("/proposals/export.csv")
@login_required
def proposals_export_csv():
    """Export the current (filtered) proposals list as CSV."""
    import csv
    import io
    from flask import Response

    flt = request.args.get("filter", "all")
    if flt not in {f[0] for f in PROPOSAL_FILTERS}:
        flt = "all"
    rows = _apply_row_query_filters(_filter_proposal_rows(_build_proposal_rows(), flt))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Title", "Client", "Vertical", "Value", "Status", "Health",
                     "Due Date", "Assigned To", "Created", "Updated"])
    for r in rows:
        p = r["project"]
        writer.writerow([
            p.name,
            p.client_name or "",
            p.vertical_label or "",
            p.dollar_amount or 0,
            r["status_label"],
            r["health"] if r["health"] is not None else "",
            p.due_date.strftime("%Y-%m-%d") if p.due_date else "",
            (p.assignee.display_name or p.assignee.username) if p.assignee else "",
            p.created_at.strftime("%Y-%m-%d") if p.created_at else "",
            p.updated_at.strftime("%Y-%m-%d") if p.updated_at else "",
        ])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=proposals_{flt}.csv"},
    )


# ---------------------------------------------------------------------------
# Proposal Posture (company setup: templates, standards, rates, branding)
# ---------------------------------------------------------------------------


@app.route("/posture")
@login_required
def posture():
    rate_sheets = UserRateSheet.query.filter_by(org_id=current_user.org_id).order_by(UserRateSheet.uploaded_at.desc()).all()
    user_templates = UserVerticalTemplate.query.filter_by(org_id=current_user.org_id, is_company_default=False).order_by(UserVerticalTemplate.uploaded_at.desc()).all()
    staff_roles = StaffRole.query.filter_by(org_id=current_user.org_id).order_by(StaffRole.category, StaffRole.role_name).all()
    equipment_items = EquipmentItem.query.filter_by(org_id=current_user.org_id).order_by(EquipmentItem.category, EquipmentItem.item_name).all()
    travel_rates = TravelExpenseRate.query.filter_by(org_id=current_user.org_id).order_by(TravelExpenseRate.expense_type).all()
    company_standards = CompanyStandard.query.filter_by(org_id=current_user.org_id).order_by(CompanyStandard.category, CompanyStandard.title).all()
    revision_templates = RevisionTemplate.query.filter_by(org_id=current_user.org_id).order_by(
        RevisionTemplate.category, RevisionTemplate.name
    ).all()

    # "Posture version" summary: most recent change across all posture content
    timestamps = []
    for coll, attr in (
        (rate_sheets, "uploaded_at"), (user_templates, "uploaded_at"),
        (staff_roles, "updated_at"), (equipment_items, "updated_at"),
        (travel_rates, "updated_at"), (company_standards, "updated_at"),
        (revision_templates, "created_at"),
    ):
        for item in coll:
            ts = getattr(item, attr, None)
            if ts:
                timestamps.append(ts)
    last_updated = max(timestamps) if timestamps else None
    total_items = (len(rate_sheets) + len(user_templates) + len(staff_roles)
                   + len(equipment_items) + len(travel_rates)
                   + len(company_standards) + len(revision_templates))

    return render_template(
        "posture.html",
        rate_sheets=rate_sheets,
        user_templates=user_templates,
        staff_roles=staff_roles,
        equipment_items=equipment_items,
        travel_rates=travel_rates,
        company_standards=company_standards,
        revision_templates=revision_templates,
        revision_categories=REVISION_CATEGORIES,
        verticals=VERTICALS,
        logo_max_dimension=LOGO_MAX_DIMENSION,
        last_updated=last_updated,
        total_items=total_items,
    )


@app.route("/setup")
@login_required
def setup_wizard():
    progress = _setup_progress(current_user)
    return render_template("setup.html", progress=progress)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        current_user.display_name = request.form.get("display_name", current_user.display_name)
        current_user.email = request.form.get("email", current_user.email)
        current_user.company_name = request.form.get("company_name", current_user.company_name)
        current_user.font_preference = request.form.get("font_preference", current_user.font_preference)
        current_user.llm_provider = request.form.get("llm_provider", current_user.llm_provider)
        current_user.llm_model = request.form.get("llm_model", current_user.llm_model)

        api_key = request.form.get("api_key", "").strip()
        if api_key:
            current_user.api_key_encrypted = encrypt_api_key(api_key)

        # Company logo preferences (checkboxes come through only when checked)
        current_user.company_logo_use_in_proposals = bool(request.form.get("company_logo_use_in_proposals"))
        current_user.company_logo_show_on_cover = bool(request.form.get("company_logo_show_on_cover"))
        placement = request.form.get("company_logo_placement", current_user.company_logo_placement or "top_left")
        if placement in ("top_left", "center"):
            current_user.company_logo_placement = placement

        db.session.commit()
        _log_activity("settings_update", "Updated user settings")
        flash("Settings saved.", "success")
        if request.form.get("from_posture"):
            return redirect(url_for("posture") + "#branding")
        return redirect(url_for("settings"))

    # Rate sheets
    rate_sheets = UserRateSheet.query.filter_by(org_id=current_user.org_id).order_by(UserRateSheet.uploaded_at.desc()).all()
    # User vertical templates
    user_templates = UserVerticalTemplate.query.filter_by(org_id=current_user.org_id, is_company_default=False).order_by(UserVerticalTemplate.uploaded_at.desc()).all()
    # Staff roles
    staff_roles = StaffRole.query.filter_by(org_id=current_user.org_id).order_by(StaffRole.category, StaffRole.role_name).all()
    # Equipment items
    equipment_items = EquipmentItem.query.filter_by(org_id=current_user.org_id).order_by(EquipmentItem.category, EquipmentItem.item_name).all()
    # Travel expense rates
    travel_rates = TravelExpenseRate.query.filter_by(org_id=current_user.org_id).order_by(TravelExpenseRate.expense_type).all()
    # Company standards
    company_standards = CompanyStandard.query.filter_by(org_id=current_user.org_id).order_by(CompanyStandard.category, CompanyStandard.title).all()
    # Revision request templates (Part 3)
    revision_templates = RevisionTemplate.query.filter_by(org_id=current_user.org_id).order_by(
        RevisionTemplate.category, RevisionTemplate.name
    ).all()

    return render_template(
        "settings.html",
        rate_sheets=rate_sheets,
        user_templates=user_templates,
        staff_roles=staff_roles,
        equipment_items=equipment_items,
        travel_rates=travel_rates,
        company_standards=company_standards,
        revision_templates=revision_templates,
        revision_categories=REVISION_CATEGORIES,
        verticals=VERTICALS,
        logo_max_dimension=LOGO_MAX_DIMENSION,
    )


@app.route("/settings/upload-rate-sheet", methods=["POST"])
@login_required
def upload_rate_sheet():
    file = request.files.get("rate_sheet")
    sheet_type = request.form.get("sheet_type", "labor_rates")

    if not file or not _allowed_file(file.filename, RATE_SHEET_EXTENSIONS):
        flash("Please upload an Excel file (.xlsx).", "error")
        return redirect(url_for("posture") + "#staff-rates")

    safe, path, size = _save_upload(file, f"rate_sheets/{current_user.id}")

    sheet = UserRateSheet(
        user_id=current_user.id,
        org_id=current_user.org_id,
        name=request.form.get("sheet_name", safe),
        sheet_type=sheet_type,
        file_path=path,
        original_filename=safe,
    )
    db.session.add(sheet)
    db.session.commit()
    _log_activity("rate_sheet_upload", f"Uploaded {sheet_type}: {safe}")
    flash("Rate sheet uploaded.", "success")
    return redirect(url_for("posture") + "#staff-rates")


# ---------------------------------------------------------------------------
# Ingestion review flows (Phase 4): parse → AI-extract → review → import
# ---------------------------------------------------------------------------

_INGEST_TARGETS = {
    "labor_rates": {"label": "Staff Rates", "anchor": "#staff-rates",
                    "cols": [("role_name", "Role"), ("category", "Category"),
                             ("hourly_rate", "Hourly Rate"), ("overtime_rate", "OT Rate")]},
    "product_pricing": {"label": "Equipment / Products", "anchor": "#equipment",
                        "cols": [("item_name", "Item"), ("category", "Category"),
                                 ("part_number", "Part #"), ("manufacturer", "Manufacturer"),
                                 ("unit_cost", "Unit Cost"), ("unit", "Unit")]},
    "travel": {"label": "Travel Rates", "anchor": "#travel",
               "cols": [("expense_type", "Expense"), ("rate", "Rate"),
                        ("unit", "Unit"), ("description", "Description")]},
}


@app.route("/posture/ingest-rates", methods=["POST"])
@login_required
def ingest_rates():
    """Parse an uploaded rate sheet and AI-map it into structured rows for review."""
    file = request.files.get("ingest_file")
    target = request.form.get("target_type", "labor_rates")
    if target not in _INGEST_TARGETS:
        target = "labor_rates"
    if not file or not file.filename:
        flash("Please choose a file to import.", "error")
        return redirect(url_for("posture"))
    if not _allowed_file(file.filename, INGEST_RATE_EXTENSIONS):
        flash("Please upload an Excel, CSV, PDF, or Word file.", "error")
        return redirect(url_for("posture"))

    safe, path, size = _save_upload(file, f"ingest/{current_user.org_id}")
    try:
        if path.lower().endswith((".xlsx", ".xls")):
            parsed = parse_rate_sheet(path)
            raw_text = parsed.get("raw_text", "")
        elif path.lower().endswith(".csv"):
            raw_text = Path(path).read_text(encoding="utf-8", errors="replace")
        else:
            raw_text = parse_document(path)
    except Exception as e:
        flash(f"Could not read the file: {e}", "error")
        return redirect(url_for("posture") + _INGEST_TARGETS[target]["anchor"])

    try:
        rows = extract_rates_from_sheet(
            raw_text, target,
            user_api_key=decrypt_api_key(current_user.api_key_encrypted) or None,
            user_model=current_user.llm_model or None,
        )
    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("posture") + _INGEST_TARGETS[target]["anchor"])
    except Exception as e:
        flash(f"Extraction failed: {friendly_api_error(e)}", "error")
        return redirect(url_for("posture") + _INGEST_TARGETS[target]["anchor"])

    if not rows:
        flash("The AI couldn't find any rows to import. You can still add rates manually.", "error")
        return redirect(url_for("posture") + _INGEST_TARGETS[target]["anchor"])

    return render_template(
        "ingest_review.html",
        mode="rates", target=target, meta=_INGEST_TARGETS[target],
        rows=rows, source_name=safe,
    )


@app.route("/posture/ingest-rates/confirm", methods=["POST"])
@login_required
def ingest_rates_confirm():
    target = request.form.get("target_type", "labor_rates")
    if target not in _INGEST_TARGETS:
        abort(400)
    count = 0
    n = len(request.form.getlist("include"))
    includes = set(request.form.getlist("include"))

    def _num(name, i):
        vals = request.form.getlist(name)
        try:
            return float((vals[i] if i < len(vals) else "0").replace(",", "").replace("$", "") or 0)
        except ValueError:
            return 0.0

    def _str(name, i):
        vals = request.form.getlist(name)
        return (vals[i] if i < len(vals) else "").strip()

    total_rows = len(request.form.getlist("row_marker"))
    for i in range(total_rows):
        if str(i) not in includes:
            continue
        if target == "labor_rates":
            name = _str("role_name", i)
            if not name:
                continue
            db.session.add(StaffRole(
                user_id=current_user.id, org_id=current_user.org_id,
                role_name=name, category=_str("category", i),
                hourly_rate=_num("hourly_rate", i), overtime_rate=_num("overtime_rate", i),
            ))
        elif target == "product_pricing":
            name = _str("item_name", i)
            if not name:
                continue
            db.session.add(EquipmentItem(
                user_id=current_user.id, org_id=current_user.org_id,
                item_name=name, category=_str("category", i),
                part_number=_str("part_number", i), manufacturer=_str("manufacturer", i),
                unit_cost=_num("unit_cost", i), unit=_str("unit", i) or "each",
            ))
        elif target == "travel":
            name = _str("expense_type", i)
            if not name:
                continue
            db.session.add(TravelExpenseRate(
                user_id=current_user.id, org_id=current_user.org_id,
                expense_type=name, rate=_num("rate", i),
                unit=_str("unit", i) or "per day", description=_str("description", i),
            ))
        count += 1
    db.session.commit()
    _log_activity("rates_ingest", f"Imported {count} {target} row(s)")
    flash(f"Imported {count} row(s) into your posture.", "success")
    return redirect(url_for("posture") + _INGEST_TARGETS[target]["anchor"])


@app.route("/posture/ingest-standards", methods=["POST"])
@login_required
def ingest_standards():
    """Parse a document and propose reusable company standard blocks for review."""
    file = request.files.get("standards_file")
    if not file or not file.filename:
        flash("Please choose a document to import.", "error")
        return redirect(url_for("posture") + "#standards")
    if not _allowed_file(file.filename, INGEST_STANDARDS_EXTENSIONS):
        flash("Please upload a PDF, Word, or text document.", "error")
        return redirect(url_for("posture") + "#standards")
    safe, path, size = _save_upload(file, f"ingest/{current_user.org_id}")
    try:
        text = parse_document(path)
    except Exception as e:
        flash(f"Could not read the document: {e}", "error")
        return redirect(url_for("posture") + "#standards")
    try:
        blocks = extract_standards(
            text,
            user_api_key=decrypt_api_key(current_user.api_key_encrypted) or None,
            user_model=current_user.llm_model or None,
        )
    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("posture") + "#standards")
    except Exception as e:
        flash(f"Extraction failed: {friendly_api_error(e)}", "error")
        return redirect(url_for("posture") + "#standards")
    if not blocks:
        flash("The AI couldn't extract standards from that document.", "error")
        return redirect(url_for("posture") + "#standards")
    return render_template("ingest_review.html", mode="standards", blocks=blocks, source_name=safe)


@app.route("/posture/ingest-standards/confirm", methods=["POST"])
@login_required
def ingest_standards_confirm():
    includes = set(request.form.getlist("include"))
    categories = request.form.getlist("category")
    titles = request.form.getlist("title")
    contents = request.form.getlist("content")
    count = 0
    for i in range(len(titles)):
        if str(i) not in includes:
            continue
        title = (titles[i] or "").strip()
        content = (contents[i] or "").strip()
        if not title or not content:
            continue
        db.session.add(CompanyStandard(
            user_id=current_user.id, org_id=current_user.org_id,
            category=(categories[i] if i < len(categories) else "general").strip() or "general",
            title=title[:300], content=content,
        ))
        count += 1
    db.session.commit()
    _log_activity("standards_ingest", f"Imported {count} standard(s)")
    flash(f"Imported {count} company standard(s).", "success")
    return redirect(url_for("posture") + "#standards")


@app.route("/settings/upload-template", methods=["POST"])
@login_required
def upload_user_template():
    file = request.files.get("template_file")
    vertical = request.form.get("vertical", "general")
    template_type = request.form.get("template_type", "proposal")

    if not file or not _allowed_file(file.filename, TEMPLATE_EXTENSIONS):
        flash("Please upload a Word or PDF file.", "error")
        return redirect(url_for("posture") + "#templates")

    safe, path, size = _save_upload(file, f"user_templates/{current_user.id}")

    tmpl = UserVerticalTemplate(
        user_id=current_user.id,
        org_id=current_user.org_id,
        vertical=vertical,
        template_type=template_type,
        name=request.form.get("template_name", safe),
        file_path=path,
        original_filename=safe,
        is_company_default=False,
    )
    db.session.add(tmpl)
    db.session.commit()
    _log_activity("template_upload", f"Uploaded {template_type} for {vertical}: {safe}")
    flash("Template uploaded.", "success")
    return redirect(url_for("posture") + "#templates")


@app.route("/settings/delete-rate-sheet/<sheet_id>", methods=["POST"])
@login_required
def delete_rate_sheet(sheet_id):
    sheet = db.session.get(UserRateSheet, sheet_id)
    if not sheet or sheet.org_id != current_user.org_id:
        abort(404)
    db.session.delete(sheet)
    db.session.commit()
    flash("Rate sheet deleted.", "success")
    return redirect(url_for("posture") + "#staff-rates")


@app.route("/settings/delete-template/<template_id>", methods=["POST"])
@login_required
def delete_user_template(template_id):
    tmpl = db.session.get(UserVerticalTemplate, template_id)
    if not tmpl or tmpl.org_id != current_user.org_id:
        abort(404)
    db.session.delete(tmpl)
    db.session.commit()
    flash("Template deleted.", "success")
    return redirect(url_for("posture") + "#templates")


# ---------------------------------------------------------------------------
# Company logo upload
# ---------------------------------------------------------------------------

@app.route("/settings/upload-logo", methods=["POST"])
@login_required
def upload_company_logo():
    file = request.files.get("company_logo")
    if not file or not file.filename:
        flash("Please choose an image file to upload.", "error")
        return redirect(url_for("posture") + "#branding")

    if not _allowed_file(file.filename, LOGO_EXTENSIONS):
        flash(
            "Unsupported image type. Please upload a PNG, JPG, JPEG, WebP, GIF, or BMP.",
            "error",
        )
        return redirect(url_for("posture") + "#branding")

    try:
        original_name, saved_path, saved_size = _process_and_save_logo(file, current_user.id)
    except ValueError as e:
        flash(f"Could not process image: {e}", "error")
        return redirect(url_for("posture") + "#branding")

    # Remove old logo file if any
    if current_user.company_logo_path:
        try:
            old = Path(current_user.company_logo_path)
            if old.exists() and old.is_file():
                old.unlink()
        except Exception:
            pass

    current_user.company_logo_path = saved_path
    current_user.company_logo_original_name = original_name
    # If this is the user's first logo upload, default "use in proposals" to True
    if not current_user.company_logo_placement:
        current_user.company_logo_placement = "top_left"
    db.session.commit()
    _log_activity("logo_upload", f"Uploaded company logo ({saved_size // 1024} KB)")
    flash(
        f"Logo uploaded and optimized ({saved_size // 1024} KB). "
        "You can now choose how it appears on your proposals.",
        "success",
    )
    return redirect(url_for("posture") + "#branding")


@app.route("/settings/delete-logo", methods=["POST"])
@login_required
def delete_company_logo():
    if current_user.company_logo_path:
        try:
            old = Path(current_user.company_logo_path)
            if old.exists() and old.is_file():
                old.unlink()
        except Exception:
            pass
    current_user.company_logo_path = ""
    current_user.company_logo_original_name = ""
    db.session.commit()
    _log_activity("logo_delete", "Removed company logo")
    flash("Company logo removed.", "success")
    return redirect(url_for("posture") + "#branding")


@app.route("/settings/logo-preview")
@login_required
def company_logo_preview():
    """Serve the current user's company logo (private — only to the owner)."""
    if not current_user.company_logo_path:
        abort(404)
    logo_path = Path(current_user.company_logo_path)
    if not logo_path.exists():
        abort(404)
    return send_file(str(logo_path), mimetype="image/png")


# ---------------------------------------------------------------------------
# Staff Roles
# ---------------------------------------------------------------------------

@app.route("/settings/add-staff-role", methods=["POST"])
@login_required
def add_staff_role():
    # Handle file upload as rate sheet
    uploaded_file = request.files.get("staff_rate_file")
    if uploaded_file and uploaded_file.filename and _allowed_file(uploaded_file.filename, ALLOWED_EXTENSIONS | {"xlsx", "xls"}):
        safe, path, size = _save_upload(uploaded_file, "rate_sheets")
        sheet = UserRateSheet(
            user_id=current_user.id,
        org_id=current_user.org_id,
            name=f"Staff Rates - {safe}",
            sheet_type="labor_rates",
            file_path=path,
            original_filename=safe,
        )
        db.session.add(sheet)
        db.session.commit()
        _log_activity("rate_sheet_upload", f"Uploaded staff rate sheet: {safe}")
        flash(f"Rate sheet '{safe}' uploaded.", "success")
        return redirect(url_for("posture") + "#staff-rates")

    role_name = request.form.get("role_name", "").strip()
    category = request.form.get("category", "").strip()
    hourly_rate = request.form.get("hourly_rate", "0")
    overtime_rate = request.form.get("overtime_rate", "0")
    description = request.form.get("description", "").strip()

    if not role_name or not hourly_rate:
        flash("Role name and hourly rate are required.", "error")
        return redirect(url_for("posture") + "#staff-rates")

    try:
        hourly_rate = float(hourly_rate.replace(",", "").replace("$", ""))
        overtime_rate = float(overtime_rate.replace(",", "").replace("$", "")) if overtime_rate else 0.0
    except ValueError:
        flash("Invalid rate format.", "error")
        return redirect(url_for("posture") + "#staff-rates")

    if hourly_rate < 0 or overtime_rate < 0:
        flash("Rates cannot be negative.", "error")
        return redirect(url_for("posture") + "#staff-rates")

    role = StaffRole(
        user_id=current_user.id,
        org_id=current_user.org_id,
        role_name=role_name,
        category=category,
        hourly_rate=hourly_rate,
        overtime_rate=overtime_rate,
        description=description,
    )
    db.session.add(role)
    db.session.commit()
    _log_activity("staff_role_add", f"Added staff role: {role_name} @ ${hourly_rate}/hr")
    flash(f"Staff role '{role_name}' added.", "success")
    return redirect(url_for("posture") + "#staff-rates")


@app.route("/settings/edit-staff-role/<role_id>", methods=["POST"])
@login_required
def edit_staff_role(role_id):
    role = db.session.get(StaffRole, role_id)
    if not role or role.org_id != current_user.org_id:
        abort(404)

    role.role_name = request.form.get("role_name", role.role_name).strip()
    role.category = request.form.get("category", role.category).strip()
    role.description = request.form.get("description", role.description).strip()

    try:
        role.hourly_rate = float(request.form.get("hourly_rate", str(role.hourly_rate)).replace(",", "").replace("$", ""))
        ot = request.form.get("overtime_rate", str(role.overtime_rate))
        role.overtime_rate = float(ot.replace(",", "").replace("$", "")) if ot else 0.0
    except ValueError:
        flash("Invalid rate format.", "error")
        return redirect(url_for("posture") + "#staff-rates")

    db.session.commit()
    _log_activity("staff_role_edit", f"Updated staff role: {role.role_name}")
    flash(f"Staff role '{role.role_name}' updated.", "success")
    return redirect(url_for("posture") + "#staff-rates")


@app.route("/settings/delete-staff-role/<role_id>", methods=["POST"])
@login_required
def delete_staff_role(role_id):
    role = db.session.get(StaffRole, role_id)
    if not role or role.org_id != current_user.org_id:
        abort(404)
    name = role.role_name
    db.session.delete(role)
    db.session.commit()
    _log_activity("staff_role_delete", f"Deleted staff role: {name}")
    flash(f"Staff role '{name}' deleted.", "success")
    return redirect(url_for("posture") + "#staff-rates")


# ---------------------------------------------------------------------------
# Equipment / Materials Price List
# ---------------------------------------------------------------------------

@app.route("/settings/add-equipment-item", methods=["POST"])
@login_required
def add_equipment_item():
    # Handle file upload as price list
    uploaded_file = request.files.get("equipment_file")
    if uploaded_file and uploaded_file.filename and _allowed_file(uploaded_file.filename, ALLOWED_EXTENSIONS | {"xlsx", "xls"}):
        safe, path, size = _save_upload(uploaded_file, "rate_sheets")
        sheet = UserRateSheet(
            user_id=current_user.id,
        org_id=current_user.org_id,
            name=f"Equipment Price List - {safe}",
            sheet_type="product_pricing",
            file_path=path,
            original_filename=safe,
        )
        db.session.add(sheet)
        db.session.commit()
        _log_activity("rate_sheet_upload", f"Uploaded equipment price list: {safe}")
        flash(f"Price list '{safe}' uploaded.", "success")
        return redirect(url_for("posture") + "#equipment")

    item_name = request.form.get("item_name", "").strip()
    category = request.form.get("eq_category", "").strip()
    part_number = request.form.get("part_number", "").strip()
    manufacturer = request.form.get("manufacturer", "").strip()
    unit_cost = request.form.get("unit_cost", "0")
    unit = request.form.get("unit", "each").strip()
    description = request.form.get("eq_description", "").strip()

    if not item_name or not unit_cost:
        flash("Item name and unit cost are required.", "error")
        return redirect(url_for("posture") + "#equipment")

    try:
        unit_cost = float(unit_cost.replace(",", "").replace("$", ""))
    except ValueError:
        flash("Invalid cost format.", "error")
        return redirect(url_for("posture") + "#equipment")

    if unit_cost < 0:
        flash("Cost cannot be negative.", "error")
        return redirect(url_for("posture") + "#equipment")

    item = EquipmentItem(
        user_id=current_user.id,
        org_id=current_user.org_id,
        item_name=item_name,
        category=category,
        part_number=part_number,
        manufacturer=manufacturer,
        unit_cost=unit_cost,
        unit=unit,
        description=description,
    )
    db.session.add(item)
    db.session.commit()
    _log_activity("equipment_add", f"Added equipment: {item_name} @ ${unit_cost}/{unit}")
    flash(f"Equipment item '{item_name}' added.", "success")
    return redirect(url_for("posture") + "#equipment")


@app.route("/settings/delete-equipment-item/<item_id>", methods=["POST"])
@login_required
def delete_equipment_item(item_id):
    item = db.session.get(EquipmentItem, item_id)
    if not item or item.org_id != current_user.org_id:
        abort(404)
    name = item.item_name
    db.session.delete(item)
    db.session.commit()
    _log_activity("equipment_delete", f"Deleted equipment: {name}")
    flash(f"Equipment item '{name}' deleted.", "success")
    return redirect(url_for("posture") + "#equipment")


# ---------------------------------------------------------------------------
# Travel & Expense Rates
# ---------------------------------------------------------------------------

@app.route("/settings/add-travel-rate", methods=["POST"])
@login_required
def add_travel_rate():
    # Handle file upload as travel rate schedule
    uploaded_file = request.files.get("travel_rate_file")
    if uploaded_file and uploaded_file.filename and _allowed_file(uploaded_file.filename, ALLOWED_EXTENSIONS | {"xlsx", "xls"}):
        safe, path, size = _save_upload(uploaded_file, "rate_sheets")
        sheet = UserRateSheet(
            user_id=current_user.id,
        org_id=current_user.org_id,
            name=f"Travel Rates - {safe}",
            sheet_type="labor_rates",
            file_path=path,
            original_filename=safe,
        )
        db.session.add(sheet)
        db.session.commit()
        _log_activity("rate_sheet_upload", f"Uploaded travel rate schedule: {safe}")
        flash(f"Travel rate schedule '{safe}' uploaded.", "success")
        return redirect(url_for("posture") + "#travel")

    expense_type = request.form.get("expense_type", "").strip()
    description = request.form.get("travel_description", "").strip()
    rate = request.form.get("travel_rate", "0")
    unit = request.form.get("travel_unit", "per day").strip()

    if not expense_type or not rate:
        flash("Expense type and rate are required.", "error")
        return redirect(url_for("posture") + "#travel")

    try:
        rate = float(rate.replace(",", "").replace("$", ""))
    except ValueError:
        flash("Invalid rate format.", "error")
        return redirect(url_for("posture") + "#travel")

    if rate < 0:
        flash("Rate cannot be negative.", "error")
        return redirect(url_for("posture") + "#travel")

    tr = TravelExpenseRate(
        user_id=current_user.id,
        org_id=current_user.org_id,
        expense_type=expense_type,
        description=description,
        rate=rate,
        unit=unit,
    )
    db.session.add(tr)
    db.session.commit()
    _log_activity("travel_rate_add", f"Added travel rate: {expense_type} @ ${rate}/{unit}")
    flash(f"Travel rate '{expense_type}' added.", "success")
    return redirect(url_for("posture") + "#travel")


@app.route("/settings/delete-travel-rate/<rate_id>", methods=["POST"])
@login_required
def delete_travel_rate(rate_id):
    tr = db.session.get(TravelExpenseRate, rate_id)
    if not tr or tr.org_id != current_user.org_id:
        abort(404)
    name = tr.expense_type
    db.session.delete(tr)
    db.session.commit()
    _log_activity("travel_rate_delete", f"Deleted travel rate: {name}")
    flash(f"Travel rate '{name}' deleted.", "success")
    return redirect(url_for("posture") + "#travel")


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@app.route("/projects/new", methods=["GET", "POST"])
@login_required
def new_project():
    if request.method == "GET":
        return render_template("new_project.html", verticals=VERTICALS)

    name = request.form.get("project_name", "").strip()
    client = request.form.get("client_name", "").strip()
    client_email = request.form.get("client_email", "").strip()
    request_type = request.form.get("request_type", "").strip().lower()
    if request_type not in ("rfp", "rfq", "rom", ""):
        request_type = ""
    due_date_raw = request.form.get("due_date", "").strip()
    if not name:
        flash("Project name is required.", "error")
        return redirect(url_for("new_project"))

    ok, msg = billing_check_project(current_user.org_id)
    if not ok:
        flash(msg, "error")
        return redirect(url_for("billing_page"))

    project = Project(
        user_id=current_user.id,
        org_id=current_user.org_id,
        name=name,
        client_name=client,
        client_email=client_email,
        request_type=request_type,
    )

    # Optional due date (YYYY-MM-DD or YYYY-MM-DDTHH:MM)
    if due_date_raw:
        try:
            if "T" in due_date_raw:
                project.due_date = datetime.fromisoformat(due_date_raw)
            else:
                project.due_date = datetime.strptime(due_date_raw, "%Y-%m-%d")
        except ValueError:
            pass  # ignore invalid dates, don't block creation

    db.session.add(project)
    db.session.commit()
    _log_activity("project_create", f"Created project: {name}", project.id)
    return redirect(url_for("project_upload", project_id=project.id))


@app.route("/projects/<project_id>/convert-to-full", methods=["POST"])
@login_required
def convert_rom_to_full(project_id):
    """Promote a ROM project to a full RFP/RFQ so a firm proposal can be drafted."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)
    if (project.request_type or "").lower() != "rom":
        flash("This project is not a ROM.", "error")
        return redirect(url_for("project_upload", project_id=project_id))
    project.request_type = request.form.get("new_type", "rfp").strip().lower()
    if project.request_type not in ("rfp", "rfq"):
        project.request_type = "rfp"
    db.session.commit()
    _log_activity("rom_convert", f"Converted ROM to {project.request_type.upper()}", project_id)
    flash(f"Converted to a full {project.request_type.upper()}. Regenerate to produce a firm proposal.", "success")
    return redirect(url_for("project_upload", project_id=project_id))


@app.route("/projects/<project_id>/set-due-date", methods=["POST"])
@login_required
def set_project_due_date(project_id):
    """Update a project's due date (deadline)."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    due_date_raw = request.form.get("due_date", "").strip()
    if not due_date_raw:
        project.due_date = None
        flash("Due date cleared.", "success")
    else:
        try:
            if "T" in due_date_raw:
                project.due_date = datetime.fromisoformat(due_date_raw)
            else:
                project.due_date = datetime.strptime(due_date_raw, "%Y-%m-%d")
            flash(f"Due date set to {project.due_date.strftime('%Y-%m-%d')}.", "success")
        except ValueError:
            flash("Invalid date format.", "error")
            return redirect(request.referrer or url_for("dashboard"))

    db.session.commit()
    _log_activity("project_due_date_set", f"Due: {project.due_date}", project_id)
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/projects/<project_id>/upload", methods=["GET", "POST"])
@login_required
def project_upload(project_id):
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    if request.method == "POST":
        files = request.files.getlist("documents")
        file_type = request.form.get("file_type", "rfp")

        for file in files:
            if file and _allowed_file(file.filename, ALLOWED_EXTENSIONS | {"xlsx", "xls"}):
                safe, path, size = _save_upload(file, f"projects/{project_id}")
                doc = ProjectDocument(
                    project_id=project_id,
                    filename=f"{uuid.uuid4().hex[:8]}_{safe}",
                    original_filename=safe,
                    file_type=file_type,
                    file_path=path,
                    file_size=size,
                )
                db.session.add(doc)

        db.session.commit()
        _log_activity("document_upload", f"Uploaded {len(files)} document(s)", project_id)
        # Notify proposal users when RFPs are uploaded
        _notify_role(
            "proposal", "rfp_uploaded",
            f"New documents uploaded: {project.name}",
            f"{current_user.display_name or current_user.username} uploaded {len(files)} document(s) to '{project.name}'.",
            link=f"/projects/{project_id}",
            exclude_user_id=current_user.id,
        )
        flash(f"{len(files)} document(s) uploaded.", "success")
        return redirect(url_for("project_upload", project_id=project_id))

    documents = ProjectDocument.query.filter_by(project_id=project_id).order_by(ProjectDocument.uploaded_at.desc()).all()

    # Counts for cost estimation checkboxes
    staff_role_count = StaffRole.query.filter_by(org_id=current_user.org_id, is_active=True).count()
    equipment_count = EquipmentItem.query.filter_by(org_id=current_user.org_id, is_active=True).count()
    travel_rate_count = TravelExpenseRate.query.filter_by(org_id=current_user.org_id, is_active=True).count()

    # Template availability for indicator
    has_user_template = UserVerticalTemplate.query.filter_by(
        org_id=current_user.org_id, is_company_default=False
    ).first() is not None
    has_company_template = UserVerticalTemplate.query.filter_by(
        is_company_default=True, org_id=current_user.org_id
    ).first() is not None

    latest_prop = (
        Proposal.query.filter_by(project_id=project_id)
        .order_by(Proposal.generated_at.desc())
        .first()
    )
    scope = ProjectScope.query.filter_by(project_id=project_id).first()
    scope_included = ScopeItem.query.filter_by(
        scope_id=scope.id, status="included"
    ).count() if scope else 0
    phases = compute_phases(project, latest_prop, doc_count=len(documents),
                            scope_status=scope.status if scope else None)
    pending_questions = ProposalQuestion.query.filter_by(
        project_id=project_id, status="pending"
    ).count()
    open_clarifications = ClarificationItem.query.filter_by(project_id=project_id).filter(
        ClarificationItem.status.in_(["open", "draft", "sent", "response_received"])
    ).count()
    proposal_users = _org_proposal_users()

    return render_template(
        "project_upload.html",
        project=project,
        documents=documents,
        verticals=VERTICALS,
        has_staff_roles=staff_role_count > 0,
        staff_role_count=staff_role_count,
        has_equipment=equipment_count > 0,
        equipment_count=equipment_count,
        has_travel_rates=travel_rate_count > 0,
        travel_rate_count=travel_rate_count,
        has_user_template=has_user_template,
        has_company_template=has_company_template,
        latest_prop=latest_prop,
        phases=phases,
        pending_questions=pending_questions,
        open_clarifications=open_clarifications,
        proposal_users=proposal_users,
        lifecycle_labels=LIFECYCLE_LABELS,
        scope=scope,
        scope_included=scope_included,
    )


@app.route("/projects/<project_id>/generate", methods=["POST"])
@login_required
def project_generate(project_id):
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    # Plan gate (Phase 5): enforce monthly generation limits
    allowed, limit_msg = billing_check_generation(current_user.org_id)
    if not allowed:
        flash(limit_msg, "error")
        return redirect(url_for("project_upload", project_id=project_id))

    # AI cost gate: enforce the monthly token budget so a single org can't run up
    # unbounded pay-per-use LLM spend (a large upload can cost 10-50x a normal run).
    budget_ok, budget_msg = billing_check_ai_budget(current_user.org_id)
    if not budget_ok:
        flash(budget_msg, "error")
        return redirect(url_for("project_upload", project_id=project_id))

    vertical = request.form.get("vertical", "auto")
    output_format = request.form.get("output_format", "docx")
    project.output_format = output_format
    db.session.commit()

    # Validate documents exist before spending a worker
    documents = ProjectDocument.query.filter_by(project_id=project_id).all()
    rfp_docs = [d for d in documents if d.file_type in ("rfp", "supporting")]
    if not rfp_docs:
        flash("No RFP/RFQ documents uploaded yet.", "error")
        return redirect(url_for("project_upload", project_id=project_id))

    cost_options = {
        "include_staff_types": request.form.get("include_staff_types") == "1",
        "include_staff_hours": request.form.get("include_staff_hours") == "1",
        "include_equipment_bom": request.form.get("include_equipment_bom") == "1",
        "include_travel_expenses": request.form.get("include_travel_expenses") == "1",
    }

    # Run the (slow) AI generation as a background job so the request returns
    # immediately and gunicorn workers / load balancers don't time out.
    job = jobs.enqueue(
        "generate_proposal",
        {
            "project_id": project_id,
            "vertical": vertical,
            "output_format": output_format,
            "cost_options": cost_options,
        },
        user_id=current_user.id,
        org_id=current_user.org_id,
    )
    return redirect(url_for("job_status_page", job_id=job.id))


def _perform_generation(project_id, user, vertical, output_format, cost_options, set_progress):
    """Gather posture context and run proposal generation. Returns a dict with
    a 'redirect' key. Runs inside a background job (no request context)."""
    project = db.session.get(Project, project_id)
    if not project:
        raise RuntimeError("Project no longer exists.")

    set_progress("reading", "Reading uploaded documents…")
    documents = ProjectDocument.query.filter_by(project_id=project_id).all()
    rfp_docs = [d for d in documents if d.file_type in ("rfp", "supporting")]
    combined_text = ""
    for doc in rfp_docs:
        try:
            storage.ensure_local(doc.file_path)
            text = parse_document(doc.file_path)
            combined_text += f"\n\n--- Document: {doc.original_filename} ---\n\n{text}"
        except Exception:
            continue
    if not combined_text.strip():
        raise RuntimeError("Could not extract text from the uploaded documents.")

    org_id = user.org_id
    include_staff_types = cost_options.get("include_staff_types")
    include_staff_hours = cost_options.get("include_staff_hours")
    include_equipment_bom = cost_options.get("include_equipment_bom")
    include_travel_expenses = cost_options.get("include_travel_expenses")

    # Build structured rate data from DB entries
    staff_roles_data = None
    if include_staff_types or include_staff_hours:
        roles = StaffRole.query.filter_by(org_id=org_id, is_active=True).all()
        if roles:
            staff_roles_data = [
                {
                    "role_name": r.role_name,
                    "category": r.category,
                    "hourly_rate": r.hourly_rate,
                    "overtime_rate": r.overtime_rate,
                    "description": r.description,
                }
                for r in roles
            ]

    equipment_data = None
    if include_equipment_bom:
        items = EquipmentItem.query.filter_by(org_id=org_id, is_active=True).all()
        if items:
            equipment_data = [
                {
                    "item_name": e.item_name,
                    "category": e.category,
                    "part_number": e.part_number,
                    "manufacturer": e.manufacturer,
                    "unit_cost": e.unit_cost,
                    "unit": e.unit,
                    "description": e.description,
                }
                for e in items
            ]

    travel_data = None
    if include_travel_expenses:
        rates = TravelExpenseRate.query.filter_by(org_id=org_id, is_active=True).all()
        if rates:
            travel_data = [
                {
                    "expense_type": t.expense_type,
                    "rate": t.rate,
                    "unit": t.unit,
                    "description": t.description,
                }
                for t in rates
            ]

    # Load user rate sheets (Excel uploads)
    rate_sheet_data = None
    active_sheets = UserRateSheet.query.filter_by(org_id=org_id, is_active=True).all()
    if active_sheets:
        rate_sheet_data = {}
        for sheet in active_sheets:
            try:
                rate_sheet_data[sheet.sheet_type] = parse_rate_sheet(sheet.file_path)
            except Exception:
                continue

    # Auto-select templates: user custom first, then company defaults as fallback
    user_templates = None
    user_tmpls = UserVerticalTemplate.query.filter_by(
        org_id=org_id, vertical=vertical, is_company_default=False
    ).all()
    if user_tmpls:
        user_templates = {}
        for t in user_tmpls:
            try:
                user_templates[t.template_type] = parse_document(t.file_path)
            except Exception:
                continue

    # Fall back to company defaults for any missing template types
    co_tmpls = UserVerticalTemplate.query.filter_by(
        vertical=vertical, is_company_default=True, org_id=org_id
    ).all()
    if co_tmpls:
        user_templates = user_templates or {}
        for t in co_tmpls:
            if t.template_type not in user_templates:
                try:
                    user_templates[t.template_type] = parse_document(t.file_path)
                except Exception:
                    continue

    # Load past corrections for AI learning
    past_corrections = ProposalCorrection.query.filter_by(
        org_id=org_id
    ).order_by(ProposalCorrection.created_at.desc()).limit(10).all()

    corrections_data = None
    if past_corrections:
        corrections_data = [
            {
                "vertical": c.vertical,
                "summary": c.correction_summary,
                "original": c.original_snippet[:500],
                "corrected": c.corrected_snippet[:500],
                "type": c.correction_type,
            }
            for c in past_corrections
        ]

    # Load company standards for auto-injection
    standards = CompanyStandard.query.filter_by(org_id=org_id, is_active=True).all()
    company_standards_data = None
    if standards:
        company_standards_data = [
            {"category": s.category, "title": s.title, "content": s.content}
            for s in standards
        ]

    # Human-approved Scope of Work guides the draft when present
    approved_scope_items = None
    scope = ProjectScope.query.filter_by(project_id=project_id).first()
    if scope and scope.status == "approved":
        included = ScopeItem.query.filter_by(scope_id=scope.id, status="included").order_by(
            ScopeItem.sort_order, ScopeItem.created_at
        ).all()
        if included:
            approved_scope_items = [i.item_text for i in included]
            if vertical == "auto" and scope.vertical:
                vertical = scope.vertical

    set_progress("drafting", "Claude is drafting your proposal…")
    result = generate_proposal(
        combined_text,
        vertical=vertical,
        rate_sheet_data=rate_sheet_data,
        user_templates=user_templates,
        company_name=user.company_name,
        user_api_key=decrypt_api_key(user.api_key_encrypted) or None,
        user_model=user.llm_model or None,
        cost_options=cost_options,
        staff_roles_data=staff_roles_data,
        equipment_data=equipment_data,
        travel_data=travel_data,
        past_corrections=corrections_data,
        company_standards=company_standards_data,
        approved_scope=approved_scope_items,
        request_type=project.request_type or "",
    )

    billing_record_generation(org_id)

    # If the agent needs clarification, record questions and stop here
    if result.get("questions"):
        set_progress("questions", "Clarification questions detected…")
        for q in result["questions"]:
            db.session.add(ProposalQuestion(
                project_id=project_id,
                question=q["question"],
                context=q.get("context", ""),
                status="pending",
                resolution_path=q.get("resolution_path", "internal"),
            ))
            db.session.add(ClarificationItem(
                project_id=project_id,
                source="ai_detected",
                resolution_path=q.get("resolution_path", "internal"),
                category=q.get("category", "general"),
                question=q["question"],
                context=q.get("context", ""),
                ai_suggestion=q.get("ai_suggestion", ""),
                status="open",
                created_by=user.id,
            ))
        project.clarification_sub_status = "clarification_pending"
        db.session.commit()
        _log_activity_for(user, "proposal_questions",
                          f"{len(result['questions'])} clarification question(s)", project_id)
        return {"redirect": url_for("project_questions", project_id=project_id)}

    set_progress("saving", "Formatting and saving your proposal…")
    job_slug = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    md_filename = f"proposal_{job_slug}.md"
    md_path = GENERATED_DIR / md_filename
    md_path.write_text(result["proposal_markdown"], encoding="utf-8")
    storage.sync_up(md_path)

    docx_filename = f"proposal_{job_slug}.docx"
    docx_path = GENERATED_DIR / docx_filename
    _brand_user = project.owner or user
    markdown_to_docx(
        result["proposal_markdown"],
        str(docx_path),
        **_logo_docx_kwargs(_brand_user),
    )
    storage.sync_up(docx_path)

    project.vertical = result["vertical"]
    project.vertical_label = result["vertical_label"]

    doc_type = "ROM" if (project.request_type or "").lower() == "rom" else result["document_type"]
    proposal = Proposal(
        project_id=project_id,
        job_id=job_slug,
        document_type=doc_type,
        vertical=result["vertical"],
        vertical_label=result["vertical_label"],
        confidence_score=result["confidence_score"],
        action_items_count=len(result["action_items"]),
        md_file=md_filename,
        docx_file=docx_filename,
        pdf_file="",
        review_status="draft",
    )
    db.session.add(proposal)
    db.session.flush()

    db.session.add(ProposalStatusHistory(
        proposal_id=proposal.id,
        from_status="",
        to_status="draft",
        actor_id=user.id,
        note="AI-generated v1.",
    ))
    db.session.add(ProposalVersion(
        proposal_id=proposal.id,
        version_number=1,
        markdown_content=result["proposal_markdown"],
        edit_source="ai",
        change_summary="AI-generated original",
    ))
    db.session.commit()
    _log_activity_for(user, "proposal_generate",
                      f"Generated {result['vertical_label']} proposal", project_id)
    _notify_role_org(
        org_id, "sales", "proposal_generated",
        f"Proposal generated: {project.name}",
        f"{user.display_name or user.username} generated a {result['vertical_label']} proposal for '{project.name}'.",
        link=f"/proposal/{proposal.id}",
        exclude_user_id=user.id,
    )
    if project.user_id != user.id:
        _notify(
            project.user_id, "proposal_generated",
            f"Proposal generated for your project: {project.name}",
            f"{user.display_name or user.username} generated a proposal for '{project.name}'.",
            link=f"/proposal/{proposal.id}",
        )
    _notify_via_integrations(
        org_id,
        f"\U0001F4C4 Proposal generated for *{project.name}* ({result['vertical_label']}).",
        event="proposal_generated",
        payload={"project": project.name, "proposal_id": proposal.id},
    )
    return {"redirect": url_for("view_proposal", proposal_id=proposal.id)}


# ---------------------------------------------------------------------------
# Background job handlers + progress page
# ---------------------------------------------------------------------------

@jobs.register("generate_proposal")
def _job_generate_proposal(payload, job):
    user = db.session.get(User, job.user_id)
    def _sp(phase, message=""):
        jobs.set_progress(job, phase, message)
    return _perform_generation(
        payload["project_id"], user, payload["vertical"],
        payload["output_format"], payload["cost_options"], _sp,
    )


@app.route("/jobs/<job_id>")
@login_required
def job_status_page(job_id):
    job = db.session.get(BackgroundJob, job_id)
    if not job or job.user_id != current_user.id:
        abort(404)
    return render_template("job_status.html", job=job)


@app.route("/jobs/<job_id>/status.json")
@login_required
def job_status_json(job_id):
    job = db.session.get(BackgroundJob, job_id)
    if not job or job.user_id != current_user.id:
        abort(404)
    data = {
        "id": job.id,
        "status": job.status,
        "phase": job.phase,
        "message": job.message,
        "error": job.error,
    }
    if job.status == "done":
        try:
            result = json.loads(job.result or "{}")
        except ValueError:
            result = {}
        data["redirect"] = result.get("redirect", url_for("dashboard"))
    return data


# ---------------------------------------------------------------------------
# Scope of Work (human-approved before generation)
# ---------------------------------------------------------------------------

def _project_rfp_text(project_id: str) -> str:
    """Combined text of the project's RFP/supporting documents."""
    documents = ProjectDocument.query.filter_by(project_id=project_id).all()
    rfp_docs = [d for d in documents if d.file_type in ("rfp", "supporting")]
    combined = ""
    for doc in rfp_docs:
        try:
            text = parse_document(doc.file_path)
            combined += f"\n\n--- Document: {doc.original_filename} ---\n\n{text}"
        except Exception:
            continue
    return combined.strip()


@app.route("/projects/<project_id>/scope")
@login_required
def project_scope(project_id):
    """Review the AI-proposed Scope of Work: accept, remove, or add items."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    scope = ProjectScope.query.filter_by(project_id=project_id).first()
    if not scope:
        flash("No scope drafted yet. Use 'Draft Scope with AI' on the project page.", "error")
        return redirect(url_for("project_upload", project_id=project_id))

    items = ScopeItem.query.filter_by(scope_id=scope.id).order_by(
        ScopeItem.sort_order, ScopeItem.created_at
    ).all()
    included_count = sum(1 for i in items if i.status == "included")

    latest_prop = (
        Proposal.query.filter_by(project_id=project_id)
        .order_by(Proposal.generated_at.desc())
        .first()
    )
    phases = compute_phases(project, latest_prop, scope_status=scope.status)

    return render_template(
        "project_scope.html",
        project=project,
        scope=scope,
        items=items,
        included_count=included_count,
        phases=phases,
        latest_prop=latest_prop,
    )


@app.route("/projects/<project_id>/scope/generate", methods=["POST"])
@login_required
def generate_scope(project_id):
    """AI-draft (or re-draft) the Scope of Work from the uploaded documents."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    combined_text = _project_rfp_text(project_id)
    if not combined_text:
        flash("Upload an RFP/RFQ document before drafting the scope of work.", "error")
        return redirect(url_for("project_upload", project_id=project_id))

    vertical = request.form.get("vertical", "auto")

    try:
        result = draft_scope_of_work(
            combined_text,
            vertical=vertical,
            company_name=current_user.company_name,
            user_api_key=decrypt_api_key(current_user.api_key_encrypted) or None,
            user_model=current_user.llm_model or None,
        )
    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("project_upload", project_id=project_id))
    except Exception as e:
        flash(f"Scope drafting failed: {friendly_api_error(e)}", "error")
        return redirect(url_for("project_upload", project_id=project_id))

    # Replace any existing scope (regeneration starts fresh)
    existing = ProjectScope.query.filter_by(project_id=project_id).first()
    if existing:
        ScopeItem.query.filter_by(scope_id=existing.id).delete()
        db.session.delete(existing)
        db.session.flush()

    scope = ProjectScope(
        project_id=project_id,
        status="draft",
        ai_summary=result["summary"],
        vertical=result["vertical"],
        vertical_label=result["vertical_label"],
    )
    db.session.add(scope)
    db.session.flush()

    for idx, entry in enumerate(result["items"]):
        db.session.add(ScopeItem(
            scope_id=scope.id,
            project_id=project_id,
            item_text=entry["item"],
            category=entry["category"],
            source="ai",
            status="included",
            sort_order=idx,
        ))

    project.vertical = result["vertical"]
    project.vertical_label = result["vertical_label"]
    db.session.commit()
    _log_activity("scope_generate", f"AI drafted {len(result['items'])} scope item(s)", project_id)
    flash(f"AI proposed {len(result['items'])} scope item(s). Review and approve below.", "success")
    return redirect(url_for("project_scope", project_id=project_id))


@app.route("/projects/<project_id>/scope/items/<item_id>/toggle", methods=["POST"])
@login_required
def toggle_scope_item(project_id, item_id):
    """Include/remove a single scope item."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)
    item = db.session.get(ScopeItem, item_id)
    if not item or item.project_id != project_id:
        abort(404)

    item.status = "removed" if item.status == "included" else "included"

    # Any change to an approved scope re-opens it for approval
    scope = db.session.get(ProjectScope, item.scope_id)
    if scope and scope.status == "approved":
        scope.status = "draft"
        flash("Scope modified — re-approve it before generating.", "success")
    db.session.commit()
    return redirect(url_for("project_scope", project_id=project_id))


@app.route("/projects/<project_id>/scope/add", methods=["POST"])
@login_required
def add_scope_item(project_id):
    """Human adds a scope item the AI missed."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)
    scope = ProjectScope.query.filter_by(project_id=project_id).first()
    if not scope:
        abort(404)

    text = request.form.get("item_text", "").strip()
    if not text:
        flash("Scope item text is required.", "error")
        return redirect(url_for("project_scope", project_id=project_id))

    category = request.form.get("category", "general").strip().lower()
    if category not in ("engineering", "installation", "commissioning",
                        "documentation", "management", "general"):
        category = "general"

    max_order = db.session.query(db.func.max(ScopeItem.sort_order)).filter_by(
        scope_id=scope.id
    ).scalar() or 0
    db.session.add(ScopeItem(
        scope_id=scope.id,
        project_id=project_id,
        item_text=text,
        category=category,
        source="human",
        status="included",
        sort_order=max_order + 1,
    ))
    if scope.status == "approved":
        scope.status = "draft"
    db.session.commit()
    _log_activity("scope_item_add", f"Added scope item: {text[:80]}", project_id)
    flash("Scope item added.", "success")
    return redirect(url_for("project_scope", project_id=project_id))


@app.route("/projects/<project_id>/scope/items/<item_id>/delete", methods=["POST"])
@login_required
def delete_scope_item(project_id, item_id):
    """Delete a human-added scope item entirely (AI items are toggled instead)."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)
    item = db.session.get(ScopeItem, item_id)
    if not item or item.project_id != project_id:
        abort(404)
    db.session.delete(item)
    db.session.commit()
    flash("Scope item deleted.", "success")
    return redirect(url_for("project_scope", project_id=project_id))


@app.route("/projects/<project_id>/scope/approve", methods=["POST"])
@login_required
def approve_scope(project_id):
    """Human signs off on the scope; generation will honor it."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)
    scope = ProjectScope.query.filter_by(project_id=project_id).first()
    if not scope:
        abort(404)

    included = ScopeItem.query.filter_by(scope_id=scope.id, status="included").count()
    if included == 0:
        flash("Approve at least one included scope item.", "error")
        return redirect(url_for("project_scope", project_id=project_id))

    scope.status = "approved"
    scope.approved_at = datetime.now(timezone.utc)
    scope.approved_by = current_user.id
    db.session.commit()
    _log_activity("scope_approve", f"Approved scope of work ({included} item(s))", project_id)
    flash(f"Scope of work approved ({included} item(s)). The AI will draft against it.", "success")
    return redirect(url_for("project_upload", project_id=project_id))


@app.route("/projects/<project_id>/scope/reopen", methods=["POST"])
@login_required
def reopen_scope(project_id):
    """Reopen an approved scope for edits."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)
    scope = ProjectScope.query.filter_by(project_id=project_id).first()
    if not scope:
        abort(404)
    scope.status = "draft"
    db.session.commit()
    flash("Scope reopened for edits.", "success")
    return redirect(url_for("project_scope", project_id=project_id))


@app.route("/projects/<project_id>/questions", methods=["GET", "POST"])
@login_required
def project_questions(project_id):
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    pending = ProposalQuestion.query.filter_by(project_id=project_id, status="pending").all()

    if request.method == "POST":
        for q in pending:
            answer = request.form.get(f"answer_{q.id}", "").strip()
            accept_suggestion = request.form.get(f"accept_{q.id}")
            if accept_suggestion and q.resolution_path == "infer":
                # User accepted the AI suggestion — find matching ClarificationItem
                ci = ClarificationItem.query.filter_by(
                    project_id=project_id, question=q.question, source="ai_detected"
                ).first()
                if ci:
                    ci.status = "resolved"
                    ci.response = ci.ai_suggestion
                    ci.responded_at = datetime.now(timezone.utc)
                    ci.responded_by = current_user.id
                q.answer = answer or "(accepted AI suggestion)"
                q.status = "answered"
                q.answered_at = datetime.now(timezone.utc)
            elif answer:
                q.answer = answer
                q.status = "answered"
                q.answered_at = datetime.now(timezone.utc)
                # Update corresponding ClarificationItem
                ci = ClarificationItem.query.filter_by(
                    project_id=project_id, question=q.question, source="ai_detected"
                ).first()
                if ci:
                    ci.status = "response_received"
                    ci.response = answer
                    ci.responded_at = datetime.now(timezone.utc)
                    ci.responded_by = current_user.id
            elif request.form.get(f"skip_{q.id}"):
                q.status = "skipped"
                ci = ClarificationItem.query.filter_by(
                    project_id=project_id, question=q.question, source="ai_detected"
                ).first()
                if ci:
                    ci.status = "skipped"
            elif request.form.get(f"send_to_customer_{q.id}"):
                # Mark as needing customer response — keep pending but tag for RFI
                ci = ClarificationItem.query.filter_by(
                    project_id=project_id, question=q.question, source="ai_detected"
                ).first()
                if ci:
                    ci.resolution_path = "customer"
                    ci.status = "open"
                q.status = "skipped"  # Skip for now, will be handled via RFI
        db.session.commit()

        # Check if there are still pending questions
        remaining = ProposalQuestion.query.filter_by(project_id=project_id, status="pending").count()
        if remaining == 0:
            project.clarification_sub_status = "none"
            db.session.commit()
            flash("All questions answered. You can now regenerate the proposal.", "success")
            return redirect(url_for("project_upload", project_id=project_id))

        return redirect(url_for("project_questions", project_id=project_id))

    # Group questions by resolution path for display
    infer_qs = [q for q in pending if q.resolution_path == "infer"]
    internal_qs = [q for q in pending if q.resolution_path == "internal"]
    customer_qs = [q for q in pending if q.resolution_path == "customer"]

    # Get AI suggestions for infer items from ClarificationItems
    ai_suggestions = {}
    for q in infer_qs:
        ci = ClarificationItem.query.filter_by(
            project_id=project_id, question=q.question, source="ai_detected"
        ).first()
        if ci and ci.ai_suggestion:
            ai_suggestions[q.id] = ci.ai_suggestion

    return render_template(
        "project_questions.html",
        project=project,
        questions=pending,
        infer_qs=infer_qs,
        internal_qs=internal_qs,
        customer_qs=customer_qs,
        ai_suggestions=ai_suggestions,
    )


@app.route("/projects/<project_id>/update-status", methods=["POST"])
@login_required
def update_project_status(project_id):
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    new_status = request.form.get("status", project.status)
    dollar_amount = request.form.get("dollar_amount")

    prev_status = project.status

    # Require a win/loss reason category when closing a deal (retrospective quality)
    if new_status in ("won", "lost") and prev_status not in ("won", "lost"):
        close_category = request.form.get("close_category", "").strip()
        if not close_category:
            flash("Please select a win/loss reason category before closing this deal.", "error")
            return redirect(request.referrer or url_for("dashboard"))

    project.status = new_status
    if dollar_amount:
        try:
            project.dollar_amount = float(dollar_amount.replace(",", "").replace("$", ""))
        except ValueError:
            pass

    if new_status == "submitted":
        project.submitted_at = datetime.now(timezone.utc)

    # Capture win/loss analysis when closing
    if new_status in ("won", "lost"):
        close_reason = request.form.get("close_reason", "").strip()
        close_category = request.form.get("close_category", "").strip()
        competitor_name = request.form.get("competitor_name", "").strip()
        if close_reason:
            project.close_reason = close_reason
        if close_category:
            project.close_category = close_category
        if competitor_name:
            project.competitor_name = competitor_name
        if prev_status not in ("won", "lost"):
            project.closed_at = datetime.now(timezone.utc)

    db.session.commit()
    _log_activity("project_status_update", f"Status → {new_status}", project_id)
    if new_status in ("won", "lost") and prev_status not in ("won", "lost"):
        emoji = "\U0001F3C6" if new_status == "won" else "\U0001F4C9"
        _notify_via_integrations(
            project.org_id,
            f"{emoji} *{project.name}* marked **{new_status.upper()}**"
            + (f" (${project.dollar_amount:,.0f})" if project.dollar_amount else ""),
            event=f"project_{new_status}",
            payload={"project": project.name, "value": project.dollar_amount,
                     "category": project.close_category},
        )
    flash(f"Project status updated to {new_status}.", "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/projects/<project_id>/close-details", methods=["POST"])
@login_required
def update_close_details(project_id):
    """Update win/loss reason, category, and competitor for a closed project."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)
    if project.status not in ("won", "lost"):
        flash("Close details can only be set on won/lost projects.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    project.close_reason = request.form.get("close_reason", "").strip()
    project.close_category = request.form.get("close_category", "").strip()
    project.competitor_name = request.form.get("competitor_name", "").strip()
    dollar_amount = request.form.get("dollar_amount", "").strip()
    if dollar_amount:
        try:
            project.dollar_amount = float(dollar_amount.replace(",", "").replace("$", ""))
        except ValueError:
            pass
    if not project.closed_at:
        project.closed_at = datetime.now(timezone.utc)
    db.session.commit()
    _log_activity("close_details_update", f"Close details updated ({project.status})", project_id)
    flash("Close details saved.", "success")
    return redirect(request.referrer or url_for("reports"))


# ---------------------------------------------------------------------------
# Proposal view & download
# ---------------------------------------------------------------------------

@app.route("/proposal/<proposal_id>")
@login_required
def view_proposal(proposal_id):
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        flash("Proposal not found.", "error")
        return redirect(url_for("dashboard"))

    if not _can_view_proposal(proposal):
        abort(404)
    project = db.session.get(Project, proposal.project_id)

    md_path = GENERATED_DIR / proposal.md_file
    if not md_path.exists():
        flash("Proposal file not found.", "error")
        return redirect(url_for("dashboard"))

    proposal_md = md_path.read_text(encoding="utf-8")
    action_items = re.findall(r"\[ACTION REQUIRED:\s*(.+?)\]", proposal_md)

    # Version history for the viewer dropdown
    versions = ProposalVersion.query.filter_by(proposal_id=proposal_id).order_by(
        ProposalVersion.version_number.desc()
    ).all()

    # Clean vs Redlines view. Redline = inline word diff of AI original (v1)
    # against the current latest version.
    view_mode = request.args.get("view", "clean")
    redline_available = len(versions) > 1
    if view_mode == "redline" and redline_available:
        try:
            v1 = versions[-1]
            latest_v = versions[0]
            proposal_md_render = _inline_redline_markdown(
                v1.markdown_content, latest_v.markdown_content
            )
        except Exception:
            view_mode = "clean"
            proposal_md_render = proposal_md
    else:
        view_mode = "clean"
        proposal_md_render = proposal_md

    proposal_html = htmlsafe.sanitize(md.markdown(proposal_md_render, extensions=["tables", "fenced_code"]))

    meta = {
        "source_file": project.name,
        "document_type": proposal.document_type,
        "vertical_label": proposal.vertical_label,
        "confidence_score": proposal.confidence_score,
        "generated_at": proposal.generated_at.isoformat() if proposal.generated_at else "",
    }

    # Load comments — open first, then resolved
    comments = ProposalComment.query.filter_by(proposal_id=proposal_id).order_by(
        ProposalComment.is_resolved.asc(), ProposalComment.created_at.desc()
    ).all()
    open_comment_count = sum(1 for c in comments if not c.is_resolved)

    # Review lifecycle context
    state = approval_state(proposal)
    reviewers = ProposalReviewer.query.filter_by(proposal_id=proposal_id).order_by(
        ProposalReviewer.assigned_at
    ).all()
    pending_req_count = RevisionRequest.query.filter_by(
        proposal_id=proposal_id, status="pending"
    ).count()
    is_owner = _is_proposal_owner(proposal)
    my_reviewer = _get_reviewer(proposal_id, current_user.id)
    status_history = ProposalStatusHistory.query.filter_by(
        proposal_id=proposal_id
    ).order_by(ProposalStatusHistory.created_at.desc()).limit(10).all()

    phases = compute_phases(project, proposal)

    # Customer share (Phase 3)
    share = _active_share(proposal_id)
    share_url = url_for("customer_portal", token=share.token, _external=True) if share else ""

    return render_template(
        "proposal.html",
        meta=meta,
        proposal_html=proposal_html,
        action_items=action_items,
        proposal=proposal,
        project=project,
        comments=comments,
        open_comment_count=open_comment_count,
        state=state,
        reviewers=reviewers,
        pending_req_count=pending_req_count,
        is_owner=is_owner,
        my_reviewer=my_reviewer,
        status_history=status_history,
        lifecycle_labels=LIFECYCLE_LABELS,
        phases=phases,
        versions=versions,
        view_mode=view_mode,
        redline_available=redline_available,
        share=share,
        share_url=share_url,
    )


@app.route("/download/<proposal_id>/<fmt>")
@login_required
def download(proposal_id, fmt):
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        flash("Proposal not found.", "error")
        return redirect(url_for("dashboard"))

    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    if fmt == "docx":
        file_path = GENERATED_DIR / proposal.docx_file
        mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif fmt == "md":
        file_path = GENERATED_DIR / proposal.md_file
        mimetype = "text/markdown"
    elif fmt == "pdf":
        try:
            file_path = _ensure_proposal_pdf(proposal, project)
        except Exception as e:
            flash(f"Could not generate PDF: {e}", "error")
            return redirect(url_for("view_proposal", proposal_id=proposal_id))
        mimetype = "application/pdf"
    else:
        flash("Invalid format.", "error")
        return redirect(url_for("view_proposal", proposal_id=proposal_id))

    return _send_stored_file(file_path, mimetype=mimetype, as_attachment=True, download_name=file_path.name)


def _ensure_proposal_pdf(proposal, project):
    """Generate (or regenerate) the proposal PDF from its latest markdown and
    return the local path. Branding uses the project owner's logo/company."""
    latest = latest_version(proposal.id)
    md_text = latest.markdown_content if latest else ""
    if not md_text:
        storage.ensure_local(GENERATED_DIR / proposal.md_file)
        md_path = GENERATED_DIR / proposal.md_file
        md_text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""

    owner = project.owner or db.session.get(User, project.user_id)
    pdf_filename = proposal.pdf_file or f"proposal_{proposal.job_id}.pdf"
    pdf_path = GENERATED_DIR / pdf_filename

    logo_path = None
    company_name = ""
    if owner:
        company_name = owner.company_name or ""
        if (owner.company_logo_path and getattr(owner, "company_logo_use_in_proposals", False)
                and Path(owner.company_logo_path).exists()):
            logo_path = owner.company_logo_path

    markdown_to_pdf(md_text, str(pdf_path), logo_path=logo_path, company_name=company_name)
    storage.sync_up(pdf_path)
    if proposal.pdf_file != pdf_filename:
        proposal.pdf_file = pdf_filename
        db.session.commit()
    return pdf_path


# ---------------------------------------------------------------------------
# Structured pricing estimate (Phase 4)
# ---------------------------------------------------------------------------

def _estimate_totals(estimate):
    """Compute subtotal by kind, markup, and grand total for an estimate."""
    items = estimate.items.order_by(EstimateLineItem.kind, EstimateLineItem.sort_order).all()
    by_kind = {}
    subtotal = 0.0
    for it in items:
        t = it.total
        by_kind[it.kind] = by_kind.get(it.kind, 0.0) + t
        subtotal += t
    markup = subtotal * (estimate.markup_pct or 0) / 100.0
    return {
        "items": items,
        "by_kind": by_kind,
        "subtotal": subtotal,
        "markup": markup,
        "grand_total": subtotal + markup,
    }


@app.route("/proposal/<proposal_id>/estimate", methods=["GET"])
@login_required
def proposal_estimate(proposal_id):
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    estimate = ProposalEstimate.query.filter_by(proposal_id=proposal_id).first()
    totals = _estimate_totals(estimate) if estimate else None
    return render_template(
        "proposal_estimate.html",
        proposal=proposal, project=project,
        estimate=estimate, totals=totals,
    )


@app.route("/proposal/<proposal_id>/estimate/draft", methods=["POST"])
@login_required
def draft_proposal_estimate(proposal_id):
    """AI-draft a structured estimate from the RFP + the org's rates."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    org_id = current_user.org_id
    staff = [{"role_name": r.role_name, "category": r.category, "hourly_rate": r.hourly_rate,
              "overtime_rate": r.overtime_rate}
             for r in StaffRole.query.filter_by(org_id=org_id, is_active=True).all()]
    equip = [{"item_name": e.item_name, "category": e.category, "unit_cost": e.unit_cost, "unit": e.unit}
             for e in EquipmentItem.query.filter_by(org_id=org_id, is_active=True).all()]
    travel = [{"expense_type": t.expense_type, "rate": t.rate, "unit": t.unit}
              for t in TravelExpenseRate.query.filter_by(org_id=org_id, is_active=True).all()]

    scope_items = None
    scope = ProjectScope.query.filter_by(project_id=project.id).first()
    if scope:
        scope_items = [i.item_text for i in ScopeItem.query.filter_by(
            scope_id=scope.id, status="included").all()]

    rfp_text = _project_rfp_text(project.id)
    try:
        result = draft_estimate(
            rfp_text, staff_roles=staff, equipment=equip, travel=travel,
            approved_scope=scope_items,
            user_api_key=decrypt_api_key(current_user.api_key_encrypted) or None,
            user_model=current_user.llm_model or None,
        )
    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("proposal_estimate", proposal_id=proposal_id))
    except Exception as e:
        flash(f"Estimate drafting failed: {friendly_api_error(e)}", "error")
        return redirect(url_for("proposal_estimate", proposal_id=proposal_id))

    existing = ProposalEstimate.query.filter_by(proposal_id=proposal_id).first()
    if existing:
        db.session.delete(existing)
        db.session.flush()
    estimate = ProposalEstimate(
        proposal_id=proposal_id, project_id=project.id, org_id=org_id,
        currency=result.get("currency", "USD"),
    )
    db.session.add(estimate)
    db.session.flush()
    for idx, it in enumerate(result["items"]):
        db.session.add(EstimateLineItem(
            estimate_id=estimate.id, kind=it["kind"], description=it["description"],
            quantity=it["quantity"], unit=it["unit"], unit_cost=it["unit_cost"], sort_order=idx,
        ))
    db.session.commit()
    _log_activity("estimate_draft", f"AI drafted {len(result['items'])} estimate line(s)", project.id)
    flash(f"AI drafted {len(result['items'])} line item(s). Review and adjust below.", "success")
    return redirect(url_for("proposal_estimate", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/estimate/save", methods=["POST"])
@login_required
def save_proposal_estimate(proposal_id):
    """Persist grid edits: line items (add/edit/delete) + markup."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    estimate = ProposalEstimate.query.filter_by(proposal_id=proposal_id).first()
    if not estimate:
        estimate = ProposalEstimate(proposal_id=proposal_id, project_id=project.id, org_id=current_user.org_id)
        db.session.add(estimate)
        db.session.flush()

    try:
        estimate.markup_pct = float(request.form.get("markup_pct", estimate.markup_pct) or 0)
    except ValueError:
        pass
    estimate.currency = (request.form.get("currency", estimate.currency) or "USD")[:10]

    EstimateLineItem.query.filter_by(estimate_id=estimate.id).delete()
    kinds = request.form.getlist("kind")
    descs = request.form.getlist("description")
    qtys = request.form.getlist("quantity")
    units = request.form.getlist("unit")
    costs = request.form.getlist("unit_cost")
    for idx in range(len(descs)):
        desc = (descs[idx] or "").strip()
        if not desc:
            continue
        kind = (kinds[idx] if idx < len(kinds) else "other").lower()
        if kind not in ("labor", "equipment", "travel", "other"):
            kind = "other"
        def _num(lst, i):
            try:
                return float((lst[i] if i < len(lst) else "0").replace(",", "").replace("$", "") or 0)
            except ValueError:
                return 0.0
        db.session.add(EstimateLineItem(
            estimate_id=estimate.id, kind=kind, description=desc[:400],
            quantity=_num(qtys, idx), unit=(units[idx] if idx < len(units) else "")[:40],
            unit_cost=_num(costs, idx), sort_order=idx,
        ))
    db.session.commit()
    _log_activity("estimate_save", "Saved structured estimate", project.id)
    flash("Estimate saved.", "success")
    return redirect(url_for("proposal_estimate", proposal_id=proposal_id))


def _estimate_markdown(estimate) -> str:
    """Render an estimate as a Markdown Pricing section."""
    totals = _estimate_totals(estimate)
    cur = estimate.currency or "USD"
    sym = "$" if cur == "USD" else ""
    lines = ["## Pricing", ""]
    kind_labels = {"labor": "Labor", "equipment": "Equipment & Materials",
                   "travel": "Travel & Expenses", "other": "Other"}
    for kind in ("labor", "equipment", "travel", "other"):
        rows = [it for it in totals["items"] if it.kind == kind]
        if not rows:
            continue
        lines.append(f"### {kind_labels[kind]}")
        lines.append("")
        lines.append("| Item | Qty | Unit | Unit Cost | Total |")
        lines.append("|------|-----|------|-----------|-------|")
        for it in rows:
            lines.append(f"| {it.description} | {it.quantity:g} | {it.unit} | "
                         f"{sym}{it.unit_cost:,.2f} | {sym}{it.total:,.2f} |")
        lines.append(f"| **{kind_labels[kind]} subtotal** | | | | **{sym}{totals['by_kind'][kind]:,.2f}** |")
        lines.append("")
    lines.append("### Total")
    lines.append("")
    lines.append("| | Amount |")
    lines.append("|--|--------|")
    lines.append(f"| Subtotal | {sym}{totals['subtotal']:,.2f} |")
    if estimate.markup_pct:
        lines.append(f"| Markup ({estimate.markup_pct:g}%) | {sym}{totals['markup']:,.2f} |")
    lines.append(f"| **Total Estimated Cost** | **{sym}{totals['grand_total']:,.2f}** |")
    lines.append("")
    return "\n".join(lines)


@app.route("/proposal/<proposal_id>/estimate/export.csv")
@login_required
def export_estimate_csv(proposal_id):
    import csv
    import io
    from flask import Response
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)
    estimate = ProposalEstimate.query.filter_by(proposal_id=proposal_id).first()
    if not estimate:
        flash("No estimate to export.", "error")
        return redirect(url_for("proposal_estimate", proposal_id=proposal_id))
    totals = _estimate_totals(estimate)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Kind", "Description", "Quantity", "Unit", "Unit Cost", "Total"])
    for it in totals["items"]:
        w.writerow([it.kind, it.description, it.quantity, it.unit, f"{it.unit_cost:.2f}", f"{it.total:.2f}"])
    w.writerow([])
    w.writerow(["", "", "", "", "Subtotal", f"{totals['subtotal']:.2f}"])
    w.writerow(["", "", "", "", f"Markup {estimate.markup_pct:g}%", f"{totals['markup']:.2f}"])
    w.writerow(["", "", "", "", "Grand Total", f"{totals['grand_total']:.2f}"])
    out.seek(0)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=estimate_{proposal.job_id}.csv"})


@app.route("/proposal/<proposal_id>/estimate/insert", methods=["POST"])
@login_required
def insert_estimate_into_proposal(proposal_id):
    """Insert/replace the proposal's Pricing section from the structured estimate,
    saving a new version."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)
    estimate = ProposalEstimate.query.filter_by(proposal_id=proposal_id).first()
    if not estimate or not estimate.items.count():
        flash("Add estimate line items first.", "error")
        return redirect(url_for("proposal_estimate", proposal_id=proposal_id))

    version = latest_version(proposal_id)
    current_md = version.markdown_content if version else ""
    pricing_md = _estimate_markdown(estimate)

    if re.search(r"^##\s+Pricing\s*$", current_md, re.MULTILINE):
        updated_md = re.sub(r"##\s+Pricing.*?(?=\n##\s|\Z)", pricing_md + "\n", current_md,
                            count=1, flags=re.DOTALL)
    else:
        updated_md = current_md.rstrip() + "\n\n" + pricing_md

    next_version = (version.version_number + 1) if version else 1
    db.session.add(ProposalVersion(
        proposal_id=proposal_id, version_number=next_version, markdown_content=updated_md,
        edit_source="human_web", editor_id=current_user.id,
        change_summary="Inserted structured pricing estimate",
    ))
    md_path = GENERATED_DIR / proposal.md_file
    md_path.write_text(updated_md, encoding="utf-8")
    storage.sync_up(md_path)
    if proposal.docx_file:
        docx_path = GENERATED_DIR / proposal.docx_file
        _brand_user = project.owner or current_user
        markdown_to_docx(updated_md, str(docx_path), **_logo_docx_kwargs(_brand_user))
        storage.sync_up(docx_path)
    project.dollar_amount = _estimate_totals(estimate)["grand_total"]
    db.session.commit()
    _log_activity("estimate_insert", f"Inserted pricing into proposal v{next_version}", project.id)
    flash(f"Pricing inserted as v{next_version}.", "success")
    return redirect(url_for("view_proposal", proposal_id=proposal_id))


# ---------------------------------------------------------------------------
# Proposal Editor & Version Control
# ---------------------------------------------------------------------------

@app.route("/proposal/<proposal_id>/edit", methods=["GET", "POST"])
@login_required
def edit_proposal(proposal_id):
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    if request.method == "POST":
        new_content = request.form.get("markdown_content", "")
        change_summary = request.form.get("change_summary", "").strip() or "Manual edit"

        if not new_content.strip():
            flash("Proposal content cannot be empty.", "error")
            return redirect(url_for("edit_proposal", proposal_id=proposal_id))

        # Get current version number
        latest = ProposalVersion.query.filter_by(proposal_id=proposal_id).order_by(
            ProposalVersion.version_number.desc()
        ).first()
        next_version = (latest.version_number + 1) if latest else 1

        # Save new version
        version = ProposalVersion(
            proposal_id=proposal_id,
            version_number=next_version,
            markdown_content=new_content,
            edit_source="human_web",
            editor_id=current_user.id,
            change_summary=change_summary,
        )
        db.session.add(version)

        # Update the markdown file on disk
        md_path = GENERATED_DIR / proposal.md_file
        md_path.write_text(new_content, encoding="utf-8")

        # Regenerate DOCX from new content
        if proposal.docx_file:
            docx_path = GENERATED_DIR / proposal.docx_file
            _brand_user = project.owner or current_user
            markdown_to_docx(
                new_content,
                str(docx_path),
                **_logo_docx_kwargs(_brand_user),
            )

        # Update action items count
        action_items = re.findall(r"\[ACTION REQUIRED:\s*(.+?)\]", new_content)
        proposal.action_items_count = len(action_items)

        db.session.commit()
        _log_activity("proposal_edit", f"Edited proposal v{next_version}: {change_summary}", project.id)
        flash(f"Proposal saved as version {next_version}.", "success")
        return redirect(url_for("edit_proposal", proposal_id=proposal_id))

    # Load current content
    md_path = GENERATED_DIR / proposal.md_file
    current_content = md_path.read_text(encoding="utf-8") if md_path.exists() else ""

    # Load version history
    versions = ProposalVersion.query.filter_by(proposal_id=proposal_id).order_by(
        ProposalVersion.version_number.desc()
    ).all()

    return render_template(
        "proposal_edit.html",
        proposal=proposal,
        project=project,
        current_content=current_content,
        versions=versions,
    )


@app.route("/proposal/<proposal_id>/preview", methods=["POST"])
@login_required
def preview_markdown(proposal_id):
    """Render posted markdown to HTML for the live editor preview pane."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)
    data = request.get_json(silent=True) or {}
    content = data.get("markdown", "")
    html = md.markdown(content, extensions=["tables", "fenced_code"])
    return {"html": html}


@app.route("/proposal/<proposal_id>/ai-assist", methods=["POST"])
@login_required
def ai_assist(proposal_id):
    """Rewrite a selected passage with a quick instruction (tighten, formalize,
    expand). Returns {"result": text}. Used inline from the editor."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    data = request.get_json(silent=True) or {}
    text = (data.get("text", "") or "").strip()
    action = (data.get("action", "") or "").strip().lower()
    if not text:
        return {"error": "Select some text first."}, 400

    instructions = {
        "tighten": "Rewrite this passage to be more concise and punchy without losing meaning.",
        "formal": "Rewrite this passage in a more formal, professional business tone.",
        "expand": "Expand this passage with more specific, persuasive detail. Do not invent facts, pricing, names, or certifications — use [ACTION REQUIRED: ...] for anything unknown.",
        "grammar": "Fix grammar, spelling, and punctuation in this passage. Keep the meaning and tone.",
    }
    instr = instructions.get(action, instructions["tighten"])

    api_key = decrypt_api_key(current_user.api_key_encrypted) or None
    from config.settings import ANTHROPIC_API_KEY as _GK
    key = api_key or _GK
    if not key:
        return {"error": "No API key configured. Add one in Settings."}, 400
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key, timeout=45, max_retries=1)
        resp = client.messages.create(
            model=current_user.llm_model or "claude-opus-4-6",
            max_tokens=1500,
            system=("You are a proposal editor. Return ONLY the rewritten passage in "
                    "Markdown — no preamble, no explanation, no code fences."),
            messages=[{"role": "user", "content": f"{instr}\n\n---PASSAGE---\n{text[:6000]}"}],
        )
        out = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        return {"result": out or text}
    except Exception as e:
        return {"error": friendly_api_error(e)}, 500


@app.route("/proposal/<proposal_id>/version/<version_id>")
@login_required
def view_version(proposal_id, version_id):
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    version = db.session.get(ProposalVersion, version_id)
    if not version or version.proposal_id != proposal_id:
        abort(404)

    proposal_html = htmlsafe.sanitize(md.markdown(version.markdown_content, extensions=["tables", "fenced_code"]))

    return render_template(
        "proposal_version.html",
        proposal=proposal,
        project=project,
        version=version,
        proposal_html=proposal_html,
    )


@app.route("/proposal/<proposal_id>/restore/<version_id>", methods=["POST"])
@login_required
def restore_version(proposal_id, version_id):
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    version = db.session.get(ProposalVersion, version_id)
    if not version or version.proposal_id != proposal_id:
        abort(404)

    # Get next version number
    latest = ProposalVersion.query.filter_by(proposal_id=proposal_id).order_by(
        ProposalVersion.version_number.desc()
    ).first()
    next_version = (latest.version_number + 1) if latest else 1

    # Create new version from restored content
    restored = ProposalVersion(
        proposal_id=proposal_id,
        version_number=next_version,
        markdown_content=version.markdown_content,
        edit_source="human_web",
        editor_id=current_user.id,
        change_summary=f"Restored from version {version.version_number}",
    )
    db.session.add(restored)

    # Update file on disk
    md_path = GENERATED_DIR / proposal.md_file
    md_path.write_text(version.markdown_content, encoding="utf-8")

    if proposal.docx_file:
        docx_path = GENERATED_DIR / proposal.docx_file
        _brand_user = project.owner or current_user
        markdown_to_docx(
            version.markdown_content,
            str(docx_path),
            **_logo_docx_kwargs(_brand_user),
        )

    db.session.commit()
    _log_activity("proposal_restore", f"Restored proposal to v{version.version_number}", project.id)
    flash(f"Restored to version {version.version_number} (saved as v{next_version}).", "success")
    return redirect(url_for("edit_proposal", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/redline")
@login_required
def download_redline(proposal_id):
    """Download a DOCX with tracked changes comparing AI original to current version."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    # Get the AI original (v1) and latest version
    v1 = ProposalVersion.query.filter_by(proposal_id=proposal_id, version_number=1).first()
    latest = ProposalVersion.query.filter_by(proposal_id=proposal_id).order_by(
        ProposalVersion.version_number.desc()
    ).first()

    if not v1 or not latest or v1.id == latest.id:
        flash("No changes to compare — only one version exists.", "error")
        return redirect(url_for("view_proposal", proposal_id=proposal_id))

    # Compare two specific versions if requested via query params
    compare_from = request.args.get("from")
    compare_to = request.args.get("to")
    if compare_from and compare_to:
        v_from = db.session.get(ProposalVersion, compare_from)
        v_to = db.session.get(ProposalVersion, compare_to)
        if v_from and v_to and v_from.proposal_id == proposal_id and v_to.proposal_id == proposal_id:
            v1 = v_from
            latest = v_to

    redline_filename = f"redline_{proposal.job_id}_v{v1.version_number}_to_v{latest.version_number}.docx"
    redline_path = GENERATED_DIR / redline_filename

    author = current_user.display_name or current_user.username
    markdown_to_redline_docx(v1.markdown_content, latest.markdown_content, str(redline_path), author=author)

    return send_file(
        str(redline_path),
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=redline_filename,
    )


@app.route("/proposal/<proposal_id>/finalize", methods=["POST"])
@login_required
def finalize_proposal(proposal_id):
    """Mark proposal as finalized and capture AI learning corrections."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    # Get AI original (v1) and latest human version
    v1 = ProposalVersion.query.filter_by(proposal_id=proposal_id, version_number=1).first()
    latest = ProposalVersion.query.filter_by(proposal_id=proposal_id).order_by(
        ProposalVersion.version_number.desc()
    ).first()

    if v1 and latest and v1.id != latest.id:
        # Generate correction summary from diff
        orig_lines = v1.markdown_content.splitlines()
        new_lines = latest.markdown_content.splitlines()
        diff = list(difflib.unified_diff(orig_lines, new_lines, lineterm=""))

        if diff:
            # Build a human-readable correction summary
            added = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
            removed = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---")]

            # Only create correction if there are meaningful changes
            if added or removed:
                summary_parts = []
                if removed:
                    summary_parts.append(f"Removed/changed {len(removed)} line(s)")
                if added:
                    summary_parts.append(f"Added/modified {len(added)} line(s)")

                correction = ProposalCorrection(
                    user_id=current_user.id,
                    org_id=current_user.org_id,
                    proposal_id=proposal_id,
                    vertical=proposal.vertical,
                    correction_summary="; ".join(summary_parts),
                    original_snippet="\n".join(removed[:50])[:3000] if removed else "",
                    corrected_snippet="\n".join(added[:50])[:3000] if added else "",
                    correction_type="general",
                )
                db.session.add(correction)

    db.session.commit()
    _log_activity("proposal_finalize", f"Finalized proposal with corrections", project.id)
    flash("Proposal finalized. AI will learn from your edits for future proposals.", "success")
    return redirect(url_for("view_proposal", proposal_id=proposal_id))


# ---------------------------------------------------------------------------
# Part 3: Multi-Stakeholder Review & Revision Workflow
# ---------------------------------------------------------------------------

REVIEW_ROLE_OPTIONS = [
    ("engineering", "Engineering"),
    ("accounting", "Accounting"),
    ("sales", "Sales"),
    ("legal", "Legal"),
    ("operations", "Operations"),
    ("other", "Other"),
]

REVISION_CATEGORIES = [
    ("pricing", "Pricing"),
    ("scope", "Scope"),
    ("resources", "Resources"),
    ("schedule", "Schedule"),
    ("terms", "Terms"),
    ("compliance", "Compliance"),
    ("tone", "Tone"),
    ("structure", "Structure"),
    ("other", "Other"),
]

REVISION_SOURCES = [
    "internal_engineering", "internal_accounting", "internal_sales",
    "internal_legal", "internal_operations", "internal_other",
    "customer", "other",
]


def _proposal_owner(proposal: Proposal) -> User:
    project = db.session.get(Project, proposal.project_id)
    return db.session.get(User, project.user_id) if project else None


def _can_view_proposal(proposal: Proposal) -> bool:
    """The proposal owner, the assigned user, any admin, OR any assigned reviewer
    can view the proposal."""
    if not proposal:
        return False
    project = db.session.get(Project, proposal.project_id)
    if not project:
        return False
    if project.user_id == current_user.id or project.assigned_to == current_user.id:
        return True
    # Admins can view — but only within their OWN organization. `is_admin` is a
    # tenant admin, not a platform super-admin, so it must be org-scoped or any
    # workspace owner could read every other tenant's proposals.
    if current_user.is_admin and _same_org(project.org_id or _owner_org_id(project)):
        return True
    # Assigned reviewer?
    reviewer = ProposalReviewer.query.filter_by(
        proposal_id=proposal.id, user_id=current_user.id
    ).first()
    return reviewer is not None


def _is_proposal_owner(proposal: Proposal) -> bool:
    """Only the project owner, its assignee, or an admin is considered the
    proposal 'owner' for workflow-control purposes."""
    if not proposal:
        return False
    project = db.session.get(Project, proposal.project_id)
    if not project:
        return False
    if project.user_id == current_user.id or project.assigned_to == current_user.id:
        return True
    # Org-scoped admin only — never a cross-tenant super-admin (see _can_view_proposal).
    return bool(current_user.is_admin and _same_org(project.org_id or _owner_org_id(project)))


def _source_for_review_role(review_role: str) -> str:
    mapping = {
        "engineering": "internal_engineering",
        "accounting": "internal_accounting",
        "sales": "internal_sales",
        "legal": "internal_legal",
        "operations": "internal_operations",
        "other": "internal_other",
    }
    return mapping.get(review_role, "internal_other")


def _get_reviewer(proposal_id: str, user_id: str) -> ProposalReviewer | None:
    return ProposalReviewer.query.filter_by(
        proposal_id=proposal_id, user_id=user_id
    ).first()


@app.route("/proposal/<proposal_id>/send-for-review", methods=["GET", "POST"])
@login_required
def send_for_review(proposal_id):
    """Assign reviewers and transition a proposal from draft to in_review."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal or not _is_proposal_owner(proposal):
        abort(404)
    project = db.session.get(Project, proposal.project_id)

    if request.method == "GET":
        all_users = _org_users_query().order_by(User.display_name).all()
        existing_reviewers = ProposalReviewer.query.filter_by(proposal_id=proposal_id).all()
        return render_template(
            "proposal_send_review.html",
            proposal=proposal,
            project=project,
            users=all_users,
            existing_reviewers=existing_reviewers,
            review_roles=REVIEW_ROLE_OPTIONS,
            lifecycle_labels=LIFECYCLE_LABELS,
        )

    # POST — parse reviewer assignments
    # Form inputs: reviewer_user_id[], reviewer_role[], reviewer_required[]
    user_ids = request.form.getlist("reviewer_user_id")
    roles = request.form.getlist("reviewer_role")
    required_flags = request.form.getlist("reviewer_required")
    note = request.form.get("review_note", "").strip()
    deadline_str = request.form.get("review_deadline", "").strip()

    if not user_ids:
        flash("Please select at least one reviewer.", "error")
        return redirect(url_for("send_for_review", proposal_id=proposal_id))

    # Enforce "owner cannot be sole approver" rule: at least one reviewer must be
    # someone other than the proposal owner (see plan §8).
    owner_id = project.user_id
    non_owner_count = sum(1 for uid in user_ids if uid and uid != owner_id)
    if non_owner_count == 0:
        flash("At least one reviewer must be someone other than the proposal owner.", "error")
        return redirect(url_for("send_for_review", proposal_id=proposal_id))

    deadline = None
    if deadline_str:
        try:
            deadline = datetime.fromisoformat(deadline_str).replace(tzinfo=timezone.utc)
        except ValueError:
            deadline = None

    # Clear old reviewer rows that aren't in the new set and add new ones.
    ProposalReviewer.query.filter_by(proposal_id=proposal_id).delete()
    db.session.flush()

    added = 0
    for idx, uid in enumerate(user_ids):
        if not uid:
            continue
        user = db.session.get(User, uid)
        # Only assign reviewers from the same org (a foreign reviewer would gain
        # access to this proposal via the reviewer branch of _can_view_proposal).
        if not user or not _same_org(user):
            continue
        role = roles[idx] if idx < len(roles) else "other"
        if role not in {r[0] for r in REVIEW_ROLE_OPTIONS}:
            role = "other"
        req_idx = f"required_{idx}"
        is_required = req_idx in required_flags or request.form.get(req_idx) == "1"
        # Default to required=True
        if not required_flags and not request.form.get(req_idx):
            is_required = True
        reviewer = ProposalReviewer(
            proposal_id=proposal_id,
            user_id=uid,
            review_role=role,
            is_required=is_required,
            assigned_by=current_user.id,
            deadline=deadline,
            notes=note,
        )
        db.session.add(reviewer)
        added += 1

        _notify(
            uid,
            "review_assigned",
            f"You've been assigned to review: {project.name}",
            f"{current_user.display_name or current_user.username} assigned you as the {role.title()} reviewer on '{project.name}'.",
            link=f"/proposal/{proposal_id}/review",
        )

    if added == 0:
        flash("No valid reviewers were added.", "error")
        return redirect(url_for("send_for_review", proposal_id=proposal_id))

    if deadline:
        proposal.review_deadline = deadline

    try:
        lifecycle_transition(proposal, "in_review", current_user.id,
                             note=f"Sent for internal review. {added} reviewer(s) assigned.")
    except LifecycleError as e:
        flash(str(e), "error")
        return redirect(url_for("view_proposal", proposal_id=proposal_id))

    db.session.commit()
    _log_activity("proposal_send_for_review",
                  f"Sent '{project.name}' proposal for internal review ({added} reviewer(s))",
                  project.id)
    flash(f"Proposal sent for review to {added} stakeholder(s).", "success")
    return redirect(url_for("view_proposal", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/review", methods=["GET", "POST"])
@login_required
def proposal_review_page(proposal_id):
    """Reviewer-facing page: file revision requests and approve/request changes."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal or not _can_view_proposal(proposal):
        abort(404)
    project = db.session.get(Project, proposal.project_id)

    reviewer = _get_reviewer(proposal.id, current_user.id)
    if reviewer is None and not _is_proposal_owner(proposal):
        flash("You are not a reviewer on this proposal.", "error")
        return redirect(url_for("view_proposal", proposal_id=proposal_id))

    version = latest_version(proposal_id)
    md_path = GENERATED_DIR / proposal.md_file
    proposal_md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    proposal_html = htmlsafe.sanitize(md.markdown(proposal_md, extensions=["tables", "fenced_code"]))

    # My pending/existing revision requests on this proposal
    my_requests = RevisionRequest.query.filter_by(
        proposal_id=proposal_id, author_id=current_user.id
    ).order_by(RevisionRequest.created_at.desc()).all()

    # Templates I can apply
    templates = RevisionTemplate.query.filter_by(org_id=current_user.org_id).order_by(
        RevisionTemplate.category, RevisionTemplate.name
    ).all()

    # My current decision on the latest version, if any
    my_decision = None
    if version:
        my_decision = ProposalApproval.query.filter_by(
            proposal_id=proposal_id,
            version_id=version.id,
            user_id=current_user.id,
        ).order_by(ProposalApproval.decided_at.desc()).first()

    state = approval_state(proposal)

    return render_template(
        "proposal_review.html",
        proposal=proposal,
        project=project,
        reviewer=reviewer,
        version=version,
        proposal_html=proposal_html,
        my_requests=my_requests,
        my_decision=my_decision,
        templates=templates,
        review_categories=REVISION_CATEGORIES,
        review_roles=REVIEW_ROLE_OPTIONS,
        state=state,
        lifecycle_labels=LIFECYCLE_LABELS,
    )


@app.route("/proposal/<proposal_id>/revision-request", methods=["POST"])
@login_required
def create_revision_request(proposal_id):
    """Create a new structured revision request."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal or not _can_view_proposal(proposal):
        abort(404)
    project = db.session.get(Project, proposal.project_id)

    reviewer = _get_reviewer(proposal_id, current_user.id)
    is_owner = _is_proposal_owner(proposal)
    if reviewer is None and not is_owner:
        abort(403)

    directive = request.form.get("directive", "").strip()
    if not directive:
        flash("Directive is required.", "error")
        return redirect(url_for("proposal_review_page", proposal_id=proposal_id))

    category = request.form.get("category", "other").strip().lower()
    if category not in {c[0] for c in REVISION_CATEGORIES}:
        category = "other"

    target_section = request.form.get("target_section", "").strip()[:200]

    if reviewer:
        source = _source_for_review_role(reviewer.review_role)
    else:
        source = "internal_other"

    req = RevisionRequest(
        proposal_id=proposal_id,
        author_id=current_user.id,
        source=source,
        category=category,
        directive=directive,
        target_section=target_section,
        status="pending",
    )
    db.session.add(req)

    # Notify the proposal owner that a request was filed
    if project and project.user_id != current_user.id:
        _notify(
            project.user_id,
            "revision_requested",
            f"Revision requested on: {project.name}",
            f"{current_user.display_name or current_user.username} filed a {category} revision request.",
            link=f"/proposal/{proposal_id}",
        )

    # If current status is in_review and we got a request, transition
    if proposal.review_status == "in_review":
        try:
            lifecycle_transition(
                proposal, "revision_requested", current_user.id,
                note=f"Revision request filed by {current_user.display_name or current_user.username}.",
            )
        except LifecycleError:
            pass

    db.session.commit()
    _log_activity("revision_request_create",
                  f"Filed {category} revision request on '{project.name}'",
                  project.id if project else None)
    flash("Revision request filed.", "success")
    return redirect(url_for("proposal_review_page", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/revision-request/<request_id>/withdraw", methods=["POST"])
@login_required
def withdraw_revision_request(proposal_id, request_id):
    """Soft-delete a revision request (status=withdrawn) — only author or owner."""
    req = db.session.get(RevisionRequest, request_id)
    if not req or req.proposal_id != proposal_id:
        abort(404)
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    if req.author_id != current_user.id and not _is_proposal_owner(proposal):
        abort(403)
    if req.status != "pending":
        flash("Only pending requests can be withdrawn.", "error")
        return redirect(url_for("proposal_review_page", proposal_id=proposal_id))
    req.status = "withdrawn"
    db.session.commit()
    _log_activity("revision_request_withdraw", f"Withdrew revision request {request_id}")
    flash("Revision request withdrawn.", "success")
    return redirect(request.referrer or url_for("proposal_review_page", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/approve", methods=["POST"])
@login_required
def approve_proposal(proposal_id):
    """Record an approval or request-changes decision for the latest version."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal or not _can_view_proposal(proposal):
        abort(404)

    reviewer = _get_reviewer(proposal_id, current_user.id)
    if reviewer is None:
        flash("You are not a reviewer on this proposal.", "error")
        return redirect(url_for("proposal_review_page", proposal_id=proposal_id))

    project = db.session.get(Project, proposal.project_id)

    # Prevent self-approval loophole: owner cannot approve their own proposal.
    if project and project.user_id == current_user.id:
        flash("You cannot approve your own proposal. Another reviewer must approve it.", "error")
        return redirect(url_for("proposal_review_page", proposal_id=proposal_id))

    decision = request.form.get("decision", "").strip()
    if decision not in ("approved", "requested_changes"):
        flash("Invalid decision.", "error")
        return redirect(url_for("proposal_review_page", proposal_id=proposal_id))

    note = request.form.get("note", "").strip()
    version = latest_version(proposal_id)
    if not version:
        flash("No version to approve.", "error")
        return redirect(url_for("proposal_review_page", proposal_id=proposal_id))

    # Upsert one decision per (proposal, version, user)
    existing = ProposalApproval.query.filter_by(
        proposal_id=proposal_id, version_id=version.id, user_id=current_user.id
    ).first()
    if existing:
        existing.decision = decision
        existing.note = note
        existing.decided_at = datetime.now(timezone.utc)
    else:
        db.session.add(ProposalApproval(
            proposal_id=proposal_id,
            version_id=version.id,
            user_id=current_user.id,
            review_role=reviewer.review_role,
            decision=decision,
            note=note,
        ))

    db.session.flush()
    auto_advance_after_decision(proposal, current_user.id)

    # Notify the proposal owner
    if project and project.user_id != current_user.id:
        label = "approved" if decision == "approved" else "requested changes"
        _notify(
            project.user_id,
            "proposal_approved" if decision == "approved" else "revision_requested",
            f"{current_user.display_name or current_user.username} {label} your proposal: {project.name}",
            note[:200] if note else "",
            link=f"/proposal/{proposal_id}",
        )

    db.session.commit()
    _log_activity(
        "proposal_approve" if decision == "approved" else "proposal_request_changes",
        f"{decision} on v{version.version_number}",
        project.id if project else None,
    )
    flash(
        "Approval recorded." if decision == "approved" else "Change request recorded.",
        "success",
    )
    return redirect(url_for("proposal_review_page", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/apply-feedback", methods=["GET", "POST"])
@login_required
def apply_feedback(proposal_id):
    """Owner-only batch-apply UI: review pending revision requests and trigger AI."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal or not _is_proposal_owner(proposal):
        abort(404)
    project = db.session.get(Project, proposal.project_id)

    pending = pending_requests(proposal_id)

    if request.method == "GET":
        return render_template(
            "proposal_apply_feedback.html",
            proposal=proposal,
            project=project,
            pending=pending,
            review_categories=REVISION_CATEGORIES,
            lifecycle_labels=LIFECYCLE_LABELS,
        )

    # POST — collect selected request ids & edited directives
    selected_ids = request.form.getlist("apply_request_id")
    if not selected_ids:
        flash("Please select at least one revision request to apply.", "error")
        return redirect(url_for("apply_feedback", proposal_id=proposal_id))

    selected_requests = []
    for rid in selected_ids:
        req = db.session.get(RevisionRequest, rid)
        if not req or req.proposal_id != proposal_id or req.status != "pending":
            continue
        # Allow owner to edit the directive inline before the AI sees it
        edited = request.form.get(f"directive_{rid}", "").strip()
        if edited:
            req.directive = edited
        selected_requests.append(req)

    if not selected_requests:
        flash("No valid requests selected.", "error")
        return redirect(url_for("apply_feedback", proposal_id=proposal_id))

    # Build the AI payload
    current_version = latest_version(proposal_id)
    if not current_version:
        flash("Proposal has no version to revise.", "error")
        return redirect(url_for("view_proposal", proposal_id=proposal_id))

    ai_payload = []
    for r in selected_requests:
        author = db.session.get(User, r.author_id) if r.author_id else None
        author_label = (author.display_name or author.username) if author else r.source.replace("_", " ").title()
        role_label = r.source.replace("internal_", "").replace("_", " ").title()
        ai_payload.append({
            "source": r.source,
            "category": r.category,
            "directive": r.directive,
            "target_section": r.target_section,
            "author_label": f"{author_label} — {role_label}",
        })

    # Load supporting context
    owner = _proposal_owner(proposal)
    owner_id = owner.id if owner else current_user.id
    standards = CompanyStandard.query.filter_by(org_id=_owner_org_id(project), is_active=True).all()
    standards_data = [
        {"category": s.category, "title": s.title, "content": s.content}
        for s in standards
    ] if standards else None

    corrections = ProposalCorrection.query.filter_by(org_id=_owner_org_id(project)).order_by(
        ProposalCorrection.created_at.desc()
    ).limit(10).all()
    corrections_data = [
        {
            "vertical": c.vertical,
            "summary": c.correction_summary,
            "original": (c.original_snippet or "")[:500],
            "corrected": (c.corrected_snippet or "")[:500],
            "type": c.correction_type,
        }
        for c in corrections
    ] if corrections else None

    try:
        result = revise_proposal(
            current_markdown=current_version.markdown_content,
            revision_requests=ai_payload,
            vertical=proposal.vertical,
            company_name=current_user.company_name,
            user_api_key=decrypt_api_key(current_user.api_key_encrypted) or None,
            user_model=current_user.llm_model or None,
            company_standards=standards_data,
            past_corrections=corrections_data,
        )
    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("apply_feedback", proposal_id=proposal_id))
    except Exception as e:
        flash(f"AI revision failed: {friendly_api_error(e)}", "error")
        return redirect(url_for("apply_feedback", proposal_id=proposal_id))

    # Create the new version
    latest_num = current_version.version_number
    new_version = ProposalVersion(
        proposal_id=proposal_id,
        version_number=latest_num + 1,
        markdown_content=result["revised_markdown"],
        edit_source="ai",
        editor_id=current_user.id,
        change_summary=f"AI revision: {result['ai_summary']}",
    )
    db.session.add(new_version)
    db.session.flush()

    # Write the new markdown/docx to disk
    md_path = GENERATED_DIR / proposal.md_file
    md_path.write_text(result["revised_markdown"], encoding="utf-8")
    if proposal.docx_file:
        docx_path = GENERATED_DIR / proposal.docx_file
        markdown_to_docx(result["revised_markdown"], str(docx_path))

    # Update action items count
    proposal.action_items_count = len(
        re.findall(r"\[ACTION REQUIRED:\s*(.+?)\]", result["revised_markdown"])
    )

    # Log the revision batch
    batch = ProposalRevisionBatch(
        proposal_id=proposal_id,
        from_version_id=current_version.id,
        to_version_id=new_version.id,
        triggered_by=current_user.id,
        request_count=len(selected_requests),
        ai_change_summary=json.dumps({
            "summary": result["ai_summary"],
            "change_log": result["change_log"],
        }),
    )
    db.session.add(batch)

    # Mark requests as applied
    for r in selected_requests:
        r.status = "applied"
        r.applied_in_version_id = new_version.id

    # Transition back to in_review so reviewers can re-approve the new version.
    # A new version invalidates all prior approvals; reviewers must re-approve.
    try:
        if proposal.review_status in (
            "revision_requested", "in_review", "internally_approved", "customer_feedback"
        ):
            lifecycle_transition(
                proposal, "in_review", current_user.id,
                note=f"AI generated v{new_version.version_number} from {len(selected_requests)} request(s)."
            )
    except LifecycleError:
        pass

    # Notify reviewers a new version needs their attention
    reviewers = ProposalReviewer.query.filter_by(proposal_id=proposal_id).all()
    for rv in reviewers:
        if rv.user_id == current_user.id:
            continue
        _notify(
            rv.user_id,
            "review_assigned",
            f"New version to review: {project.name}",
            f"v{new_version.version_number} was generated from {len(selected_requests)} revision request(s). Please re-review.",
            link=f"/proposal/{proposal_id}/review",
        )

    db.session.commit()
    _log_activity(
        "proposal_revise",
        f"AI-revised proposal to v{new_version.version_number} ({len(selected_requests)} requests applied)",
        project.id,
    )
    flash(
        f"Version {new_version.version_number} generated. {len(selected_requests)} request(s) applied.",
        "success",
    )
    return redirect(url_for("view_proposal", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/submit-to-customer", methods=["POST"])
@login_required
def submit_to_customer(proposal_id):
    """Owner transitions internally_approved → submitted_to_customer."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal or not _is_proposal_owner(proposal):
        abort(404)
    project = db.session.get(Project, proposal.project_id)

    try:
        lifecycle_transition(
            proposal, "submitted_to_customer", current_user.id,
            note="Submitted to customer by owner.",
        )
    except LifecycleError as e:
        flash(str(e), "error")
        return redirect(url_for("view_proposal", proposal_id=proposal_id))

    db.session.commit()
    _log_activity("proposal_submit_to_customer", f"Submitted proposal for '{project.name}' to customer", project.id)
    flash("Proposal marked as submitted to customer.", "success")
    return redirect(url_for("view_proposal", proposal_id=proposal_id))


# ---------------------------------------------------------------------------
# Customer share portal (Phase 3)
# ---------------------------------------------------------------------------

def _active_share(proposal_id):
    return (
        ProposalShare.query.filter_by(proposal_id=proposal_id)
        .filter(ProposalShare.revoked_at.is_(None))
        .order_by(ProposalShare.created_at.desc())
        .first()
    )


@app.route("/proposal/<proposal_id>/share", methods=["POST"])
@login_required
def create_share(proposal_id):
    """Create (or refresh) a customer share link, optionally emailing it with
    the PDF attached. Also advances the lifecycle to submitted_to_customer."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal or not _is_proposal_owner(proposal):
        abort(404)
    project = db.session.get(Project, proposal.project_id)

    customer_email = request.form.get("customer_email", "").strip()
    allow_comments = request.form.get("allow_comments", "1") == "1"
    allow_decision = request.form.get("allow_decision", "1") == "1"
    send_email_flag = request.form.get("send_email") == "1"

    if customer_email:
        project.client_email = customer_email

    version = latest_version(proposal_id)

    # Reuse an existing active share or create a new one
    share = _active_share(proposal_id)
    if not share:
        share = ProposalShare(
            proposal_id=proposal_id,
            project_id=project.id,
            created_by=current_user.id,
        )
        db.session.add(share)
    share.customer_email = customer_email or share.customer_email
    share.allow_comments = allow_comments
    share.allow_decision = allow_decision
    share.version_number = version.version_number if version else 0
    db.session.flush()

    # Advance lifecycle if we're at the internally-approved gate
    if proposal.review_status == "internally_approved":
        try:
            lifecycle_transition(proposal, "submitted_to_customer", current_user.id,
                                 note="Shared with customer via portal link.")
        except LifecycleError:
            pass

    share_url = url_for("customer_portal", token=share.token, _external=True)

    emailed = False
    if send_email_flag and customer_email:
        try:
            pdf_path = _ensure_proposal_pdf(proposal, project)
        except Exception:
            pdf_path = None
        import mailer
        body = (
            f"Hello,\n\n{current_user.company_name or 'We'} have prepared a proposal for "
            f"{project.name}. You can review it online here:\n\n{share_url}\n\n"
        )
        if share.allow_decision:
            body += "You can accept, decline, or leave comments directly on that page.\n\n"
        body += f"Thank you,\n{current_user.display_name or current_user.username}"
        attachments = [str(pdf_path)] if pdf_path else None
        emailed = mailer.send_email(
            to=customer_email,
            subject=f"Proposal for {project.name}",
            body=body,
            attachments=attachments,
        )

    db.session.commit()
    _log_activity("proposal_share", f"Created customer share for '{project.name}'", project.id)
    if emailed:
        flash(f"Proposal emailed to {customer_email}. Share link is also on this page.", "success")
    else:
        flash(f"Share link ready: {share_url}", "success")
    return redirect(url_for("view_proposal", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/share/revoke", methods=["POST"])
@login_required
def revoke_share(proposal_id):
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal or not _is_proposal_owner(proposal):
        abort(404)
    share = _active_share(proposal_id)
    if share:
        share.revoked_at = datetime.now(timezone.utc)
        db.session.commit()
        flash("Customer share link revoked.", "success")
    return redirect(url_for("view_proposal", proposal_id=proposal_id))


def _valid_share(token):
    share = ProposalShare.query.filter_by(token=token).first()
    if not share or share.revoked_at:
        return None
    if share.expires_at:
        exp = share.expires_at.replace(tzinfo=None) if share.expires_at.tzinfo else share.expires_at
        if exp < datetime.utcnow():
            return None
    return share


@app.route("/p/<token>")
def customer_portal(token):
    """Public, read-only branded proposal view for a customer. No login."""
    share = _valid_share(token)
    if not share:
        return render_template("portal_invalid.html"), 404

    proposal = db.session.get(Proposal, share.proposal_id)
    project = db.session.get(Project, share.project_id)
    owner = db.session.get(User, project.user_id) if project else None

    # Record the view (dedupe rapid reloads within 60s from same IP)
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    recent = (
        ShareView.query.filter_by(share_id=share.id, ip=ip)
        .order_by(ShareView.viewed_at.desc())
        .first()
    )
    now = datetime.now(timezone.utc)
    is_new_view = True
    if recent and recent.viewed_at:
        last = recent.viewed_at.replace(tzinfo=timezone.utc) if recent.viewed_at.tzinfo is None else recent.viewed_at
        if (now - last).total_seconds() < 60:
            is_new_view = False
    if is_new_view:
        db.session.add(ShareView(
            share_id=share.id, ip=ip,
            user_agent=(request.headers.get("User-Agent", "") or "")[:400],
        ))
        share.view_count = (share.view_count or 0) + 1
        share.last_viewed_at = now
        db.session.commit()

    version = latest_version(proposal.id)
    md_text = version.markdown_content if version else ""
    # Strip internal-only markers from the customer-facing view
    # DOTALL/IGNORECASE so multi-line internal markers are also stripped and can't
    # leak onto the public customer portal.
    md_text = re.sub(r"\[ACTION REQUIRED:.*?\]", "", md_text, flags=re.DOTALL | re.IGNORECASE)
    md_text = re.sub(r"\[ASSUMED:.*?\]", "", md_text, flags=re.DOTALL | re.IGNORECASE)
    proposal_html = htmlsafe.sanitize(md.markdown(md_text, extensions=["tables", "fenced_code"]))

    company_name = (owner.company_name if owner else "") or ""
    logo_url = url_for("portal_logo", token=token) if (
        owner and owner.company_logo_path and getattr(owner, "company_logo_use_in_proposals", False)
        and Path(owner.company_logo_path).exists()
    ) else ""

    return render_template(
        "portal.html",
        share=share,
        proposal=proposal,
        project=project,
        proposal_html=proposal_html,
        company_name=company_name,
        logo_url=logo_url,
    )


@app.route("/p/<token>/logo")
def portal_logo(token):
    share = _valid_share(token)
    if not share:
        abort(404)
    project = db.session.get(Project, share.project_id)
    owner = db.session.get(User, project.user_id) if project else None
    if not owner or not owner.company_logo_path or not Path(owner.company_logo_path).exists():
        abort(404)
    return send_file(owner.company_logo_path, mimetype="image/png")


@app.route("/p/<token>/comment", methods=["POST"])
def portal_comment(token):
    """Customer leaves a comment → becomes a pending customer revision request."""
    share = _valid_share(token)
    if not share or not share.allow_comments:
        abort(404)
    proposal = db.session.get(Proposal, share.proposal_id)
    project = db.session.get(Project, share.project_id)

    body = request.form.get("comment", "").strip()
    section = request.form.get("section", "").strip()[:200]
    if not body:
        flash("Please enter a comment.", "error")
        return redirect(url_for("customer_portal", token=token))

    db.session.add(RevisionRequest(
        proposal_id=proposal.id,
        author_id=None,
        source="customer",
        category="other",
        directive=body,
        target_section=section,
        status="pending",
    ))
    # Move the proposal into customer_feedback so the owner sees it
    if proposal.review_status == "submitted_to_customer":
        try:
            lifecycle_transition(proposal, "customer_feedback", None,
                                 note="Customer left a comment via the portal.")
        except LifecycleError:
            pass
    db.session.commit()

    if project:
        _notify(project.user_id, "customer_feedback",
                f"Customer comment on {project.name}",
                body[:160], link=f"/proposal/{proposal.id}")
        _notify_via_integrations(project.org_id,
                                 f"💬 Customer comment on *{project.name}*: {body[:200]}")
    flash("Thanks — your comment has been sent to the team.", "success")
    return redirect(url_for("customer_portal", token=token))


@app.route("/p/<token>/decision", methods=["POST"])
def portal_decision(token):
    """Customer accepts or declines through the portal."""
    share = _valid_share(token)
    if not share or not share.allow_decision:
        abort(404)
    proposal = db.session.get(Proposal, share.proposal_id)
    project = db.session.get(Project, share.project_id)

    decision = request.form.get("decision", "").strip()
    note = request.form.get("note", "").strip()
    if decision not in ("accepted", "declined"):
        flash("Invalid decision.", "error")
        return redirect(url_for("customer_portal", token=token))

    share.decision = decision
    share.decision_note = note
    share.decided_at = datetime.now(timezone.utc)

    try:
        if decision == "accepted":
            if proposal.review_status in ("submitted_to_customer", "customer_feedback"):
                lifecycle_transition(proposal, "customer_approved", None, note="Accepted by customer via portal.")
                lifecycle_transition(proposal, "won", None, note="Customer accepted.")
        else:
            if proposal.review_status in ("submitted_to_customer", "customer_feedback"):
                lifecycle_transition(proposal, "customer_declined", None, note=note or "Declined by customer via portal.")
                lifecycle_transition(proposal, "lost", None, note="Customer declined.")
    except LifecycleError:
        pass
    db.session.commit()

    if project:
        verb = "accepted" if decision == "accepted" else "declined"
        _notify(project.user_id, "customer_decision",
                f"Customer {verb}: {project.name}", note[:160],
                link=f"/proposal/{proposal.id}")
        _notify_via_integrations(project.org_id,
                                 f"{'✅' if decision=='accepted' else '❌'} Customer *{verb}* the proposal for *{project.name}*.")
    flash("Thank you — your response has been recorded.", "success")
    return redirect(url_for("customer_portal", token=token))


@app.route("/proposal/<proposal_id>/customer-feedback", methods=["GET", "POST"])
@login_required
def customer_feedback(proposal_id):
    """Owner enters customer feedback — either typed items or pasted email for AI parsing."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal or not _is_proposal_owner(proposal):
        abort(404)
    project = db.session.get(Project, proposal.project_id)

    if request.method == "GET":
        return render_template(
            "proposal_customer_feedback.html",
            proposal=proposal,
            project=project,
            review_categories=REVISION_CATEGORIES,
            lifecycle_labels=LIFECYCLE_LABELS,
        )

    mode = request.form.get("mode", "manual")
    created = 0

    if mode == "parse_email":
        email_text = request.form.get("email_text", "").strip()
        if not email_text:
            flash("Please paste the customer's email.", "error")
            return redirect(url_for("customer_feedback", proposal_id=proposal_id))
        try:
            drafts = parse_customer_email(
                email_text,
                user_api_key=decrypt_api_key(current_user.api_key_encrypted) or None,
                user_model=current_user.llm_model or None,
            )
        except RuntimeError as e:
            flash(str(e), "error")
            return redirect(url_for("customer_feedback", proposal_id=proposal_id))
        except Exception as e:
            flash(f"Email parsing failed: {friendly_api_error(e)}", "error")
            return redirect(url_for("customer_feedback", proposal_id=proposal_id))

        if not drafts:
            flash("The AI did not find any revision requests in that email.", "error")
            return redirect(url_for("customer_feedback", proposal_id=proposal_id))

        for d in drafts:
            req = RevisionRequest(
                proposal_id=proposal_id,
                author_id=current_user.id,
                source="customer",
                category=d.get("category", "other"),
                directive=d["directive"],
                target_section=d.get("target_section", ""),
                status="pending",
            )
            db.session.add(req)
            created += 1
    else:
        # Manual entry: one directive per row (directives[])
        directives = request.form.getlist("directive")
        categories = request.form.getlist("category")
        sections = request.form.getlist("target_section")
        for idx, text in enumerate(directives):
            text = text.strip()
            if not text:
                continue
            cat = categories[idx] if idx < len(categories) else "other"
            if cat not in {c[0] for c in REVISION_CATEGORIES}:
                cat = "other"
            sect = sections[idx] if idx < len(sections) else ""
            req = RevisionRequest(
                proposal_id=proposal_id,
                author_id=current_user.id,
                source="customer",
                category=cat,
                directive=text,
                target_section=sect[:200],
                status="pending",
            )
            db.session.add(req)
            created += 1

    if created == 0:
        flash("No revision requests were created.", "error")
        return redirect(url_for("customer_feedback", proposal_id=proposal_id))

    # Transition to customer_feedback state
    try:
        if proposal.review_status in ("submitted_to_customer", "customer_feedback"):
            if proposal.review_status == "submitted_to_customer":
                lifecycle_transition(
                    proposal, "customer_feedback", current_user.id,
                    note=f"{created} customer feedback item(s) logged.",
                )
    except LifecycleError:
        pass

    db.session.commit()
    _log_activity("customer_feedback_log", f"Logged {created} customer feedback item(s)", project.id)
    flash(
        f"{created} customer revision request(s) logged. Review and apply them to generate a new version.",
        "success",
    )
    return redirect(url_for("apply_feedback", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/customer-decision", methods=["POST"])
@login_required
def customer_decision(proposal_id):
    """Owner records the customer's final decision: accepted or declined."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal or not _is_proposal_owner(proposal):
        abort(404)
    project = db.session.get(Project, proposal.project_id)

    decision = request.form.get("decision", "").strip()
    note = request.form.get("note", "").strip()

    if decision == "accepted":
        try:
            lifecycle_transition(proposal, "customer_approved", current_user.id, note=note)
            lifecycle_transition(proposal, "won", current_user.id, note="Customer accepted.")
        except LifecycleError as e:
            flash(str(e), "error")
            return redirect(url_for("view_proposal", proposal_id=proposal_id))
        flash("Marked as won. Congratulations!", "success")
    elif decision == "declined":
        try:
            lifecycle_transition(proposal, "customer_declined", current_user.id, note=note)
            lifecycle_transition(proposal, "lost", current_user.id, note="Customer declined.")
        except LifecycleError as e:
            flash(str(e), "error")
            return redirect(url_for("view_proposal", proposal_id=proposal_id))
        flash("Marked as lost.", "success")
    else:
        flash("Invalid decision.", "error")
        return redirect(url_for("view_proposal", proposal_id=proposal_id))

    db.session.commit()
    _log_activity("customer_decision", f"Customer {decision}", project.id)
    return redirect(url_for("view_proposal", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/preflight")
@login_required
def proposal_preflight(proposal_id):
    """Run a pre-flight AI sanity check on the latest version. Returns JSON."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal or not _is_proposal_owner(proposal):
        abort(404)

    version = latest_version(proposal_id)
    if not version:
        return {"action_items": [], "warnings": ["No version to check."], "ready": False}

    try:
        result = preflight_check_proposal(
            version.markdown_content,
            user_api_key=decrypt_api_key(current_user.api_key_encrypted) or None,
            user_model=current_user.llm_model or None,
        )
    except Exception as e:
        result = {
            "action_items": [],
            "warnings": [f"Pre-flight check error: {friendly_api_error(e)}"],
            "ready": False,
        }

    return result


# ---------------------------------------------------------------------------
# Revision Request Templates (user-level presets)
# ---------------------------------------------------------------------------


@app.route("/settings/add-revision-template", methods=["POST"])
@login_required
def add_revision_template():
    name = request.form.get("template_name", "").strip()
    category = request.form.get("template_category", "other").strip().lower()
    directive = request.form.get("template_directive", "").strip()
    description = request.form.get("template_description", "").strip()

    if not name or not directive:
        flash("Name and directive are required.", "error")
        return redirect(url_for("posture") + "#revision-presets")

    if category not in {c[0] for c in REVISION_CATEGORIES}:
        category = "other"

    tmpl = RevisionTemplate(
        user_id=current_user.id,
        org_id=current_user.org_id,
        name=name[:200],
        category=category,
        directive_template=directive,
        description=description,
    )
    db.session.add(tmpl)
    db.session.commit()
    _log_activity("revision_template_add", f"Added revision template: {name}")
    flash(f"Revision template '{name}' added.", "success")
    return redirect(url_for("posture") + "#revision-presets")


@app.route("/settings/delete-revision-template/<template_id>", methods=["POST"])
@login_required
def delete_revision_template(template_id):
    tmpl = db.session.get(RevisionTemplate, template_id)
    if not tmpl or tmpl.org_id != current_user.org_id:
        abort(404)
    name = tmpl.name
    db.session.delete(tmpl)
    db.session.commit()
    _log_activity("revision_template_delete", f"Deleted revision template: {name}")
    flash(f"Revision template '{name}' deleted.", "success")
    return redirect(url_for("posture") + "#revision-presets")


# ---------------------------------------------------------------------------
# Company Standards & Posture Library
# ---------------------------------------------------------------------------

@app.route("/settings/add-company-standard", methods=["POST"])
@login_required
def add_company_standard():
    category = request.form.get("standard_category", "").strip()
    title = request.form.get("standard_title", "").strip()
    content = request.form.get("standard_content", "").strip()

    if not category or not title:
        flash("Category and title are required.", "error")
        return redirect(url_for("posture") + "#standards")

    # Handle file upload as alternative to text content
    uploaded_file = request.files.get("standard_file")
    if uploaded_file and uploaded_file.filename and _allowed_file(uploaded_file.filename, ALLOWED_EXTENSIONS | {"xlsx", "xls"}):
        safe, path, size = _save_upload(uploaded_file, "company_standards")
        content = content or f"[Uploaded file: {safe}]"

    if not content:
        flash("Either content or a file is required.", "error")
        return redirect(url_for("posture") + "#standards")

    standard = CompanyStandard(
        user_id=current_user.id,
        org_id=current_user.org_id,
        category=category,
        title=title,
        content=content,
    )
    db.session.add(standard)
    db.session.commit()
    _log_activity("company_standard_add", f"Added standard: {title}")
    flash(f"Company standard '{title}' added.", "success")
    return redirect(url_for("posture") + "#standards")


@app.route("/settings/edit-company-standard/<standard_id>", methods=["POST"])
@login_required
def edit_company_standard(standard_id):
    std = db.session.get(CompanyStandard, standard_id)
    if not std or std.org_id != current_user.org_id:
        abort(404)

    std.category = request.form.get("standard_category", std.category).strip()
    std.title = request.form.get("standard_title", std.title).strip()
    std.content = request.form.get("standard_content", std.content).strip()

    db.session.commit()
    _log_activity("company_standard_edit", f"Updated standard: {std.title}")
    flash(f"Standard '{std.title}' updated.", "success")
    return redirect(url_for("posture") + "#standards")


@app.route("/settings/delete-company-standard/<standard_id>", methods=["POST"])
@login_required
def delete_company_standard(standard_id):
    std = db.session.get(CompanyStandard, standard_id)
    if not std or std.org_id != current_user.org_id:
        abort(404)
    title = std.title
    db.session.delete(std)
    db.session.commit()
    _log_activity("company_standard_delete", f"Deleted standard: {title}")
    flash(f"Standard '{title}' deleted.", "success")
    return redirect(url_for("posture") + "#standards")


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

@app.route("/admin")
@login_required
def admin_panel():
    if not current_user.is_admin:
        flash("Access denied.", "error")
        return redirect(url_for("dashboard"))

    users = _org_users_query().order_by(User.created_at.desc()).all()
    org = db.session.get(Organization, current_user.org_id) if current_user.org_id else None
    pending_invitations = OrgInvitation.query.filter_by(
        org_id=current_user.org_id
    ).filter(
        OrgInvitation.accepted_at.is_(None),
        OrgInvitation.revoked_at.is_(None),
    ).order_by(OrgInvitation.created_at.desc()).all() if current_user.org_id else []

    from sqlalchemy import func

    # Per-user stats
    user_stats = []
    for user in users:
        total = Project.query.filter_by(user_id=user.id).count()
        won = Project.query.filter_by(user_id=user.id, status="won").count()
        lost = Project.query.filter_by(user_id=user.id, status="lost").count()
        decided = won + lost
        total_dollar = db.session.query(func.sum(Project.dollar_amount)).filter(
            Project.user_id == user.id, Project.dollar_amount > 0
        ).scalar() or 0
        proposal_count = Proposal.query.join(Project).filter(Project.user_id == user.id).count()

        # Last activity timestamp
        last_log = ActivityLog.query.filter_by(user_id=user.id).order_by(ActivityLog.created_at.desc()).first()
        last_active = last_log.created_at.strftime('%Y-%m-%d') if last_log else None

        user_stats.append({
            "user": user,
            "total_projects": total,
            "proposal_count": proposal_count,
            "won": won,
            "lost": lost,
            "win_rate": round((won / decided) * 100) if decided > 0 else 0,
            "total_dollar": total_dollar,
            "last_active": last_active,
        })

    # Organization-wide totals
    org_project_filter = Project.org_id == current_user.org_id
    total_projects = Project.query.filter(org_project_filter).count()
    total_proposals = Proposal.query.join(Project).filter(org_project_filter).count()
    total_won = Project.query.filter(org_project_filter, Project.status == "won").count()
    total_lost = Project.query.filter(org_project_filter, Project.status == "lost").count()
    total_decided = total_won + total_lost
    total_users = len(users)
    company_total_dollar = db.session.query(func.sum(Project.dollar_amount)).filter(
        org_project_filter, Project.dollar_amount > 0
    ).scalar() or 0

    company_stats = {
        "total_users": total_users,
        "total_projects": total_projects,
        "total_proposals": total_proposals,
        "total_won": total_won,
        "total_lost": total_lost,
        "win_rate": round((total_won / total_decided) * 100) if total_decided > 0 else 0,
        "loss_rate": round((total_lost / total_decided) * 100) if total_decided > 0 else 0,
        "total_dollar": company_total_dollar,
    }

    # Role counts
    role_counts = {"admin": 0, "sales": 0, "proposal": 0}
    for u in users:
        r = getattr(u, "role", None) or ("admin" if u.is_admin else "proposal")
        role_counts[r] = role_counts.get(r, 0) + 1

    # Per-role performance breakdown
    role_performance = {}
    for role_key in ("admin", "sales", "proposal"):
        role_users = [us for us in user_stats if (getattr(us["user"], "role", None) or ("admin" if us["user"].is_admin else "proposal")) == role_key]
        r_projects = sum(us["total_projects"] for us in role_users)
        r_proposals = sum(us["proposal_count"] for us in role_users)
        r_won = sum(us["won"] for us in role_users)
        r_lost = sum(us["lost"] for us in role_users)
        r_decided = r_won + r_lost
        r_dollar = sum(us["total_dollar"] for us in role_users)
        role_performance[role_key] = {
            "user_count": len(role_users),
            "projects": r_projects,
            "proposals": r_proposals,
            "won": r_won,
            "lost": r_lost,
            "win_rate": round((r_won / r_decided) * 100) if r_decided > 0 else 0,
            "pipeline": r_dollar,
        }

    # Activity filter (org-scoped: only members of this org)
    org_user_ids = [u.id for u in users]
    activity_filter = request.args.get("activity_role", "")
    if activity_filter and activity_filter in ("admin", "sales", "proposal"):
        role_user_ids = [u.id for u in users if (getattr(u, "role", None) or ("admin" if u.is_admin else "proposal")) == activity_filter]
        recent_activity = ActivityLog.query.filter(
            ActivityLog.user_id.in_(role_user_ids)
        ).order_by(ActivityLog.created_at.desc()).limit(50).all()
    else:
        recent_activity = ActivityLog.query.filter(
            ActivityLog.user_id.in_(org_user_ids)
        ).order_by(ActivityLog.created_at.desc()).limit(50).all()

    return render_template(
        "admin.html",
        users=users,
        org=org,
        pending_invitations=pending_invitations,
        user_stats=user_stats,
        company_stats=company_stats,
        role_counts=role_counts,
        role_performance=role_performance,
        recent_activity=recent_activity,
        activity_filter=activity_filter,
    )


@app.route("/admin/org-name", methods=["POST"])
@login_required
def update_org_name():
    """Rename the organization (workspace)."""
    if not current_user.is_admin or not current_user.org_id:
        abort(403)
    org = db.session.get(Organization, current_user.org_id)
    name = request.form.get("org_name", "").strip()
    if not name:
        flash("Workspace name cannot be empty.", "error")
        return redirect(url_for("admin_panel"))
    org.name = name[:300]
    db.session.commit()
    _log_activity("org_rename", f"Renamed workspace to {org.name}")
    flash("Workspace name updated.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/invite", methods=["POST"])
@login_required
def create_invitation():
    """Invite a teammate to the organization by email."""
    if not current_user.is_admin or not current_user.org_id:
        abort(403)

    email = request.form.get("invite_email", "").strip().lower()
    role = request.form.get("invite_role", "proposal").strip().lower()
    if role not in ("admin", "sales", "proposal"):
        role = "proposal"
    if not email or "@" not in email:
        flash("A valid email address is required.", "error")
        return redirect(url_for("admin_panel"))

    # Don't invite existing members
    existing = _org_users_query().filter(User.email.ilike(email)).first()
    if existing:
        flash(f"{email} is already a member of this workspace.", "error")
        return redirect(url_for("admin_panel"))

    # Seat-limit gate (counts members + outstanding invites)
    pending = OrgInvitation.query.filter_by(org_id=current_user.org_id).filter(
        OrgInvitation.accepted_at.is_(None), OrgInvitation.revoked_at.is_(None)
    ).count()
    ok, msg = billing_check_seat(current_user.org_id, pending_invites=pending)
    if not ok:
        flash(msg, "error")
        return redirect(url_for("billing_page"))

    from datetime import timedelta
    inv = OrgInvitation(
        org_id=current_user.org_id,
        email=email,
        role=role,
        invited_by=current_user.id,
        expires_at=datetime.utcnow() + timedelta(days=14),
    )
    db.session.add(inv)
    db.session.commit()

    invite_url = url_for("accept_invite", token=inv.token, _external=True)

    # Email the invite when a mailer is configured; always surface the link
    try:
        import mailer
        org = db.session.get(Organization, current_user.org_id)
        sent = mailer.send_email(
            to=email,
            subject=f"You're invited to join {org.name} on {APP_NAME}",
            body=(
                f"{current_user.display_name or current_user.username} invited you to join "
                f"{org.name} on {APP_NAME} as a {role.title()} user.\n\n"
                f"Accept the invitation:\n{invite_url}\n\n"
                f"This link expires in 14 days."
            ),
        )
    except Exception:
        sent = False

    _log_activity("org_invite", f"Invited {email} as {role}")
    if sent:
        flash(f"Invitation emailed to {email}.", "success")
    else:
        flash(f"Invitation created. Share this link with {email}: {invite_url}", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/invitations/<invite_id>/revoke", methods=["POST"])
@login_required
def revoke_invitation(invite_id):
    if not current_user.is_admin:
        abort(403)
    inv = db.session.get(OrgInvitation, invite_id)
    if not inv or inv.org_id != current_user.org_id:
        abort(404)
    inv.revoked_at = datetime.now(timezone.utc)
    db.session.commit()
    flash(f"Invitation for {inv.email} revoked.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/integrations", methods=["POST"])
@login_required
def update_integrations():
    """Save the org's Slack incoming webhook and generic outbound webhook URLs."""
    if not current_user.is_admin or not current_user.org_id:
        abort(403)
    import integrations
    org = db.session.get(Organization, current_user.org_id)
    slack = request.form.get("slack_webhook_url", "").strip()
    webhook = request.form.get("outbound_webhook_url", "").strip()
    # Validate against SSRF: must be a public host (no internal/metadata targets),
    # Slack pinned to slack.com. Empty clears the value; an invalid URL is rejected.
    errors = []
    if not slack:
        org.slack_webhook_url = ""
    elif integrations.is_safe_webhook_url(slack, require_https=True, host_suffix="slack.com"):
        org.slack_webhook_url = slack
    else:
        errors.append("Slack webhook must be a valid https://hooks.slack.com URL.")
    if not webhook:
        org.outbound_webhook_url = ""
    elif integrations.is_safe_webhook_url(webhook):
        org.outbound_webhook_url = webhook
    else:
        errors.append("Outbound webhook must be a valid public http(s) URL.")
    db.session.commit()
    _log_activity("integrations_update", "Updated integrations")
    flash("Integrations saved." if not errors else " ".join(errors),
          "success" if not errors else "error")
    return redirect(url_for("admin_panel") + "#integrations")


@app.route("/admin/export-data")
@login_required
def export_org_data():
    """Export all of the org's projects + proposals as a JSON file."""
    if not current_user.is_admin:
        abort(403)
    from flask import Response
    org = db.session.get(Organization, current_user.org_id)
    projects = Project.query.filter_by(org_id=current_user.org_id).all()
    data = {
        "organization": {"id": org.id, "name": org.name, "plan": org.plan} if org else {},
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "projects": [],
    }
    for p in projects:
        proposals = []
        for prop in Proposal.query.filter_by(project_id=p.id).all():
            latest = latest_version(prop.id)
            proposals.append({
                "id": prop.id,
                "document_type": prop.document_type,
                "review_status": prop.review_status,
                "confidence_score": prop.confidence_score,
                "generated_at": prop.generated_at.isoformat() if prop.generated_at else None,
                "markdown": latest.markdown_content if latest else "",
            })
        data["projects"].append({
            "id": p.id,
            "name": p.name,
            "client_name": p.client_name,
            "request_type": p.request_type,
            "status": p.status,
            "vertical": p.vertical_label,
            "dollar_amount": p.dollar_amount,
            "close_reason": p.close_reason,
            "close_category": p.close_category,
            "competitor_name": p.competitor_name,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "proposals": proposals,
        })
    _log_activity("data_export", f"Exported {len(projects)} project(s)")
    return Response(
        json.dumps(data, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=proposal_manager_export.json"},
    )


@app.route("/load-sample-data", methods=["POST"])
@login_required
def load_sample_data():
    """Seed the workspace with a sample project + posture so a new trial can see
    the product working immediately. Admin-only; refuses if data already exists."""
    if not current_user.is_admin:
        abort(403)
    org_id = current_user.org_id
    if Project.query.filter_by(org_id=org_id).count() > 0:
        flash("Sample data is only available for an empty workspace.", "error")
        return redirect(url_for("dashboard"))

    # Sample posture
    if StaffRole.query.filter_by(org_id=org_id).count() == 0:
        for rn, cat, hr in [("Senior Engineer", "Engineering", 165),
                            ("Project Manager", "Management", 185),
                            ("Field Technician", "Technician", 95)]:
            db.session.add(StaffRole(user_id=current_user.id, org_id=org_id,
                                     role_name=rn, category=cat, hourly_rate=hr))
    if CompanyStandard.query.filter_by(org_id=org_id).count() == 0:
        db.session.add(CompanyStandard(
            user_id=current_user.id, org_id=org_id, category="certifications",
            title="Certifications & Safety",
            content="ISO 9001:2015 certified. EMR of 0.72. OSHA 30 trained field staff."))

    # Sample project + RFP document
    project = Project(user_id=current_user.id, org_id=org_id,
                      name="Sample — Data Center EPMS", client_name="Acme Colo",
                      request_type="rfp", vertical="data_center",
                      vertical_label="Data Center / Mission Critical")
    db.session.add(project)
    db.session.flush()
    sample_rfp = (
        "REQUEST FOR PROPOSAL — Electrical Power Monitoring System (EPMS)\n\n"
        "Acme Colo seeks a complete EPMS for a new 5MW data hall: metering at all "
        "switchgear, PDUs, and RPPs; integration to the existing BMS; factory and "
        "site acceptance testing; and O&M documentation. Redundant (N+1) monitoring "
        "required. Provide a fixed-price proposal with schedule and staffing plan."
    )
    dpath = UPLOADS_DIR / f"projects/{project.id}"
    dpath.mkdir(parents=True, exist_ok=True)
    fpath = dpath / "sample_rfp.txt"
    fpath.write_text(sample_rfp, encoding="utf-8")
    storage.sync_up(fpath)
    db.session.add(ProjectDocument(
        project_id=project.id, filename="sample_rfp.txt", original_filename="sample_rfp.txt",
        file_type="rfp", file_path=str(fpath), file_size=fpath.stat().st_size))
    db.session.commit()
    _log_activity("sample_data", "Loaded sample data", project.id)
    flash("Sample project and posture loaded. Open it and try generating a proposal!", "success")
    return redirect(url_for("project_upload", project_id=project.id))


# ---------------------------------------------------------------------------
# Billing & Plans (Phase 5)
# ---------------------------------------------------------------------------

@app.route("/billing")
@login_required
def billing_page():
    org = db.session.get(Organization, current_user.org_id) if current_user.org_id else None
    current_plan = billing.plan_for(org)
    usage = {
        "generations": billing.generations_this_month(current_user.org_id),
        "seats": billing.seats_used(current_user.org_id),
        "limits": billing.limits_for(org),
    }
    return render_template(
        "billing.html",
        org=org,
        plans=billing.PLANS,
        current_plan_key=(org.plan if org else "free") or "free",
        current_plan=current_plan,
        usage=usage,
        stripe_enabled=billing.stripe_enabled(),
        is_admin=current_user.is_admin,
    )


@app.route("/billing/checkout/<plan_key>", methods=["POST"])
@login_required
def billing_checkout(plan_key):
    if not current_user.is_admin:
        flash("Only workspace admins can change the plan.", "error")
        return redirect(url_for("billing_page"))
    if plan_key not in billing.PLANS:
        abort(404)
    org = db.session.get(Organization, current_user.org_id)

    if not billing.stripe_enabled():
        # No Stripe configured. Direct plan switching is only for self-hosted
        # single-tenant installs (SELF_HOSTED=true). In hosted/SaaS mode a
        # missing Stripe key must NOT let an admin self-upgrade to a paid plan
        # for free — allow only downgrades to the free plan.
        if not SELF_HOSTED and plan_key != "free":
            flash("Online payments aren't available right now. Please try again later.", "error")
            return redirect(url_for("billing_page"))
        org.plan = plan_key
        org.billing_status = "active" if plan_key != "free" else ""
        db.session.commit()
        _log_activity("plan_change", f"Switched to {plan_key} plan (no Stripe)")
        flash(f"Plan changed to {billing.PLANS[plan_key]['name']}.", "success")
        return redirect(url_for("billing_page"))

    try:
        import stripe
        stripe.api_key = billing.STRIPE_SECRET_KEY
        price_id = billing.PLANS[plan_key]["price_id"]
        if not price_id:
            flash("This plan is not configured for checkout.", "error")
            return redirect(url_for("billing_page"))
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=url_for("billing_page", _external=True) + "?success=1",
            cancel_url=url_for("billing_page", _external=True) + "?canceled=1",
            customer=org.stripe_customer_id or None,
            client_reference_id=org.id,
            metadata={"org_id": org.id, "plan": plan_key},
        )
        return redirect(session.url, code=303)
    except Exception as e:
        flash(f"Could not start checkout: {e}", "error")
        return redirect(url_for("billing_page"))


@app.route("/billing/portal", methods=["POST"])
@login_required
def billing_portal():
    if not current_user.is_admin:
        abort(403)
    org = db.session.get(Organization, current_user.org_id)
    if not billing.stripe_enabled() or not org.stripe_customer_id:
        flash("No active Stripe subscription to manage.", "error")
        return redirect(url_for("billing_page"))
    try:
        import stripe
        stripe.api_key = billing.STRIPE_SECRET_KEY
        session = stripe.billing_portal.Session.create(
            customer=org.stripe_customer_id,
            return_url=url_for("billing_page", _external=True),
        )
        return redirect(session.url, code=303)
    except Exception as e:
        flash(f"Could not open billing portal: {e}", "error")
        return redirect(url_for("billing_page"))


@app.route("/billing/webhook", methods=["POST"])
def billing_webhook():
    """Stripe webhook: keep org plan/subscription state in sync."""
    if not billing.stripe_enabled():
        return {"ok": False}, 400
    import stripe
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    # Never trust an unsigned event. Without the webhook secret an attacker could
    # POST a forged checkout.session.completed to upgrade any org for free, so we
    # refuse to process webhooks at all until the secret is configured.
    if not billing.STRIPE_WEBHOOK_SECRET:
        app.logger.error(
            "Stripe webhook received but STRIPE_WEBHOOK_SECRET is not set; refusing to process."
        )
        return {"ok": False, "error": "webhook signature secret not configured"}, 503
    try:
        event = stripe.Webhook.construct_event(payload, sig, billing.STRIPE_WEBHOOK_SECRET)
    except Exception:
        return {"ok": False}, 400

    # Idempotency / replay guard: skip an event we've already processed.
    event_id = event.get("id")
    if event_id and db.session.get(ProcessedWebhookEvent, event_id):
        return {"ok": True, "duplicate": True}

    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    if etype == "checkout.session.completed":
        org_id = (obj.get("metadata") or {}).get("org_id") or obj.get("client_reference_id")
        org = db.session.get(Organization, org_id) if org_id else None
        if org:
            org.stripe_customer_id = obj.get("customer", org.stripe_customer_id)
            org.stripe_subscription_id = obj.get("subscription", "")
            org.plan = (obj.get("metadata") or {}).get("plan", org.plan)
            org.billing_status = "active"
            db.session.commit()
    elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = obj.get("id")
        org = Organization.query.filter_by(stripe_subscription_id=sub_id).first()
        if org:
            status = obj.get("status", "")
            org.billing_status = status
            if etype == "customer.subscription.deleted" or status in ("canceled", "unpaid"):
                org.plan = "free"
            db.session.commit()

    # Record the event as processed (best-effort) so replays are ignored.
    if event_id:
        db.session.add(ProcessedWebhookEvent(id=event_id))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    return {"ok": True}


@app.route("/admin/upload-company-template", methods=["POST"])
@login_required
def upload_company_template():
    if not current_user.is_admin:
        abort(403)

    file = request.files.get("template_file")
    vertical = request.form.get("vertical", "general")
    template_type = request.form.get("template_type", "proposal")

    if not file or not _allowed_file(file.filename, TEMPLATE_EXTENSIONS):
        flash("Please upload a Word or PDF file.", "error")
        return redirect(url_for("admin_panel"))

    safe, path, size = _save_upload(file, "company_templates")

    tmpl = UserVerticalTemplate(
        user_id=current_user.id,
        org_id=current_user.org_id,
        vertical=vertical,
        template_type=template_type,
        name=request.form.get("template_name", safe),
        file_path=path,
        original_filename=safe,
        is_company_default=True,
    )
    db.session.add(tmpl)
    db.session.commit()
    _log_activity("admin_template_upload", f"Company default {template_type} for {vertical}: {safe}")
    flash("Company template uploaded.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/delete-company-template/<template_id>", methods=["POST"])
@login_required
def delete_company_template(template_id):
    if not current_user.is_admin:
        abort(403)

    tmpl = db.session.get(UserVerticalTemplate, template_id)
    # Scope to the caller's org — an admin must not delete another tenant's template.
    if not tmpl or not tmpl.is_company_default or not _same_org(tmpl):
        abort(404)

    db.session.delete(tmpl)
    db.session.commit()
    flash("Company template deleted.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/toggle-admin/<user_id>", methods=["POST"])
@login_required
def toggle_admin(user_id):
    """Legacy route — redirects to update_user_role."""
    if not current_user.is_admin:
        abort(403)
    if user_id == current_user.id:
        flash("You cannot change your own role.", "error")
        return redirect(url_for("admin_panel"))
    user = db.session.get(User, user_id)
    # Scope to the caller's own organization — is_admin is a tenant admin, so an
    # admin must not be able to flip roles of users in another org.
    if not user or not _same_org(user):
        abort(404)
    user.is_admin = not user.is_admin
    user.role = "admin" if user.is_admin else "proposal"
    db.session.commit()
    flash(f"Role updated for {user.username}.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/update-role/<user_id>", methods=["POST"])
@login_required
def update_user_role(user_id):
    """Update a user's role (admin, sales, proposal)."""
    if not current_user.is_admin:
        abort(403)
    if user_id == current_user.id:
        flash("You cannot change your own role.", "error")
        return redirect(url_for("admin_panel"))

    user = db.session.get(User, user_id)
    # Scope to the caller's own organization (see toggle_admin).
    if not user or not _same_org(user):
        abort(404)

    new_role = request.form.get("role", "").strip().lower()
    if new_role not in ("admin", "sales", "proposal"):
        flash("Invalid role.", "error")
        return redirect(url_for("admin_panel"))

    user.role = new_role
    user.is_admin = (new_role == "admin")
    db.session.commit()
    _log_activity("role_change", f"Changed {user.username} role to {new_role}")
    flash(f"Role for {user.display_name or user.username} updated to {new_role.title()}.", "success")
    return redirect(url_for("admin_panel"))


# ---------------------------------------------------------------------------
# Document Library
# ---------------------------------------------------------------------------

@app.route("/documents")
@login_required
def document_library():
    """Full-featured document library with search, filter, stats, and proposals."""
    from sqlalchemy import func

    # Query params for search/filter
    q = request.args.get("q", "").strip()
    filter_type = request.args.get("type", "")
    filter_project = request.args.get("project", "")
    filter_tag = request.args.get("tag", "")
    filter_status = request.args.get("status", "")
    sort_by = request.args.get("sort", "date_desc")
    show_reference = request.args.get("reference", "")

    # All user projects for filter dropdown
    if current_user.is_admin:
        all_projects = Project.query.filter_by(org_id=current_user.org_id).order_by(Project.name).all()
    else:
        all_projects = Project.query.filter(_my_projects_filter()).order_by(Project.name).all()
    project_ids = [p.id for p in all_projects]

    # Base document query
    doc_query = ProjectDocument.query.filter(ProjectDocument.project_id.in_(project_ids))

    # Apply search
    if q:
        doc_query = doc_query.filter(
            db.or_(
                ProjectDocument.original_filename.ilike(f"%{q}%"),
                ProjectDocument.notes.ilike(f"%{q}%"),
            )
        )

    # Apply filters
    if filter_type:
        doc_query = doc_query.filter(ProjectDocument.file_type == filter_type)
    if filter_project:
        doc_query = doc_query.filter(ProjectDocument.project_id == filter_project)
    if show_reference:
        doc_query = doc_query.filter(ProjectDocument.is_reference == True)
    if filter_tag:
        tagged_ids = db.session.query(DocumentTag.document_id).filter(DocumentTag.tag == filter_tag).subquery()
        doc_query = doc_query.filter(ProjectDocument.id.in_(tagged_ids))
    if filter_status:
        status_project_ids = [p.id for p in all_projects if p.status == filter_status]
        doc_query = doc_query.filter(ProjectDocument.project_id.in_(status_project_ids))

    # Apply sort
    if sort_by == "name_asc":
        doc_query = doc_query.order_by(ProjectDocument.original_filename.asc())
    elif sort_by == "name_desc":
        doc_query = doc_query.order_by(ProjectDocument.original_filename.desc())
    elif sort_by == "size_desc":
        doc_query = doc_query.order_by(ProjectDocument.file_size.desc())
    elif sort_by == "size_asc":
        doc_query = doc_query.order_by(ProjectDocument.file_size.asc())
    elif sort_by == "date_asc":
        doc_query = doc_query.order_by(ProjectDocument.uploaded_at.asc())
    else:
        doc_query = doc_query.order_by(ProjectDocument.uploaded_at.desc())

    documents = doc_query.all()

    # Build project lookup
    project_map = {p.id: p for p in all_projects}

    # All unique tags for filter
    all_tags = db.session.query(DocumentTag.tag).join(ProjectDocument).filter(
        ProjectDocument.project_id.in_(project_ids)
    ).distinct().order_by(DocumentTag.tag).all()
    all_tags = [t[0] for t in all_tags]

    # Storage stats
    total_size = db.session.query(func.sum(ProjectDocument.file_size)).filter(
        ProjectDocument.project_id.in_(project_ids)
    ).scalar() or 0
    total_docs = ProjectDocument.query.filter(ProjectDocument.project_id.in_(project_ids)).count()

    # Per-project stats
    project_stats = []
    for p in all_projects:
        p_docs = ProjectDocument.query.filter_by(project_id=p.id).count()
        p_size = db.session.query(func.sum(ProjectDocument.file_size)).filter(
            ProjectDocument.project_id == p.id
        ).scalar() or 0
        if p_docs > 0:
            project_stats.append({"project": p, "doc_count": p_docs, "total_size": p_size})

    # Generated proposals
    proposals = Proposal.query.filter(
        Proposal.project_id.in_(project_ids)
    ).order_by(Proposal.generated_at.desc()).all()

    # Reference documents count
    ref_count = ProjectDocument.query.filter(
        ProjectDocument.project_id.in_(project_ids),
        ProjectDocument.is_reference == True,
    ).count()

    return render_template(
        "document_library.html",
        documents=documents,
        project_map=project_map,
        all_projects=all_projects,
        all_tags=all_tags,
        project_stats=project_stats,
        proposals=proposals,
        total_docs=total_docs,
        total_size=total_size,
        ref_count=ref_count,
        q=q,
        filter_type=filter_type,
        filter_project=filter_project,
        filter_tag=filter_tag,
        filter_status=filter_status,
        sort_by=sort_by,
        show_reference=show_reference,
    )


@app.route("/documents/<doc_id>/download")
@login_required
def download_document(doc_id):
    """Download a single document."""
    doc = db.session.get(ProjectDocument, doc_id)
    if not doc:
        abort(404)
    project = db.session.get(Project, doc.project_id)
    if not _can_access_project(project):
        abort(404)
    return send_file(doc.file_path, as_attachment=True, download_name=doc.original_filename)


@app.route("/documents/<doc_id>/preview")
@login_required
def preview_document(doc_id):
    """Preview a document inline (returns file for browser rendering)."""
    doc = db.session.get(ProjectDocument, doc_id)
    if not doc:
        abort(404)
    project = db.session.get(Project, doc.project_id)
    if not _can_access_project(project):
        abort(404)
    return send_file(doc.file_path, as_attachment=False)


@app.route("/documents/<doc_id>/tags", methods=["POST"])
@login_required
def update_document_tags(doc_id):
    """Add or update tags on a document."""
    doc = db.session.get(ProjectDocument, doc_id)
    if not doc:
        abort(404)
    project = db.session.get(Project, doc.project_id)
    if not _can_access_project(project):
        abort(404)

    tags_str = request.form.get("tags", "").strip()
    new_tags = [t.strip() for t in tags_str.split(",") if t.strip()]

    # Clear existing tags and set new ones
    DocumentTag.query.filter_by(document_id=doc.id).delete()
    for tag in new_tags:
        db.session.add(DocumentTag(document_id=doc.id, tag=tag[:100]))
    db.session.commit()
    flash(f"Tags updated for '{doc.original_filename}'.", "success")
    return redirect(url_for("document_library"))


@app.route("/documents/<doc_id>/notes", methods=["POST"])
@login_required
def update_document_notes(doc_id):
    """Update notes on a document."""
    doc = db.session.get(ProjectDocument, doc_id)
    if not doc:
        abort(404)
    project = db.session.get(Project, doc.project_id)
    if not _can_access_project(project):
        abort(404)

    doc.notes = request.form.get("notes", "").strip()
    db.session.commit()
    flash(f"Notes updated for '{doc.original_filename}'.", "success")
    return redirect(url_for("document_library"))


@app.route("/documents/<doc_id>/toggle-reference", methods=["POST"])
@login_required
def toggle_document_reference(doc_id):
    """Toggle a document as a reference document (available across projects)."""
    doc = db.session.get(ProjectDocument, doc_id)
    if not doc:
        abort(404)
    project = db.session.get(Project, doc.project_id)
    if not _can_access_project(project):
        abort(404)

    doc.is_reference = not doc.is_reference
    db.session.commit()
    status = "marked as reference" if doc.is_reference else "unmarked as reference"
    flash(f"'{doc.original_filename}' {status}.", "success")
    return redirect(url_for("document_library"))


@app.route("/documents/<doc_id>/copy-to-project", methods=["POST"])
@login_required
def copy_document_to_project(doc_id):
    """Copy a document to another project."""
    import shutil

    doc = db.session.get(ProjectDocument, doc_id)
    if not doc:
        abort(404)
    src_project = db.session.get(Project, doc.project_id)
    if not _can_access_project(src_project):
        abort(404)

    target_project_id = request.form.get("target_project_id", "").strip()
    target_project = db.session.get(Project, target_project_id)
    if not _can_access_project(target_project):
        flash("Invalid target project.", "error")
        return redirect(url_for("document_library"))

    # Copy the physical file
    src_path = Path(doc.file_path)
    if not src_path.exists():
        flash("Source file not found.", "error")
        return redirect(url_for("document_library"))

    dest_dir = UPLOADS_DIR / "projects" / target_project_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    new_filename = f"{uuid.uuid4().hex[:8]}_{doc.original_filename}"
    dest_path = dest_dir / new_filename
    shutil.copy2(str(src_path), str(dest_path))

    new_doc = ProjectDocument(
        project_id=target_project_id,
        filename=new_filename,
        original_filename=doc.original_filename,
        file_type=doc.file_type,
        file_path=str(dest_path),
        file_size=doc.file_size,
        notes=doc.notes,
        is_reference=doc.is_reference,
    )
    db.session.add(new_doc)

    # Copy tags
    for tag in doc.tags.all():
        db.session.add(DocumentTag(document_id=new_doc.id, tag=tag.tag))

    db.session.commit()
    _log_activity("document_copy", f"Copied '{doc.original_filename}' to project '{target_project.name}'")
    flash(f"Document copied to '{target_project.name}'.", "success")
    return redirect(url_for("document_library"))


@app.route("/documents/<doc_id>/version-label", methods=["POST"])
@login_required
def update_document_version(doc_id):
    """Update version label for a document."""
    doc = db.session.get(ProjectDocument, doc_id)
    if not doc:
        abort(404)
    project = db.session.get(Project, doc.project_id)
    if not _can_access_project(project):
        abort(404)

    doc.version_label = request.form.get("version_label", "").strip()
    if not doc.version_group:
        doc.version_group = uuid.uuid4().hex
    db.session.commit()
    flash(f"Version label updated for '{doc.original_filename}'.", "success")
    return redirect(url_for("document_library"))


@app.route("/documents/bulk-download", methods=["POST"])
@login_required
def bulk_download_documents():
    """Download all documents for a project as a ZIP."""
    import zipfile
    import tempfile

    project_id = request.form.get("project_id", "").strip()
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    docs = ProjectDocument.query.filter_by(project_id=project_id).all()
    if not docs:
        flash("No documents to download.", "error")
        return redirect(url_for("document_library"))

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc in docs:
            src = Path(doc.file_path)
            if src.exists():
                zf.write(str(src), doc.original_filename)
    tmp.close()

    safe_name = secure_filename(project.name) or "project"
    return send_file(tmp.name, as_attachment=True, download_name=f"{safe_name}_documents.zip")


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

@app.route("/notifications")
@login_required
def notifications_page():
    """View all notifications."""
    notes = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(100).all()
    # Mark all as read
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    return render_template("notifications.html", notifications=notes)


@app.route("/notifications/<notif_id>/read", methods=["POST"])
@login_required
def mark_notification_read(notif_id):
    n = db.session.get(Notification, notif_id)
    if n and n.user_id == current_user.id:
        n.is_read = True
        db.session.commit()
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def mark_all_notifications_read():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    flash("All notifications marked as read.", "success")
    return redirect(request.referrer or url_for("dashboard"))


# ---------------------------------------------------------------------------
# Team Assignments
# ---------------------------------------------------------------------------

@app.route("/projects/<project_id>/assign", methods=["POST"])
@login_required
def assign_project(project_id):
    """Assign a project to a proposal user."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    assignee_id = request.form.get("assigned_to", "").strip()
    if assignee_id:
        assignee = db.session.get(User, assignee_id)
        # The assignee must be a member of this org — assigning a foreign-org
        # user would grant them standing access to this project.
        if not assignee or not _same_org(assignee):
            flash("User not found.", "error")
            return redirect(request.referrer or url_for("dashboard"))
        project.assigned_to = assignee_id
        db.session.commit()
        _log_activity("project_assign", f"Assigned '{project.name}' to {assignee.display_name or assignee.username}", project_id=project.id)
        _notify(
            assignee_id,
            "assignment",
            f"Project assigned to you: {project.name}",
            f"{current_user.display_name or current_user.username} assigned you to project '{project.name}' ({project.client_name or 'no client'}).",
            link=f"/projects/{project.id}",
        )
        flash(f"Project assigned to {assignee.display_name or assignee.username}.", "success")
    else:
        project.assigned_to = None
        db.session.commit()
        flash("Assignment removed.", "success")

    return redirect(request.referrer or url_for("dashboard"))


# ---------------------------------------------------------------------------
# Calendar & Deadlines (Part 2)
# ---------------------------------------------------------------------------

def _my_projects_filter():
    """Filter for projects the current user owns or is assigned to."""
    return db.or_(
        Project.user_id == current_user.id,
        Project.assigned_to == current_user.id,
    )


@app.route("/calendar")
@login_required
def calendar_view():
    """Calendar view showing project deadlines for the current month (or requested month)."""
    # Parse year/month from query string; default to current month
    now = datetime.now(timezone.utc)
    try:
        year = int(request.args.get("year", now.year))
        month = int(request.args.get("month", now.month))
        if month < 1 or month > 12:
            month = now.month
    except ValueError:
        year, month = now.year, now.month

    import calendar as _cal
    first_weekday, days_in_month = _cal.monthrange(year, month)

    # Load projects with a due date (owned or assigned)
    if current_user.is_admin:
        all_with_due = Project.query.filter_by(org_id=current_user.org_id).filter(
            Project.due_date.isnot(None)).all()
    else:
        all_with_due = Project.query.filter(
            _my_projects_filter(), Project.due_date.isnot(None)
        ).all()

    # Bucket projects by day-of-month for the requested month
    by_day = {}
    for p in all_with_due:
        if p.due_date and p.due_date.year == year and p.due_date.month == month:
            by_day.setdefault(p.due_date.day, []).append(p)

    # Previous/next month nav
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)

    # Upcoming deadlines (next 14 days) and overdue
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    window_end = today + timedelta(days=14)
    upcoming = [p for p in all_with_due if p.due_date and p.due_date.replace(tzinfo=None) >= today.replace(tzinfo=None) and p.due_date.replace(tzinfo=None) <= window_end.replace(tzinfo=None) and p.status == "active"]
    upcoming.sort(key=lambda p: p.due_date)
    overdue = [p for p in all_with_due if p.due_date and p.due_date.replace(tzinfo=None) < today.replace(tzinfo=None) and p.status == "active"]
    overdue.sort(key=lambda p: p.due_date)

    month_name = _cal.month_name[month]

    return render_template(
        "calendar.html",
        year=year,
        month=month,
        month_name=month_name,
        first_weekday=first_weekday,
        days_in_month=days_in_month,
        by_day=by_day,
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        upcoming=upcoming,
        overdue=overdue,
        today_day=now.day if (year == now.year and month == now.month) else None,
    )


# ---------------------------------------------------------------------------
# Reports & Analytics (Part 2)
# ---------------------------------------------------------------------------

@app.route("/reports")
@login_required
def reports():
    """Win/loss analysis, pipeline trends, vertical performance."""
    from sqlalchemy import func
    from collections import OrderedDict, Counter

    # Admins see company-wide; other users see own + assigned
    if current_user.is_admin:
        base_query = Project.query.filter_by(org_id=current_user.org_id)
    else:
        base_query = Project.query.filter(_my_projects_filter())

    all_projects = base_query.all()

    # Overall stats
    won_projects = [p for p in all_projects if p.status == "won"]
    lost_projects = [p for p in all_projects if p.status == "lost"]
    total_won = len(won_projects)
    total_lost = len(lost_projects)
    total_decided = total_won + total_lost
    overall_win_rate = round((total_won / total_decided) * 100) if total_decided else 0
    won_value = sum(p.dollar_amount or 0 for p in won_projects)
    lost_value = sum(p.dollar_amount or 0 for p in lost_projects)

    # Win/loss by vertical
    vertical_stats = {}
    for p in all_projects:
        label = p.vertical_label or "General"
        vs = vertical_stats.setdefault(label, {"won": 0, "lost": 0, "won_value": 0, "lost_value": 0, "active": 0})
        if p.status == "won":
            vs["won"] += 1
            vs["won_value"] += p.dollar_amount or 0
        elif p.status == "lost":
            vs["lost"] += 1
            vs["lost_value"] += p.dollar_amount or 0
        elif p.status == "active":
            vs["active"] += 1

    for label, vs in vertical_stats.items():
        decided = vs["won"] + vs["lost"]
        vs["win_rate"] = round((vs["won"] / decided) * 100) if decided else 0
        vs["total_value"] = vs["won_value"] + vs["lost_value"]

    # Top competitors
    competitor_counter = Counter()
    competitor_wins = Counter()
    for p in won_projects + lost_projects:
        if p.competitor_name:
            competitor_counter[p.competitor_name] += 1
            if p.status == "won":
                competitor_wins[p.competitor_name] += 1
    top_competitors = []
    for name, total in competitor_counter.most_common(10):
        wins = competitor_wins.get(name, 0)
        losses = total - wins
        top_competitors.append({
            "name": name,
            "total": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / total) * 100) if total else 0,
        })

    # Close reason categories breakdown
    category_labels = {
        "price": "Price",
        "scope": "Scope",
        "schedule": "Schedule / Timing",
        "relationship": "Relationship / Incumbent",
        "technical": "Technical Approach",
        "compliance": "Compliance / Requirements",
        "other": "Other",
    }
    won_category_counts = {k: 0 for k in category_labels}
    lost_category_counts = {k: 0 for k in category_labels}
    for p in won_projects:
        key = p.close_category or "other"
        if key in won_category_counts:
            won_category_counts[key] += 1
    for p in lost_projects:
        key = p.close_category or "other"
        if key in lost_category_counts:
            lost_category_counts[key] += 1

    # Monthly trend (last 6 months of closures)
    from datetime import timedelta
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    trend = OrderedDict()
    for i in range(5, -1, -1):
        # Approximate by calendar month
        m = now.month - i
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        key = f"{y}-{m:02d}"
        trend[key] = {"won": 0, "lost": 0, "won_value": 0, "lost_value": 0}

    for p in won_projects + lost_projects:
        when = p.closed_at or p.submitted_at or p.updated_at
        if not when:
            continue
        when = when.replace(tzinfo=None) if when.tzinfo else when
        key = f"{when.year}-{when.month:02d}"
        if key in trend:
            if p.status == "won":
                trend[key]["won"] += 1
                trend[key]["won_value"] += p.dollar_amount or 0
            else:
                trend[key]["lost"] += 1
                trend[key]["lost_value"] += p.dollar_amount or 0

    # Recently closed projects with missing details (prompt user to fill in)
    missing_details = [
        p for p in (won_projects + lost_projects)
        if not (p.close_reason or p.close_category or p.competitor_name)
    ]
    missing_details.sort(key=lambda p: p.closed_at or p.updated_at or now, reverse=True)

    stats = {
        "total_projects": len(all_projects),
        "total_active": sum(1 for p in all_projects if p.status == "active"),
        "total_submitted": sum(1 for p in all_projects if p.status == "submitted"),
        "total_won": total_won,
        "total_lost": total_lost,
        "win_rate": overall_win_rate,
        "won_value": won_value,
        "lost_value": lost_value,
    }

    return render_template(
        "reports.html",
        stats=stats,
        vertical_stats=vertical_stats,
        top_competitors=top_competitors,
        won_category_counts=won_category_counts,
        lost_category_counts=lost_category_counts,
        category_labels=category_labels,
        trend=trend,
        missing_details=missing_details[:10],
    )


# ---------------------------------------------------------------------------
# Global Search (Part 2)
# ---------------------------------------------------------------------------

@app.route("/search")
@login_required
def search():
    """Unified search across projects, proposals, and documents."""
    q = request.args.get("q", "").strip()
    results = {"projects": [], "proposals": [], "documents": [], "comments": []}

    if not q or len(q) < 2:
        return render_template("search.html", q=q, results=results, total=0)

    like = f"%{q}%"

    # Projects (owned or assigned)
    proj_query = Project.query.filter(
        db.or_(
            Project.name.ilike(like),
            Project.client_name.ilike(like),
            Project.close_reason.ilike(like),
            Project.competitor_name.ilike(like),
        )
    )
    if current_user.is_admin:
        proj_query = proj_query.filter(Project.org_id == current_user.org_id)
    else:
        proj_query = proj_query.filter(_my_projects_filter())
    results["projects"] = proj_query.order_by(Project.updated_at.desc()).limit(25).all()

    # Documents (under user's projects, or admin sees all)
    if current_user.is_admin:
        accessible_project_ids = [
            p.id for p in Project.query.filter_by(org_id=current_user.org_id).all()
        ]
    else:
        accessible_project_ids = [
            p.id for p in Project.query.filter(_my_projects_filter()).all()
        ]
    doc_query = ProjectDocument.query.filter(
        ProjectDocument.project_id.in_(accessible_project_ids),
        db.or_(
            ProjectDocument.original_filename.ilike(like),
            ProjectDocument.notes.ilike(like),
            ProjectDocument.version_label.ilike(like),
        ),
    )
    results["documents"] = doc_query.order_by(ProjectDocument.uploaded_at.desc()).limit(25).all()

    # Proposals — search within markdown content of their latest version
    prop_query = Proposal.query.filter(Proposal.project_id.in_(accessible_project_ids))
    proposal_matches = []
    for prop in prop_query.limit(200).all():
        latest = ProposalVersion.query.filter_by(proposal_id=prop.id).order_by(
            ProposalVersion.version_number.desc()
        ).first()
        if latest and q.lower() in (latest.markdown_content or "").lower():
            # Extract a small snippet around the match
            content = latest.markdown_content
            lower = content.lower()
            idx = lower.find(q.lower())
            start = max(0, idx - 60)
            end = min(len(content), idx + len(q) + 60)
            snippet = content[start:end].replace("\n", " ")
            if start > 0:
                snippet = "…" + snippet
            if end < len(content):
                snippet = snippet + "…"
            proposal_matches.append({"proposal": prop, "snippet": snippet})
    results["proposals"] = proposal_matches[:25]

    # Comments — always scoped to accessible (org) proposals
    if True:
        accessible_proposal_ids = [
            p.id for p in Proposal.query.filter(Proposal.project_id.in_(accessible_project_ids)).all()
        ]
        comment_query = ProposalComment.query.filter(
            ProposalComment.proposal_id.in_(accessible_proposal_ids),
            ProposalComment.body.ilike(like),
        )
    results["comments"] = comment_query.order_by(ProposalComment.created_at.desc()).limit(25).all()

    total = (
        len(results["projects"])
        + len(results["proposals"])
        + len(results["documents"])
        + len(results["comments"])
    )

    # Build project lookup for display
    project_lookup = {p.id: p for p in Project.query.filter(
        Project.id.in_(accessible_project_ids)
    ).all()}

    return render_template(
        "search.html",
        q=q,
        results=results,
        total=total,
        project_lookup=project_lookup,
    )


# ---------------------------------------------------------------------------
# Proposal Comments (Part 2)
# ---------------------------------------------------------------------------

@app.route("/proposal/<proposal_id>/comments", methods=["POST"])
@login_required
def add_proposal_comment(proposal_id):
    """Add a review comment to a proposal."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    body = request.form.get("body", "").strip()
    section_anchor = request.form.get("section_anchor", "").strip()
    if not body:
        flash("Comment cannot be empty.", "error")
        return redirect(request.referrer or url_for("view_proposal", proposal_id=proposal_id))

    comment = ProposalComment(
        proposal_id=proposal_id,
        author_id=current_user.id,
        body=body,
        section_anchor=section_anchor,
    )
    db.session.add(comment)
    db.session.commit()
    _log_activity("proposal_comment_add", f"Comment on {proposal.job_id}", project.id)

    # Notify owner/assignee if they are not the author
    notify_ids = set()
    if project.user_id != current_user.id:
        notify_ids.add(project.user_id)
    if project.assigned_to and project.assigned_to != current_user.id:
        notify_ids.add(project.assigned_to)
    for uid in notify_ids:
        _notify(
            uid,
            "proposal_comment",
            f"New comment on {project.name}",
            f"{current_user.display_name or current_user.username}: {body[:140]}",
            link=f"/proposal/{proposal_id}#comment-{comment.id}",
        )

    flash("Comment posted.", "success")
    return redirect(request.referrer or url_for("view_proposal", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/comments/<comment_id>/resolve", methods=["POST"])
@login_required
def resolve_proposal_comment(proposal_id, comment_id):
    """Mark a comment as resolved or unresolve it."""
    comment = db.session.get(ProposalComment, comment_id)
    if not comment or comment.proposal_id != proposal_id:
        abort(404)
    proposal = db.session.get(Proposal, proposal_id)
    project = db.session.get(Project, proposal.project_id) if proposal else None
    if not _can_access_project(project):
        abort(404)

    if comment.is_resolved:
        comment.is_resolved = False
        comment.resolved_by = None
        comment.resolved_at = None
    else:
        comment.is_resolved = True
        comment.resolved_by = current_user.id
        comment.resolved_at = datetime.now(timezone.utc)
    db.session.commit()
    return redirect(request.referrer or url_for("view_proposal", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/comments/<comment_id>/delete", methods=["POST"])
@login_required
def delete_proposal_comment(proposal_id, comment_id):
    """Delete a comment (author or admin only)."""
    comment = db.session.get(ProposalComment, comment_id)
    if not comment or comment.proposal_id != proposal_id:
        abort(404)
    # Enforce tenant isolation first: the caller must be able to access the
    # parent proposal's project (org-scoped) before any author/admin check.
    proposal = db.session.get(Proposal, proposal_id)
    project = db.session.get(Project, proposal.project_id) if proposal else None
    if not _can_access_project(project):
        abort(404)
    if comment.author_id != current_user.id and not current_user.is_admin:
        abort(403)
    db.session.delete(comment)
    db.session.commit()
    flash("Comment deleted.", "success")
    return redirect(request.referrer or url_for("view_proposal", proposal_id=proposal_id))

# Clarification Register (Phase 1)
# ---------------------------------------------------------------------------

@app.route("/projects/<project_id>/clarifications")
@login_required
def clarification_register(project_id):
    """View the clarification register for a project."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    filter_status = request.args.get("status", "")
    filter_path = request.args.get("path", "")
    filter_category = request.args.get("category", "")

    query = ClarificationItem.query.filter_by(project_id=project_id)
    if filter_status:
        query = query.filter_by(status=filter_status)
    if filter_path:
        query = query.filter_by(resolution_path=filter_path)
    if filter_category:
        query = query.filter_by(category=filter_category)

    items = query.order_by(ClarificationItem.created_at.desc()).all()

    # Stats
    total = ClarificationItem.query.filter_by(project_id=project_id).count()
    open_count = ClarificationItem.query.filter_by(project_id=project_id, status="open").count()
    resolved_count = ClarificationItem.query.filter_by(project_id=project_id, status="resolved").count()
    customer_count = ClarificationItem.query.filter_by(project_id=project_id, resolution_path="customer").count()
    parking_lot_count = ClarificationItem.query.filter_by(project_id=project_id, is_parking_lot=True).count()

    # Confidence impact (Phase 4)
    proposal = Proposal.query.filter_by(project_id=project_id).order_by(Proposal.generated_at.desc()).first()
    unresolved_impact = sum(
        ci.confidence_impact for ci in
        ClarificationItem.query.filter_by(project_id=project_id).filter(
            ClarificationItem.status.in_(["open", "draft", "sent"])
        ).all()
    )

    users = _org_users_query().order_by(User.display_name).all()

    return render_template(
        "clarification_register.html",
        project=project, items=items, proposal=proposal,
        total=total, open_count=open_count, resolved_count=resolved_count,
        customer_count=customer_count, parking_lot_count=parking_lot_count,
        unresolved_impact=unresolved_impact, users=users,
        filter_status=filter_status, filter_path=filter_path,
        filter_category=filter_category,
    )


@app.route("/projects/<project_id>/clarifications/add", methods=["POST"])
@login_required
def add_clarification(project_id):
    """Manually add a clarification item."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    ci = ClarificationItem(
        project_id=project_id,
        source="human_review",
        resolution_path=request.form.get("resolution_path", "internal"),
        category=request.form.get("category", "general"),
        priority=request.form.get("priority", "medium"),
        question=request.form.get("question", "").strip(),
        context=request.form.get("context", "").strip(),
        proposal_section=request.form.get("proposal_section", "").strip(),
        assigned_to_role=request.form.get("assigned_to_role", ""),
        assigned_to_user_id=request.form.get("assigned_to_user_id") or None,
        is_parking_lot=request.form.get("is_parking_lot") == "1",
        status="open",
        created_by=current_user.id,
    )

    # Link to latest proposal if exists
    proposal = Proposal.query.filter_by(project_id=project_id).order_by(Proposal.generated_at.desc()).first()
    if proposal:
        ci.proposal_id = proposal.id

    db.session.add(ci)

    # Update project sub-status
    project.clarification_sub_status = "clarification_pending"
    db.session.commit()

    _log_activity("clarification_add", f"Added clarification: {ci.question[:80]}", project_id)

    # Notify assignee if assigned
    if ci.assigned_to_user_id:
        _notify(ci.assigned_to_user_id, "assignment",
                f"Clarification assigned: {project.name}",
                f"You have been assigned a clarification question on '{project.name}': {ci.question[:100]}",
                link=f"/projects/{project_id}/clarifications")

    flash("Clarification item added.", "success")
    return redirect(url_for("clarification_register", project_id=project_id))


@app.route("/clarifications/<item_id>/respond", methods=["POST"])
@login_required
def respond_clarification(item_id):
    """Submit a response to a clarification item."""
    ci = db.session.get(ClarificationItem, item_id)
    if not ci:
        abort(404)
    project = db.session.get(Project, ci.project_id)
    if not _can_access_project(project):
        abort(404)

    ci.response = request.form.get("response", "").strip()
    ci.responded_by = current_user.id
    ci.responded_at = datetime.now(timezone.utc)
    ci.status = "response_received"
    db.session.commit()

    _log_activity("clarification_respond", f"Responded to clarification: {ci.question[:60]}", ci.project_id)
    flash("Response recorded.", "success")
    return redirect(url_for("clarification_register", project_id=ci.project_id))


@app.route("/clarifications/<item_id>/resolve", methods=["POST"])
@login_required
def resolve_clarification(item_id):
    """Mark a clarification item as resolved/incorporated."""
    ci = db.session.get(ClarificationItem, item_id)
    if not ci:
        abort(404)
    project = db.session.get(Project, ci.project_id)
    if not _can_access_project(project):
        abort(404)

    ci.status = "resolved"
    ci.incorporated_at = datetime.now(timezone.utc)
    db.session.commit()

    # Check if all items resolved — clear sub-status
    remaining = ClarificationItem.query.filter_by(project_id=ci.project_id).filter(
        ClarificationItem.status.in_(["open", "draft", "sent", "response_received"])
    ).count()
    if remaining == 0:
        project.clarification_sub_status = "none"
        db.session.commit()

    _log_activity("clarification_resolve", f"Resolved clarification: {ci.question[:60]}", ci.project_id)
    flash("Clarification resolved.", "success")
    return redirect(url_for("clarification_register", project_id=ci.project_id))


@app.route("/clarifications/<item_id>/parking-lot", methods=["POST"])
@login_required
def toggle_parking_lot(item_id):
    """Toggle parking lot status for a clarification item (Phase 4)."""
    ci = db.session.get(ClarificationItem, item_id)
    if not ci:
        abort(404)
    if not _can_access_project(db.session.get(Project, ci.project_id)):
        abort(404)

    ci.is_parking_lot = not ci.is_parking_lot
    db.session.commit()
    status = "moved to parking lot" if ci.is_parking_lot else "removed from parking lot"
    flash(f"Clarification {status}.", "success")
    return redirect(url_for("clarification_register", project_id=ci.project_id))


@app.route("/clarifications/<item_id>/update", methods=["POST"])
@login_required
def update_clarification(item_id):
    """Update a clarification item's fields."""
    ci = db.session.get(ClarificationItem, item_id)
    if not ci:
        abort(404)
    if not _can_access_project(db.session.get(Project, ci.project_id)):
        abort(404)

    ci.priority = request.form.get("priority", ci.priority)
    ci.category = request.form.get("category", ci.category)
    ci.resolution_path = request.form.get("resolution_path", ci.resolution_path)
    ci.assigned_to_user_id = request.form.get("assigned_to_user_id") or ci.assigned_to_user_id
    ci.assigned_to_role = request.form.get("assigned_to_role", ci.assigned_to_role)
    ci.confidence_impact = int(request.form.get("confidence_impact", ci.confidence_impact) or 0)
    db.session.commit()

    flash("Clarification updated.", "success")
    return redirect(url_for("clarification_register", project_id=ci.project_id))


# ---------------------------------------------------------------------------
# Review Comments & Cycles (Phase 2)
# ---------------------------------------------------------------------------

@app.route("/proposal/<proposal_id>/reviews")
@login_required
def proposal_reviews(proposal_id):
    """View review comments and cycles for a proposal."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    # Get or create active review cycle
    active_cycle = ReviewCycle.query.filter_by(
        proposal_id=proposal_id, status="active"
    ).first()

    cycles = ReviewCycle.query.filter_by(proposal_id=proposal_id).order_by(
        ReviewCycle.cycle_number.desc()
    ).all()

    # Comments for active cycle (or all if no cycles)
    if active_cycle:
        comments = ReviewComment.query.filter_by(
            proposal_id=proposal_id, review_cycle_id=active_cycle.id
        ).order_by(ReviewComment.created_at.desc()).all()
    else:
        comments = ReviewComment.query.filter_by(
            proposal_id=proposal_id
        ).order_by(ReviewComment.created_at.desc()).all()

    # Stats
    total_comments = len(comments)
    open_comments = sum(1 for c in comments if c.status == "open")
    questions = sum(1 for c in comments if c.comment_type == "question" and c.status == "open")
    change_requests = sum(1 for c in comments if c.comment_type == "change_request" and c.status == "open")
    approvals = sum(1 for c in comments if c.comment_type == "approval")

    # Extract section headings from proposal for dropdown
    md_path = GENERATED_DIR / proposal.md_file
    sections = []
    if md_path.exists():
        for line in md_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("## ") or stripped.startswith("### "):
                sections.append(stripped)

    users = _org_users_query().order_by(User.display_name).all()

    return render_template(
        "proposal_reviews.html",
        proposal=proposal, project=project,
        active_cycle=active_cycle, cycles=cycles,
        comments=comments, sections=sections, users=users,
        total_comments=total_comments, open_comments=open_comments,
        questions=questions, change_requests=change_requests, approvals=approvals,
    )


@app.route("/proposal/<proposal_id>/reviews/start-cycle", methods=["POST"])
@login_required
def start_review_cycle(proposal_id):
    """Start a new review cycle."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    # Complete any active cycle
    active = ReviewCycle.query.filter_by(proposal_id=proposal_id, status="active").first()
    if active:
        active.status = "completed"
        active.completed_at = datetime.now(timezone.utc)

    # Get next cycle number
    max_cycle = db.session.query(db.func.max(ReviewCycle.cycle_number)).filter_by(
        proposal_id=proposal_id
    ).scalar() or 0

    cycle = ReviewCycle(
        proposal_id=proposal_id,
        cycle_number=max_cycle + 1,
        name=request.form.get("cycle_name", f"Review {max_cycle + 1}"),
        status="active",
        started_by=current_user.id,
    )
    db.session.add(cycle)

    project.clarification_sub_status = "in_review"
    db.session.commit()

    _log_activity("review_cycle_start", f"Started review cycle {cycle.cycle_number}", project.id)
    flash(f"Review cycle '{cycle.name}' started.", "success")
    return redirect(url_for("proposal_reviews", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/reviews/complete-cycle", methods=["POST"])
@login_required
def complete_review_cycle(proposal_id):
    """Complete the active review cycle."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    active = ReviewCycle.query.filter_by(proposal_id=proposal_id, status="active").first()
    if active:
        active.status = "completed"
        active.completed_at = datetime.now(timezone.utc)
        project.clarification_sub_status = "none"
        db.session.commit()
        _log_activity("review_cycle_complete", f"Completed review cycle {active.cycle_number}", project.id)
        flash(f"Review cycle '{active.name}' completed.", "success")

    return redirect(url_for("proposal_reviews", proposal_id=proposal_id))


@app.route("/proposal/<proposal_id>/reviews/add-comment", methods=["POST"])
@login_required
def add_review_comment(proposal_id):
    """Add a review comment to a proposal."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    active_cycle = ReviewCycle.query.filter_by(
        proposal_id=proposal_id, status="active"
    ).first()

    comment = ReviewComment(
        proposal_id=proposal_id,
        review_cycle_id=active_cycle.id if active_cycle else None,
        section_heading=request.form.get("section_heading", ""),
        line_reference=request.form.get("line_reference", "").strip(),
        comment_type=request.form.get("comment_type", "comment"),
        content=request.form.get("content", "").strip(),
        author_id=current_user.id,
        assigned_to_user_id=request.form.get("assigned_to_user_id") or None,
        assigned_to_role=request.form.get("assigned_to_role", ""),
        status="open",
    )
    db.session.add(comment)
    db.session.flush()

    # If it's a question or change_request, also create a ClarificationItem
    if comment.comment_type in ("question", "change_request"):
        ci = ClarificationItem(
            project_id=project.id,
            proposal_id=proposal_id,
            source="human_review",
            resolution_path="internal",
            category="general",
            question=comment.content,
            proposal_section=comment.section_heading,
            assigned_to_user_id=comment.assigned_to_user_id,
            assigned_to_role=comment.assigned_to_role,
            status="open",
            created_by=current_user.id,
        )
        db.session.add(ci)
        db.session.flush()
        comment.clarification_item_id = ci.id
        project.clarification_sub_status = "in_review"

    db.session.commit()

    # Notify assignee
    if comment.assigned_to_user_id and comment.assigned_to_user_id != current_user.id:
        type_label = comment.comment_type.replace("_", " ").title()
        _notify(comment.assigned_to_user_id, "assignment",
                f"Review {type_label}: {project.name}",
                f"{current_user.display_name or current_user.username} left a {type_label} on '{project.name}': {comment.content[:100]}",
                link=f"/proposal/{proposal_id}/reviews")

    _log_activity("review_comment_add", f"Added {comment.comment_type} on {comment.section_heading or 'proposal'}", project.id)
    flash("Review comment added.", "success")
    return redirect(url_for("proposal_reviews", proposal_id=proposal_id))


@app.route("/reviews/<comment_id>/resolve", methods=["POST"])
@login_required
def resolve_review_comment(comment_id):
    """Resolve a review comment."""
    comment = db.session.get(ReviewComment, comment_id)
    if not comment:
        abort(404)
    proposal = db.session.get(Proposal, comment.proposal_id)
    if not _can_access_project(db.session.get(Project, proposal.project_id)):
        abort(404)

    comment.status = "resolved"
    comment.resolution_note = request.form.get("resolution_note", "").strip()
    comment.resolved_by = current_user.id
    comment.resolved_at = datetime.now(timezone.utc)

    # Also resolve linked clarification item
    if comment.clarification_item_id:
        ci = db.session.get(ClarificationItem, comment.clarification_item_id)
        if ci:
            ci.status = "resolved"
            ci.incorporated_at = datetime.now(timezone.utc)

    db.session.commit()

    _log_activity("review_comment_resolve", f"Resolved {comment.comment_type}", proposal.project_id)
    flash("Comment resolved.", "success")
    return redirect(url_for("proposal_reviews", proposal_id=comment.proposal_id))


# ---------------------------------------------------------------------------
# RFI Letter Export (Phase 3)
# ---------------------------------------------------------------------------

@app.route("/projects/<project_id>/rfi/generate", methods=["POST"])
@login_required
def generate_rfi_letter(project_id):
    """Generate and download an RFI/Clarification letter from customer-facing items."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    customer_items = ClarificationItem.query.filter_by(
        project_id=project_id, resolution_path="customer"
    ).filter(
        ClarificationItem.status.in_(["open", "draft"])
    ).order_by(ClarificationItem.category, ClarificationItem.created_at).all()

    if not customer_items:
        flash("No customer-facing clarification items to include in RFI.", "error")
        return redirect(url_for("clarification_register", project_id=project_id))

    # Assign RFI reference IDs
    for i, ci in enumerate(customer_items, 1):
        ci.rfi_reference_id = f"RFI-{i:03d}"
        ci.status = "sent"
        ci.rfi_sent_at = datetime.now(timezone.utc)

    project.clarification_sub_status = "rfi_sent"
    db.session.commit()

    # Generate DOCX
    company_name = current_user.company_name or "Our Company"
    rfi_filename = f"rfi_letter_{project_id[:8]}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.docx"
    rfi_path = GENERATED_DIR / rfi_filename

    markdown_to_rfi_docx(
        items=customer_items,
        project_name=project.name,
        client_name=project.client_name,
        company_name=company_name,
        author=current_user.display_name or current_user.username,
        output_path=str(rfi_path),
    )

    _log_activity("rfi_generate", f"Generated RFI letter with {len(customer_items)} item(s)", project_id)
    return send_file(str(rfi_path), as_attachment=True, download_name=rfi_filename,
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.route("/projects/<project_id>/rfi/record-response/<item_id>", methods=["POST"])
@login_required
def record_rfi_response(project_id, item_id):
    """Record a customer's response to an RFI item."""
    ci = db.session.get(ClarificationItem, item_id)
    if not ci or ci.project_id != project_id:
        abort(404)
    if not _can_access_project(db.session.get(Project, ci.project_id)):
        abort(404)

    ci.response = request.form.get("response", "").strip()
    ci.responded_by = current_user.id
    ci.responded_at = datetime.now(timezone.utc)
    ci.status = "response_received"
    db.session.commit()

    # Check if all RFI items have responses
    pending_rfi = ClarificationItem.query.filter_by(
        project_id=project_id, resolution_path="customer", status="sent"
    ).count()
    if pending_rfi == 0:
        project = db.session.get(Project, project_id)
        project.clarification_sub_status = "clarification_pending"
        db.session.commit()

    _log_activity("rfi_response", f"Recorded response for {ci.rfi_reference_id}", project_id)
    flash(f"Response recorded for {ci.rfi_reference_id}.", "success")
    return redirect(url_for("clarification_register", project_id=project_id))


# ---------------------------------------------------------------------------
# Addendum Impact Analysis (Phase 3)
# ---------------------------------------------------------------------------

@app.route("/projects/<project_id>/addendum-analysis", methods=["POST"])
@login_required
def addendum_analysis(project_id):
    """Analyze a newly uploaded addendum against existing proposal."""
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    addendum_doc_id = request.form.get("addendum_doc_id")
    addendum_doc = db.session.get(ProjectDocument, addendum_doc_id)
    if not addendum_doc:
        flash("Addendum document not found.", "error")
        return redirect(url_for("project_upload", project_id=project_id))

    # Get original RFP text
    rfp_docs = ProjectDocument.query.filter_by(project_id=project_id, file_type="rfp").all()
    original_rfp_text = ""
    for doc in rfp_docs:
        if doc.id != addendum_doc_id:
            try:
                original_rfp_text += parse_document(doc.file_path) + "\n"
            except Exception:
                continue

    # Get addendum text
    try:
        addendum_text = parse_document(addendum_doc.file_path)
    except Exception:
        flash("Could not parse addendum document.", "error")
        return redirect(url_for("project_upload", project_id=project_id))

    # Get current proposal
    proposal = Proposal.query.filter_by(project_id=project_id).order_by(Proposal.generated_at.desc()).first()
    current_md = ""
    if proposal:
        md_path = GENERATED_DIR / proposal.md_file
        if md_path.exists():
            current_md = md_path.read_text(encoding="utf-8")

    if not current_md:
        flash("No existing proposal to analyze against.", "error")
        return redirect(url_for("project_upload", project_id=project_id))

    try:
        result = analyze_addendum_impact(
            original_rfp_text, addendum_text, current_md,
            user_api_key=decrypt_api_key(current_user.api_key_encrypted) or None,
            user_model=current_user.llm_model or None,
        )

        # Create ClarificationItems for each identified change
        for change in result.get("changes", []):
            ci = ClarificationItem(
                project_id=project_id,
                proposal_id=proposal.id if proposal else None,
                source="addendum",
                resolution_path="internal" if change.get("can_ai_resolve") else "internal",
                category="scope",
                priority=change.get("severity", "medium"),
                question=change.get("addendum_item", ""),
                context=change.get("impact_description", ""),
                ai_suggestion=change.get("suggested_resolution", ""),
                proposal_section=", ".join(change.get("affected_sections", [])),
                status="open",
                created_by=current_user.id,
            )
            db.session.add(ci)

        project.clarification_sub_status = "clarification_pending"
        db.session.commit()

        _log_activity("addendum_analysis", f"Analyzed addendum: {len(result.get('changes', []))} impacts found", project_id)
        flash(f"Addendum analysis complete: {len(result.get('changes', []))} impact(s) identified and added to clarification register.", "success")

    except Exception as e:
        flash(f"Error analyzing addendum: {friendly_api_error(e)}", "error")

    return redirect(url_for("clarification_register", project_id=project_id))


# ---------------------------------------------------------------------------
# Targeted Section Regeneration (Phase 4)
# ---------------------------------------------------------------------------

@app.route("/proposal/<proposal_id>/regenerate-section", methods=["POST"])
@login_required
def regenerate_proposal_section(proposal_id):
    """Regenerate a specific section of the proposal with new clarification info."""
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not _can_access_project(project):
        abort(404)

    section_heading = request.form.get("section_heading", "").strip()
    clarification_answer = request.form.get("clarification_answer", "").strip()

    if not section_heading or not clarification_answer:
        flash("Section heading and clarification info are required.", "error")
        return redirect(url_for("edit_proposal", proposal_id=proposal_id))

    # Get current proposal content
    md_path = GENERATED_DIR / proposal.md_file
    current_md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""

    # Get original RFP text for context
    rfp_docs = ProjectDocument.query.filter_by(project_id=proposal.project_id, file_type="rfp").all()
    rfp_text = ""
    for doc in rfp_docs:
        try:
            rfp_text += parse_document(doc.file_path) + "\n"
        except Exception:
            continue

    try:
        result = regenerate_section(
            full_proposal_md=current_md,
            section_heading=section_heading,
            clarification_answer=clarification_answer,
            original_rfp_text=rfp_text,
            company_name=current_user.company_name,
            user_api_key=decrypt_api_key(current_user.api_key_encrypted) or None,
            user_model=current_user.llm_model or None,
        )

        # Replace the section in the full proposal
        new_section = result["section_markdown"]
        section_pattern = re.escape(section_heading)
        updated_md = re.sub(
            rf"({section_pattern}.*?)(?=\n## |\Z)",
            new_section + "\n\n",
            current_md,
            count=1,
            flags=re.DOTALL,
        )

        # Save as new version
        latest = ProposalVersion.query.filter_by(proposal_id=proposal_id).order_by(
            ProposalVersion.version_number.desc()
        ).first()
        next_version = (latest.version_number + 1) if latest else 1

        version = ProposalVersion(
            proposal_id=proposal_id,
            version_number=next_version,
            markdown_content=updated_md,
            edit_source="ai",
            editor_id=current_user.id,
            change_summary=f"AI regenerated section: {section_heading}",
        )
        db.session.add(version)

        # Update file on disk
        md_path.write_text(updated_md, encoding="utf-8")
        if proposal.docx_file:
            docx_path = GENERATED_DIR / proposal.docx_file
            markdown_to_docx(updated_md, str(docx_path))

        # Update action items count
        action_items = re.findall(r"\[ACTION REQUIRED:\s*(.+?)\]", updated_md)
        proposal.action_items_count = len(action_items)

        db.session.commit()
        _log_activity("section_regenerate", f"Regenerated section: {section_heading}", project.id)
        flash(f"Section '{section_heading}' regenerated and saved as v{next_version}.", "success")

    except Exception as e:
        flash(f"Error regenerating section: {friendly_api_error(e)}", "error")

    return redirect(url_for("edit_proposal", proposal_id=proposal_id))


# ---------------------------------------------------------------------------
# CSV Activity Report
# ---------------------------------------------------------------------------

@app.route("/admin/export-activity")
@login_required
def export_activity_csv():
    """Export activity log as CSV, optionally filtered by role."""
    import csv
    import io

    if not current_user.is_admin:
        abort(403)

    role_filter = request.args.get("role", "")
    logs_query = ActivityLog.query.filter(
        ActivityLog.user_id.in_([u.id for u in _org_users_query().all()])
    ).order_by(ActivityLog.created_at.desc())

    if role_filter in ("admin", "sales", "proposal"):
        role_user_ids = [u.id for u in _org_users_query().filter_by(role=role_filter).all()]
        logs_query = logs_query.filter(ActivityLog.user_id.in_(role_user_ids))

    logs = logs_query.limit(5000).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Time", "User", "Role", "Action", "Detail", "Project ID"])
    for log in logs:
        user = db.session.get(User, log.user_id)
        writer.writerow([
            log.created_at.strftime("%Y-%m-%d"),
            log.created_at.strftime("%H:%M:%S"),
            (user.display_name or user.username) if user else "Unknown",
            (user.role or "proposal") if user else "",
            log.action,
            log.detail,
            log.project_id or "",
        ])

    output.seek(0)
    from flask import Response
    filename = f"activity_report{'_' + role_filter if role_filter else ''}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# FAQ
# ---------------------------------------------------------------------------

@app.route("/faq")
@login_required
def faq_page():
    faqs = [
        {
            "q": "How do I generate a proposal?",
            "a": "Create a new project (or drop an RFP on the dashboard), upload your RFP/RFQ documents, optionally draft and approve a Scope of Work, select the industry vertical and cost estimation options, then click 'Generate Proposal'. The AI will analyze your documents and produce a draft proposal in minutes."
        },
        {
            "q": "What is the Scope of Work step?",
            "a": "Before generating, you can have the AI read the RFP and propose a Scope of Work checklist. You accept or strike each item (and add your own), then approve. The generated proposal then covers exactly the approved scope — nothing more, nothing less."
        },
        {
            "q": "What file formats can I upload?",
            "a": "You can upload PDF, Word (.docx), plain text (.txt), Markdown (.md), and Excel (.xlsx, .xls) files. For project documents, PDF and Word are the most common. For rate sheets, Excel is recommended."
        },
        {
            "q": "How does the AI learn from my edits?",
            "a": "When you edit a proposal in the in-app editor and click 'Finalize & Teach AI', the system compares your edits to the original AI output. It stores the patterns of changes you make (tone, structure, pricing adjustments, etc.) and uses these corrections to improve future proposals for your account."
        },
        {
            "q": "What are Company Standards?",
            "a": "Company Standards are boilerplate content blocks (mission statement, certifications, safety record, past performance, etc.) that the AI automatically weaves into every proposal. Configure them in the Proposal Posture page (sidebar > Library > Proposal Posture)."
        },
        {
            "q": "How does cost estimation work?",
            "a": "First, configure your staff sell rates, equipment price list, and travel rates in the Proposal Posture page. When generating a proposal, check the boxes for which cost estimates you want included. The AI will use your actual rates to build cost tables in the proposal."
        },
        {
            "q": "Can I revert to a previous version of a proposal?",
            "a": "Yes. Open the proposal editor and use the Version History sidebar on the right. Each version has a 'View' button to preview it and a 'Restore' button to revert to that version. Restoring creates a new version, so you never lose any edits."
        },
        {
            "q": "What is the redline export?",
            "a": "The 'Download Redline DOCX' feature creates a Word document showing what changed between the AI's original draft and your latest edits. Deletions appear in red strikethrough and additions in blue underline \u2014 useful for review with your team."
        },
        {
            "q": "How do I set up my API key?",
            "a": "Go to Settings > Profile & AI. Select your preferred AI provider and model, then enter your API key. The key is stored encrypted and is never shared. You need a valid API key for proposal generation to work."
        },
        {
            "q": "What are the industry verticals?",
            "a": "Verticals are industry-specific templates that guide the AI's proposal structure and language. Current verticals include Data Center, Life Science/Pharma, Food & Beverage, and General. Choose 'Auto-detect' to let the AI determine the best fit from your RFP."
        },
        {
            "q": "Who has access to my data?",
            "a": "Your projects, proposals, rates, and settings are visible only to you. Admins can see aggregate metrics (project counts, win rates, pipeline value) for company reporting, but they cannot view your proposal content or rate details."
        },
    ]
    return render_template("faq.html", faqs=faqs)


# ---------------------------------------------------------------------------
# Help Chatbot (FAQ-based)
# ---------------------------------------------------------------------------

_HELP_KB = """You are the in-app help assistant for Proposal Manager, an AI proposal platform.
Answer concisely (2-5 sentences) and only about using the product. If unsure, point
the user to the relevant page. Key features and where they live:
- Dashboard: "Pick up where you left off" list; drop an RFP on the hero to start.
- New Proposal / Projects: upload RFP/RFQ/ROM docs (PDF, DOCX, TXT, MD, XLSX).
- Request types: RFP, RFQ, and ROM (a lighter range-priced budgetary estimate you
  can later convert to a full proposal).
- Scope of Work: AI drafts a checklist you approve before generating.
- Generation runs as a background job with a progress page.
- Internal Review: assign reviewers, they approve or request changes; the owner
  batch-applies revision requests to generate a new version.
- Structured pricing: the Estimate page (from a proposal) has an editable grid with
  live totals, CSV export, and "insert into proposal".
- Proposal Posture: templates, company standards, staff/equipment/travel rates, and
  branding. "AI import & review" extracts rates/standards from an uploaded file.
- Customer Portal: create a secure share link; the customer views, comments, and
  accepts/declines; you see open tracking.
- Export: DOCX and PDF; redline DOCX shows tracked changes vs the AI original.
- Reports: win/loss analysis, pipeline, competitors.
- Billing & Plans (admins): Free/Pro/Business with seat, project, and generation limits.
- People & Roles (admins): invite teammates by email, set roles.
- Settings > Profile & AI: set your Anthropic API key and model.
"""


def _fallback_help(message: str) -> str:
    responses = {
        ("generate", "proposal", "create"): "To generate a proposal: create a project, upload your RFP/RFQ, optionally approve a Scope of Work, then click Generate. It runs as a background job with a progress page.",
        ("upload", "file", "document", "format"): "You can upload PDF, Word (.docx), text (.txt), Markdown (.md), and Excel (.xlsx) files on the project page.",
        ("rate", "pricing", "cost", "estimate", "staff", "equipment", "travel"): "Set rates in Proposal Posture. For structured pricing, open a proposal and click Estimate — an editable grid with live totals, CSV export, and insert-into-proposal.",
        ("share", "customer", "send", "portal"): "On an approved proposal, use the Customer Portal panel to create a secure share link. The customer can view, comment, and accept/decline, and you get open tracking.",
        ("rom", "budgetary", "ballpark"): "Choose ROM as the request type for a lighter, range-priced budgetary estimate. You can convert it to a full RFP/RFQ later.",
        ("invite", "team", "seat", "member"): "Admins invite teammates from People & Roles by email and assign a role.",
        ("bill", "plan", "upgrade", "subscription"): "See Billing & Plans (admin) for your plan, usage vs limits, and upgrades.",
        ("api", "key", "model", "llm"): "Set your Anthropic API key and model in Settings > Profile & AI.",
    }
    best, score = None, 0
    for kws, resp in responses.items():
        s = sum(1 for kw in kws if kw in message)
        if s > score:
            best, score = resp, s
    return best or "I can help with generating proposals, pricing/estimates, the customer portal, posture setup, invites, and billing. Try rephrasing, or see the FAQ page."


@app.route("/api/chat", methods=["POST"])
@login_required
def chat_help():
    """Claude-powered help assistant grounded in the product knowledge base,
    with a keyword fallback when no API key is configured or the call fails."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message", "") or "").strip()
    if not message:
        return {"reply": "Please type a question and I'll do my best to help!"}

    api_key = decrypt_api_key(current_user.api_key_encrypted) or None
    from config.settings import ANTHROPIC_API_KEY as _GLOBAL_KEY
    effective_key = api_key or _GLOBAL_KEY
    if effective_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=effective_key, timeout=30, max_retries=1)
            resp = client.messages.create(
                model=current_user.llm_model or "claude-opus-4-6",
                max_tokens=400,
                system=_HELP_KB,
                messages=[{"role": "user", "content": message[:2000]}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            if text:
                return {"reply": text}
        except Exception:
            pass  # fall through to keyword help

    return {"reply": _fallback_help(message.lower())}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from config.settings import FLASK_DEBUG, FLASK_HOST, FLASK_PORT
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)

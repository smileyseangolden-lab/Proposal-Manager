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
import json
import os
import re
import uuid
from datetime import datetime, timezone
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
    url_for,
)
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from werkzeug.utils import secure_filename

from config.settings import (
    ALLOWED_EXTENSIONS,
    APP_COMPANY,
    APP_FOOTER,
    APP_NAME,
    APP_SHORT_NAME,
    FLASK_SECRET_KEY,
    GENERATED_DIR,
    MAX_UPLOAD_SIZE_MB,
    UPLOADS_DIR,
    VERTICALS,
)
from document_parser import parse_document
from models import (
    ActivityLog,
    CompanyStandard,
    DocumentTag,
    EquipmentItem,
    Notification,
    Project,
    ProjectDocument,
    Proposal,
    ProposalApproval,
    ProposalComment,
    ProposalCorrection,
    ProposalQuestion,
    ProposalReviewer,
    ProposalRevisionBatch,
    ProposalStatusHistory,
    ProposalVersion,
    RevisionRequest,
    RevisionTemplate,
    StaffRole,
    TravelExpenseRate,
    User,
    UserRateSheet,
    UserVerticalTemplate,
    db,
)
from proposal_agent import (
    generate_proposal,
    parse_customer_email,
    preflight_check_proposal,
    revise_proposal,
)
from proposal_export import markdown_to_docx, markdown_to_redline_docx
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

app = Flask(__name__, template_folder="web_templates", static_folder="static")
app.secret_key = FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE_MB * 1024 * 1024
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{Path(__file__).resolve().parent / 'data' / 'proposal_manager.db'}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

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


with app.app_context():
    # Cross-worker schema bootstrap lock. Under gunicorn, every worker imports
    # this module and runs db.create_all() + migrations. Without a lock, two
    # workers race to CREATE TABLE for a brand-new table and the loser crashes
    # with "table already exists". A simple file lock (fcntl.flock) serializes
    # the bootstrap so only one worker at a time touches the schema.
    import fcntl as _fcntl
    _data_dir = Path(__file__).resolve().parent / "data"
    _data_dir.mkdir(exist_ok=True)
    _lock_path = _data_dir / ".schema.lock"
    with open(_lock_path, "w") as _lock_fh:
        _fcntl.flock(_lock_fh, _fcntl.LOCK_EX)
        try:
            # Tolerate the race even if the lock somehow fails: if another
            # worker already created a table between our check and create,
            # swallow the "already exists" error. All other DDL errors
            # still propagate.
            try:
                db.create_all()
            except Exception as _e:
                if "already exists" not in str(_e):
                    raise

            # Migrate existing project_documents table to add new columns if missing
            import sqlite3 as _sqlite3
            _db_path = str(_data_dir / "proposal_manager.db")
            _conn = _sqlite3.connect(_db_path)
            _cur = _conn.cursor()
            _cur.execute("PRAGMA table_info(project_documents)")
            _existing_cols = {row[1] for row in _cur.fetchall()}
            _migrations = [
                ("is_reference", "ALTER TABLE project_documents ADD COLUMN is_reference BOOLEAN DEFAULT 0"),
                ("notes", 'ALTER TABLE project_documents ADD COLUMN notes TEXT DEFAULT ""'),
                ("version_group", 'ALTER TABLE project_documents ADD COLUMN version_group VARCHAR(32) DEFAULT ""'),
                ("version_label", 'ALTER TABLE project_documents ADD COLUMN version_label VARCHAR(100) DEFAULT ""'),
            ]
            for col, sql in _migrations:
                if col not in _existing_cols:
                    _cur.execute(sql)
            _conn.commit()

            # Migrate projects table to add assigned_to column if missing
            _cur.execute("PRAGMA table_info(projects)")
            _proj_cols = {row[1] for row in _cur.fetchall()}
            if "assigned_to" not in _proj_cols:
                _cur.execute('ALTER TABLE projects ADD COLUMN assigned_to VARCHAR(32) DEFAULT NULL')
                _conn.commit()

            # Part 2 migrations: deadlines and win/loss analysis columns
            _cur.execute("PRAGMA table_info(projects)")
            _proj_cols = {row[1] for row in _cur.fetchall()}
            _project_migrations = [
                ("due_date", "ALTER TABLE projects ADD COLUMN due_date DATETIME DEFAULT NULL"),
                ("close_reason", 'ALTER TABLE projects ADD COLUMN close_reason TEXT DEFAULT ""'),
                ("close_category", 'ALTER TABLE projects ADD COLUMN close_category VARCHAR(50) DEFAULT ""'),
                ("competitor_name", 'ALTER TABLE projects ADD COLUMN competitor_name VARCHAR(300) DEFAULT ""'),
                ("closed_at", "ALTER TABLE projects ADD COLUMN closed_at DATETIME DEFAULT NULL"),
            ]
            for col, sql in _project_migrations:
                if col not in _proj_cols:
                    _cur.execute(sql)
            _conn.commit()

            # Migrate users table to add role column if missing
            _cur.execute("PRAGMA table_info(users)")
            _user_cols = {row[1] for row in _cur.fetchall()}
            if "role" not in _user_cols:
                _cur.execute('ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT "proposal"')
                # Backfill: set existing admins to admin role
                _cur.execute('UPDATE users SET role = "admin" WHERE is_admin = 1')
                _conn.commit()

            # Migrate proposals table for Part 3 review lifecycle
            _cur.execute("PRAGMA table_info(proposals)")
            _prop_cols = {row[1] for row in _cur.fetchall()}
            if "review_status" not in _prop_cols:
                _cur.execute('ALTER TABLE proposals ADD COLUMN review_status VARCHAR(40) DEFAULT "draft"')
                _conn.commit()
            if "review_deadline" not in _prop_cols:
                _cur.execute('ALTER TABLE proposals ADD COLUMN review_deadline DATETIME DEFAULT NULL')
                _conn.commit()

            _conn.close()
        finally:
            _fcntl.flock(_lock_fh, _fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _allowed_file(filename: str, extensions: set = None) -> bool:
    exts = extensions or ALLOWED_EXTENSIONS
    return "." in filename and filename.rsplit(".", 1)[1].lower() in exts


def _log_activity(action: str, detail: str = "", project_id: str = None):
    log = ActivityLog(
        user_id=current_user.id,
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
    """Send a notification to all users with a given role."""
    users = User.query.filter_by(role=role).all()
    for u in users:
        if exclude_user_id and u.id == exclude_user_id:
            continue
        db.session.add(Notification(user_id=u.id, category=category, title=title, message=message, link=link))
    db.session.commit()


@app.context_processor
def inject_notifications():
    if hasattr(current_user, "id") and current_user.is_authenticated:
        unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
        return dict(unread_notifications=unread)
    return dict(unread_notifications=0)


def _can_access_project(project) -> bool:
    """Check if current user can access a project (owner, assigned, or admin)."""
    if not project:
        return False
    return (
        project.user_id == current_user.id
        or project.assigned_to == current_user.id
        or current_user.is_admin
    )


def _save_upload(file, subdir: str = "") -> tuple[str, str, int]:
    """Save an uploaded file. Returns (safe_name, full_path, file_size)."""
    safe = secure_filename(file.filename)
    unique = f"{uuid.uuid4().hex[:8]}_{safe}"
    dest_dir = UPLOADS_DIR / subdir if subdir else UPLOADS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / unique
    file.save(str(dest))
    size = dest.stat().st_size
    return safe, str(dest), size


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html")

    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    display_name = request.form.get("display_name", "").strip()
    company_name = request.form.get("company_name", "").strip()

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

    # First user becomes admin
    if User.query.count() == 0:
        user.is_admin = True
        user.role = "admin"

    db.session.add(user)
    db.session.commit()
    login_user(user)
    _log_activity("signup", f"User {username} created account")
    flash("Account created successfully.", "success")
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = User.query.filter_by(username=username).first()

    if not user or not user.check_password(password):
        flash("Invalid username or password.", "error")
        return redirect(url_for("login"))

    login_user(user)
    _log_activity("login")
    return redirect(url_for("dashboard"))


@app.route("/logout")
@login_required
def logout():
    _log_activity("logout")
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

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
    proposal_users = User.query.filter(User.role.in_(["proposal", "admin"])).order_by(User.display_name).all()

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

    return render_template(
        "dashboard.html",
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
            current_user.api_key_encrypted = api_key  # In production, encrypt this

        db.session.commit()
        _log_activity("settings_update", "Updated user settings")
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))

    # Rate sheets
    rate_sheets = UserRateSheet.query.filter_by(user_id=current_user.id).order_by(UserRateSheet.uploaded_at.desc()).all()
    # User vertical templates
    user_templates = UserVerticalTemplate.query.filter_by(user_id=current_user.id, is_company_default=False).order_by(UserVerticalTemplate.uploaded_at.desc()).all()
    # Staff roles
    staff_roles = StaffRole.query.filter_by(user_id=current_user.id).order_by(StaffRole.category, StaffRole.role_name).all()
    # Equipment items
    equipment_items = EquipmentItem.query.filter_by(user_id=current_user.id).order_by(EquipmentItem.category, EquipmentItem.item_name).all()
    # Travel expense rates
    travel_rates = TravelExpenseRate.query.filter_by(user_id=current_user.id).order_by(TravelExpenseRate.expense_type).all()
    # Company standards
    company_standards = CompanyStandard.query.filter_by(user_id=current_user.id).order_by(CompanyStandard.category, CompanyStandard.title).all()
    # Revision request templates (Part 3)
    revision_templates = RevisionTemplate.query.filter_by(user_id=current_user.id).order_by(
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
    )


@app.route("/settings/upload-rate-sheet", methods=["POST"])
@login_required
def upload_rate_sheet():
    file = request.files.get("rate_sheet")
    sheet_type = request.form.get("sheet_type", "labor_rates")

    if not file or not _allowed_file(file.filename, RATE_SHEET_EXTENSIONS):
        flash("Please upload an Excel file (.xlsx).", "error")
        return redirect(url_for("settings") + "#company")

    safe, path, size = _save_upload(file, f"rate_sheets/{current_user.id}")

    sheet = UserRateSheet(
        user_id=current_user.id,
        name=request.form.get("sheet_name", safe),
        sheet_type=sheet_type,
        file_path=path,
        original_filename=safe,
    )
    db.session.add(sheet)
    db.session.commit()
    _log_activity("rate_sheet_upload", f"Uploaded {sheet_type}: {safe}")
    flash("Rate sheet uploaded.", "success")
    return redirect(url_for("settings") + "#company")


@app.route("/settings/upload-template", methods=["POST"])
@login_required
def upload_user_template():
    file = request.files.get("template_file")
    vertical = request.form.get("vertical", "general")
    template_type = request.form.get("template_type", "proposal")

    if not file or not _allowed_file(file.filename, TEMPLATE_EXTENSIONS):
        flash("Please upload a Word or PDF file.", "error")
        return redirect(url_for("settings") + "#company")

    safe, path, size = _save_upload(file, f"user_templates/{current_user.id}")

    tmpl = UserVerticalTemplate(
        user_id=current_user.id,
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
    return redirect(url_for("settings") + "#company")


@app.route("/settings/delete-rate-sheet/<sheet_id>", methods=["POST"])
@login_required
def delete_rate_sheet(sheet_id):
    sheet = db.session.get(UserRateSheet, sheet_id)
    if not sheet or sheet.user_id != current_user.id:
        abort(404)
    db.session.delete(sheet)
    db.session.commit()
    flash("Rate sheet deleted.", "success")
    return redirect(url_for("settings") + "#company")


@app.route("/settings/delete-template/<template_id>", methods=["POST"])
@login_required
def delete_user_template(template_id):
    tmpl = db.session.get(UserVerticalTemplate, template_id)
    if not tmpl or tmpl.user_id != current_user.id:
        abort(404)
    db.session.delete(tmpl)
    db.session.commit()
    flash("Template deleted.", "success")
    return redirect(url_for("settings") + "#company")


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
            name=f"Staff Rates - {safe}",
            sheet_type="labor_rates",
            file_path=path,
            original_filename=safe,
        )
        db.session.add(sheet)
        db.session.commit()
        _log_activity("rate_sheet_upload", f"Uploaded staff rate sheet: {safe}")
        flash(f"Rate sheet '{safe}' uploaded.", "success")
        return redirect(url_for("settings") + "#company")

    role_name = request.form.get("role_name", "").strip()
    category = request.form.get("category", "").strip()
    hourly_rate = request.form.get("hourly_rate", "0")
    overtime_rate = request.form.get("overtime_rate", "0")
    description = request.form.get("description", "").strip()

    if not role_name or not hourly_rate:
        flash("Role name and hourly rate are required.", "error")
        return redirect(url_for("settings") + "#company")

    try:
        hourly_rate = float(hourly_rate.replace(",", "").replace("$", ""))
        overtime_rate = float(overtime_rate.replace(",", "").replace("$", "")) if overtime_rate else 0.0
    except ValueError:
        flash("Invalid rate format.", "error")
        return redirect(url_for("settings") + "#company")

    if hourly_rate < 0 or overtime_rate < 0:
        flash("Rates cannot be negative.", "error")
        return redirect(url_for("settings") + "#company")

    role = StaffRole(
        user_id=current_user.id,
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
    return redirect(url_for("settings") + "#company")


@app.route("/settings/edit-staff-role/<role_id>", methods=["POST"])
@login_required
def edit_staff_role(role_id):
    role = db.session.get(StaffRole, role_id)
    if not role or role.user_id != current_user.id:
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
        return redirect(url_for("settings") + "#company")

    db.session.commit()
    _log_activity("staff_role_edit", f"Updated staff role: {role.role_name}")
    flash(f"Staff role '{role.role_name}' updated.", "success")
    return redirect(url_for("settings") + "#company")


@app.route("/settings/delete-staff-role/<role_id>", methods=["POST"])
@login_required
def delete_staff_role(role_id):
    role = db.session.get(StaffRole, role_id)
    if not role or role.user_id != current_user.id:
        abort(404)
    name = role.role_name
    db.session.delete(role)
    db.session.commit()
    _log_activity("staff_role_delete", f"Deleted staff role: {name}")
    flash(f"Staff role '{name}' deleted.", "success")
    return redirect(url_for("settings") + "#company")


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
            name=f"Equipment Price List - {safe}",
            sheet_type="product_pricing",
            file_path=path,
            original_filename=safe,
        )
        db.session.add(sheet)
        db.session.commit()
        _log_activity("rate_sheet_upload", f"Uploaded equipment price list: {safe}")
        flash(f"Price list '{safe}' uploaded.", "success")
        return redirect(url_for("settings") + "#company")

    item_name = request.form.get("item_name", "").strip()
    category = request.form.get("eq_category", "").strip()
    part_number = request.form.get("part_number", "").strip()
    manufacturer = request.form.get("manufacturer", "").strip()
    unit_cost = request.form.get("unit_cost", "0")
    unit = request.form.get("unit", "each").strip()
    description = request.form.get("eq_description", "").strip()

    if not item_name or not unit_cost:
        flash("Item name and unit cost are required.", "error")
        return redirect(url_for("settings") + "#company")

    try:
        unit_cost = float(unit_cost.replace(",", "").replace("$", ""))
    except ValueError:
        flash("Invalid cost format.", "error")
        return redirect(url_for("settings") + "#company")

    if unit_cost < 0:
        flash("Cost cannot be negative.", "error")
        return redirect(url_for("settings") + "#company")

    item = EquipmentItem(
        user_id=current_user.id,
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
    return redirect(url_for("settings") + "#company")


@app.route("/settings/delete-equipment-item/<item_id>", methods=["POST"])
@login_required
def delete_equipment_item(item_id):
    item = db.session.get(EquipmentItem, item_id)
    if not item or item.user_id != current_user.id:
        abort(404)
    name = item.item_name
    db.session.delete(item)
    db.session.commit()
    _log_activity("equipment_delete", f"Deleted equipment: {name}")
    flash(f"Equipment item '{name}' deleted.", "success")
    return redirect(url_for("settings") + "#company")


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
            name=f"Travel Rates - {safe}",
            sheet_type="labor_rates",
            file_path=path,
            original_filename=safe,
        )
        db.session.add(sheet)
        db.session.commit()
        _log_activity("rate_sheet_upload", f"Uploaded travel rate schedule: {safe}")
        flash(f"Travel rate schedule '{safe}' uploaded.", "success")
        return redirect(url_for("settings") + "#company")

    expense_type = request.form.get("expense_type", "").strip()
    description = request.form.get("travel_description", "").strip()
    rate = request.form.get("travel_rate", "0")
    unit = request.form.get("travel_unit", "per day").strip()

    if not expense_type or not rate:
        flash("Expense type and rate are required.", "error")
        return redirect(url_for("settings") + "#company")

    try:
        rate = float(rate.replace(",", "").replace("$", ""))
    except ValueError:
        flash("Invalid rate format.", "error")
        return redirect(url_for("settings") + "#company")

    if rate < 0:
        flash("Rate cannot be negative.", "error")
        return redirect(url_for("settings") + "#company")

    tr = TravelExpenseRate(
        user_id=current_user.id,
        expense_type=expense_type,
        description=description,
        rate=rate,
        unit=unit,
    )
    db.session.add(tr)
    db.session.commit()
    _log_activity("travel_rate_add", f"Added travel rate: {expense_type} @ ${rate}/{unit}")
    flash(f"Travel rate '{expense_type}' added.", "success")
    return redirect(url_for("settings") + "#company")


@app.route("/settings/delete-travel-rate/<rate_id>", methods=["POST"])
@login_required
def delete_travel_rate(rate_id):
    tr = db.session.get(TravelExpenseRate, rate_id)
    if not tr or tr.user_id != current_user.id:
        abort(404)
    name = tr.expense_type
    db.session.delete(tr)
    db.session.commit()
    _log_activity("travel_rate_delete", f"Deleted travel rate: {name}")
    flash(f"Travel rate '{name}' deleted.", "success")
    return redirect(url_for("settings") + "#company")


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
    due_date_raw = request.form.get("due_date", "").strip()
    if not name:
        flash("Project name is required.", "error")
        return redirect(url_for("new_project"))

    project = Project(
        user_id=current_user.id,
        name=name,
        client_name=client,
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
    staff_role_count = StaffRole.query.filter_by(user_id=current_user.id, is_active=True).count()
    equipment_count = EquipmentItem.query.filter_by(user_id=current_user.id, is_active=True).count()
    travel_rate_count = TravelExpenseRate.query.filter_by(user_id=current_user.id, is_active=True).count()

    # Template availability for indicator
    has_user_template = UserVerticalTemplate.query.filter_by(
        user_id=current_user.id, is_company_default=False
    ).first() is not None
    has_company_template = UserVerticalTemplate.query.filter_by(
        is_company_default=True
    ).first() is not None

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
    )


@app.route("/projects/<project_id>/generate", methods=["POST"])
@login_required
def project_generate(project_id):
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    vertical = request.form.get("vertical", "auto")
    output_format = request.form.get("output_format", "docx")

    project.output_format = output_format
    db.session.commit()

    # Collect all document text
    documents = ProjectDocument.query.filter_by(project_id=project_id).all()
    rfp_docs = [d for d in documents if d.file_type in ("rfp", "supporting")]
    if not rfp_docs:
        flash("No RFP/RFQ documents uploaded yet.", "error")
        return redirect(url_for("project_upload", project_id=project_id))

    combined_text = ""
    for doc in rfp_docs:
        try:
            text = parse_document(doc.file_path)
            combined_text += f"\n\n--- Document: {doc.original_filename} ---\n\n{text}"
        except Exception:
            continue

    if not combined_text.strip():
        flash("Could not extract text from the uploaded documents.", "error")
        return redirect(url_for("project_upload", project_id=project_id))

    # Read cost estimation checkbox options
    include_staff_types = request.form.get("include_staff_types") == "1"
    include_staff_hours = request.form.get("include_staff_hours") == "1"
    include_equipment_bom = request.form.get("include_equipment_bom") == "1"
    include_travel_expenses = request.form.get("include_travel_expenses") == "1"

    cost_options = {
        "include_staff_types": include_staff_types,
        "include_staff_hours": include_staff_hours,
        "include_equipment_bom": include_equipment_bom,
        "include_travel_expenses": include_travel_expenses,
    }

    # Build structured rate data from DB entries
    staff_roles_data = None
    if include_staff_types or include_staff_hours:
        roles = StaffRole.query.filter_by(user_id=current_user.id, is_active=True).all()
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
        items = EquipmentItem.query.filter_by(user_id=current_user.id, is_active=True).all()
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
        rates = TravelExpenseRate.query.filter_by(user_id=current_user.id, is_active=True).all()
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
    active_sheets = UserRateSheet.query.filter_by(user_id=current_user.id, is_active=True).all()
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
        user_id=current_user.id, vertical=vertical, is_company_default=False
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
        vertical=vertical, is_company_default=True
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
        user_id=current_user.id
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
    standards = CompanyStandard.query.filter_by(user_id=current_user.id, is_active=True).all()
    company_standards_data = None
    if standards:
        company_standards_data = [
            {"category": s.category, "title": s.title, "content": s.content}
            for s in standards
        ]

    try:
        result = generate_proposal(
            combined_text,
            vertical=vertical,
            rate_sheet_data=rate_sheet_data,
            user_templates=user_templates,
            company_name=current_user.company_name,
            user_api_key=current_user.api_key_encrypted or None,
            user_model=current_user.llm_model or None,
            cost_options=cost_options,
            staff_roles_data=staff_roles_data,
            equipment_data=equipment_data,
            travel_data=travel_data,
            past_corrections=corrections_data,
            company_standards=company_standards_data,
        )

        # Check if the agent has questions
        if result.get("questions"):
            for q in result["questions"]:
                pq = ProposalQuestion(
                    project_id=project_id,
                    question=q["question"],
                    context=q.get("context", ""),
                    status="pending",
                )
                db.session.add(pq)
            db.session.commit()
            _log_activity("proposal_questions", f"{len(result['questions'])} clarification question(s)", project_id)
            return redirect(url_for("project_questions", project_id=project_id))

        # Save outputs
        job_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        md_filename = f"proposal_{job_id}.md"
        md_path = GENERATED_DIR / md_filename
        md_path.write_text(result["proposal_markdown"], encoding="utf-8")

        docx_filename = f"proposal_{job_id}.docx"
        docx_path = GENERATED_DIR / docx_filename
        markdown_to_docx(result["proposal_markdown"], str(docx_path))

        pdf_filename = ""
        # PDF generation could be added here if needed

        project.vertical = result["vertical"]
        project.vertical_label = result["vertical_label"]

        proposal = Proposal(
            project_id=project_id,
            job_id=job_id,
            document_type=result["document_type"],
            vertical=result["vertical"],
            vertical_label=result["vertical_label"],
            confidence_score=result["confidence_score"],
            action_items_count=len(result["action_items"]),
            md_file=md_filename,
            docx_file=docx_filename,
            pdf_file=pdf_filename,
            review_status="draft",
        )
        db.session.add(proposal)
        db.session.flush()  # Get proposal.id before commit

        # Record the initial "draft" state in status history
        db.session.add(ProposalStatusHistory(
            proposal_id=proposal.id,
            from_status="",
            to_status="draft",
            actor_id=current_user.id,
            note="AI-generated v1.",
        ))

        # Save version 1 (AI-generated original)
        v1 = ProposalVersion(
            proposal_id=proposal.id,
            version_number=1,
            markdown_content=result["proposal_markdown"],
            edit_source="ai",
            change_summary="AI-generated original",
        )
        db.session.add(v1)
        db.session.commit()
        _log_activity("proposal_generate", f"Generated {result['vertical_label']} proposal", project_id)
        # Notify sales users when proposals are generated
        _notify_role(
            "sales", "proposal_generated",
            f"Proposal generated: {project.name}",
            f"{current_user.display_name or current_user.username} generated a {result['vertical_label']} proposal for '{project.name}'.",
            link=f"/proposal/{proposal.id}",
            exclude_user_id=current_user.id,
        )
        # Also notify the project owner if different from generator
        if project.user_id != current_user.id:
            _notify(
                project.user_id, "proposal_generated",
                f"Proposal generated for your project: {project.name}",
                f"{current_user.display_name or current_user.username} generated a proposal for '{project.name}'.",
                link=f"/proposal/{proposal.id}",
            )

        return redirect(url_for("view_proposal", proposal_id=proposal.id))

    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("project_upload", project_id=project_id))
    except Exception as e:
        flash(f"An error occurred: {e}", "error")
        return redirect(url_for("project_upload", project_id=project_id))


@app.route("/projects/<project_id>/questions", methods=["GET", "POST"])
@login_required
def project_questions(project_id):
    project = db.session.get(Project, project_id)
    if not project or project.user_id != current_user.id:
        abort(404)

    pending = ProposalQuestion.query.filter_by(project_id=project_id, status="pending").all()

    if request.method == "POST":
        for q in pending:
            answer = request.form.get(f"answer_{q.id}", "").strip()
            if answer:
                q.answer = answer
                q.status = "answered"
                q.answered_at = datetime.now(timezone.utc)
            elif request.form.get(f"skip_{q.id}"):
                q.status = "skipped"
        db.session.commit()

        # Check if there are still pending questions
        remaining = ProposalQuestion.query.filter_by(project_id=project_id, status="pending").count()
        if remaining == 0:
            flash("All questions answered. You can now regenerate the proposal.", "success")
            return redirect(url_for("project_upload", project_id=project_id))

        return redirect(url_for("project_questions", project_id=project_id))

    return render_template("project_questions.html", project=project, questions=pending)


@app.route("/projects/<project_id>/update-status", methods=["POST"])
@login_required
def update_project_status(project_id):
    project = db.session.get(Project, project_id)
    if not _can_access_project(project):
        abort(404)

    new_status = request.form.get("status", project.status)
    dollar_amount = request.form.get("dollar_amount")

    prev_status = project.status
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
    proposal_html = md.markdown(proposal_md, extensions=["tables", "fenced_code"])
    action_items = re.findall(r"\[ACTION REQUIRED:\s*(.+?)\]", proposal_md)

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
    )


@app.route("/download/<proposal_id>/<fmt>")
@login_required
def download(proposal_id, fmt):
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        flash("Proposal not found.", "error")
        return redirect(url_for("dashboard"))

    project = db.session.get(Project, proposal.project_id)
    if not project or (project.user_id != current_user.id and not current_user.is_admin):
        abort(404)

    if fmt == "docx":
        file_path = GENERATED_DIR / proposal.docx_file
        mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif fmt == "md":
        file_path = GENERATED_DIR / proposal.md_file
        mimetype = "text/markdown"
    else:
        flash("Invalid format.", "error")
        return redirect(url_for("view_proposal", proposal_id=proposal_id))

    return send_file(str(file_path), mimetype=mimetype, as_attachment=True, download_name=file_path.name)


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
    if not project or (project.user_id != current_user.id and not current_user.is_admin):
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
            markdown_to_docx(new_content, str(docx_path))

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


@app.route("/proposal/<proposal_id>/version/<version_id>")
@login_required
def view_version(proposal_id, version_id):
    proposal = db.session.get(Proposal, proposal_id)
    if not proposal:
        abort(404)
    project = db.session.get(Project, proposal.project_id)
    if not project or (project.user_id != current_user.id and not current_user.is_admin):
        abort(404)

    version = db.session.get(ProposalVersion, version_id)
    if not version or version.proposal_id != proposal_id:
        abort(404)

    proposal_html = md.markdown(version.markdown_content, extensions=["tables", "fenced_code"])

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
    if not project or (project.user_id != current_user.id and not current_user.is_admin):
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
        markdown_to_docx(version.markdown_content, str(docx_path))

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
    if not project or (project.user_id != current_user.id and not current_user.is_admin):
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
    if not project or (project.user_id != current_user.id and not current_user.is_admin):
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
    if (
        project.user_id == current_user.id
        or project.assigned_to == current_user.id
        or current_user.is_admin
    ):
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
    return (
        project.user_id == current_user.id
        or project.assigned_to == current_user.id
        or current_user.is_admin
    )


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
        all_users = User.query.order_by(User.display_name).all()
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
        if not user:
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
    proposal_html = md.markdown(proposal_md, extensions=["tables", "fenced_code"])

    # My pending/existing revision requests on this proposal
    my_requests = RevisionRequest.query.filter_by(
        proposal_id=proposal_id, author_id=current_user.id
    ).order_by(RevisionRequest.created_at.desc()).all()

    # Templates I can apply
    templates = RevisionTemplate.query.filter_by(user_id=current_user.id).order_by(
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
    standards = CompanyStandard.query.filter_by(user_id=owner_id, is_active=True).all()
    standards_data = [
        {"category": s.category, "title": s.title, "content": s.content}
        for s in standards
    ] if standards else None

    corrections = ProposalCorrection.query.filter_by(user_id=owner_id).order_by(
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
            user_api_key=current_user.api_key_encrypted or None,
            user_model=current_user.llm_model or None,
            company_standards=standards_data,
            past_corrections=corrections_data,
        )
    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("apply_feedback", proposal_id=proposal_id))
    except Exception as e:
        flash(f"AI revision failed: {e}", "error")
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
                user_api_key=current_user.api_key_encrypted or None,
                user_model=current_user.llm_model or None,
            )
        except RuntimeError as e:
            flash(str(e), "error")
            return redirect(url_for("customer_feedback", proposal_id=proposal_id))
        except Exception as e:
            flash(f"Email parsing failed: {e}", "error")
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
            user_api_key=current_user.api_key_encrypted or None,
            user_model=current_user.llm_model or None,
        )
    except Exception as e:
        result = {"action_items": [], "warnings": [f"Pre-flight check error: {e}"], "ready": False}

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
        return redirect(url_for("settings") + "#revision-templates")

    if category not in {c[0] for c in REVISION_CATEGORIES}:
        category = "other"

    tmpl = RevisionTemplate(
        user_id=current_user.id,
        name=name[:200],
        category=category,
        directive_template=directive,
        description=description,
    )
    db.session.add(tmpl)
    db.session.commit()
    _log_activity("revision_template_add", f"Added revision template: {name}")
    flash(f"Revision template '{name}' added.", "success")
    return redirect(url_for("settings") + "#revision-templates")


@app.route("/settings/delete-revision-template/<template_id>", methods=["POST"])
@login_required
def delete_revision_template(template_id):
    tmpl = db.session.get(RevisionTemplate, template_id)
    if not tmpl or tmpl.user_id != current_user.id:
        abort(404)
    name = tmpl.name
    db.session.delete(tmpl)
    db.session.commit()
    _log_activity("revision_template_delete", f"Deleted revision template: {name}")
    flash(f"Revision template '{name}' deleted.", "success")
    return redirect(url_for("settings") + "#revision-templates")


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
        return redirect(url_for("settings") + "#company")

    # Handle file upload as alternative to text content
    uploaded_file = request.files.get("standard_file")
    if uploaded_file and uploaded_file.filename and _allowed_file(uploaded_file.filename, ALLOWED_EXTENSIONS | {"xlsx", "xls"}):
        safe, path, size = _save_upload(uploaded_file, "company_standards")
        content = content or f"[Uploaded file: {safe}]"

    if not content:
        flash("Either content or a file is required.", "error")
        return redirect(url_for("settings") + "#company")

    standard = CompanyStandard(
        user_id=current_user.id,
        category=category,
        title=title,
        content=content,
    )
    db.session.add(standard)
    db.session.commit()
    _log_activity("company_standard_add", f"Added standard: {title}")
    flash(f"Company standard '{title}' added.", "success")
    return redirect(url_for("settings") + "#company")


@app.route("/settings/edit-company-standard/<standard_id>", methods=["POST"])
@login_required
def edit_company_standard(standard_id):
    std = db.session.get(CompanyStandard, standard_id)
    if not std or std.user_id != current_user.id:
        abort(404)

    std.category = request.form.get("standard_category", std.category).strip()
    std.title = request.form.get("standard_title", std.title).strip()
    std.content = request.form.get("standard_content", std.content).strip()

    db.session.commit()
    _log_activity("company_standard_edit", f"Updated standard: {std.title}")
    flash(f"Standard '{std.title}' updated.", "success")
    return redirect(url_for("settings") + "#company")


@app.route("/settings/delete-company-standard/<standard_id>", methods=["POST"])
@login_required
def delete_company_standard(standard_id):
    std = db.session.get(CompanyStandard, standard_id)
    if not std or std.user_id != current_user.id:
        abort(404)
    title = std.title
    db.session.delete(std)
    db.session.commit()
    _log_activity("company_standard_delete", f"Deleted standard: {title}")
    flash(f"Standard '{title}' deleted.", "success")
    return redirect(url_for("settings") + "#company")


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

@app.route("/admin")
@login_required
def admin_panel():
    if not current_user.is_admin:
        flash("Access denied.", "error")
        return redirect(url_for("dashboard"))

    users = User.query.order_by(User.created_at.desc()).all()

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

    # Company-wide totals
    total_projects = Project.query.count()
    total_proposals = Proposal.query.count()
    total_won = Project.query.filter_by(status="won").count()
    total_lost = Project.query.filter_by(status="lost").count()
    total_decided = total_won + total_lost
    total_users = len(users)
    company_total_dollar = db.session.query(func.sum(Project.dollar_amount)).filter(
        Project.dollar_amount > 0
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

    # Activity filter
    activity_filter = request.args.get("activity_role", "")
    if activity_filter and activity_filter in ("admin", "sales", "proposal"):
        role_user_ids = [u.id for u in users if (getattr(u, "role", None) or ("admin" if u.is_admin else "proposal")) == activity_filter]
        recent_activity = ActivityLog.query.filter(
            ActivityLog.user_id.in_(role_user_ids)
        ).order_by(ActivityLog.created_at.desc()).limit(50).all()
    else:
        recent_activity = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(50).all()

    return render_template(
        "admin.html",
        users=users,
        user_stats=user_stats,
        company_stats=company_stats,
        role_counts=role_counts,
        role_performance=role_performance,
        recent_activity=recent_activity,
        activity_filter=activity_filter,
    )


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
    if not tmpl or not tmpl.is_company_default:
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
    if not user:
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
    if not user:
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
    all_projects = Project.query.filter_by(user_id=current_user.id).order_by(Project.name).all()
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
    proposals = Proposal.query.join(Project).filter(
        Project.user_id == current_user.id
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
    if not project or project.user_id != current_user.id:
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
    if not project or project.user_id != current_user.id:
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
    if not project or project.user_id != current_user.id:
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
    if not project or project.user_id != current_user.id:
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
    if not project or project.user_id != current_user.id:
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
    if not src_project or src_project.user_id != current_user.id:
        abort(404)

    target_project_id = request.form.get("target_project_id", "").strip()
    target_project = db.session.get(Project, target_project_id)
    if not target_project or target_project.user_id != current_user.id:
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
    if not project or project.user_id != current_user.id:
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
    if not project or project.user_id != current_user.id:
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
    if not project or (project.user_id != current_user.id and not current_user.is_admin):
        abort(404)

    assignee_id = request.form.get("assigned_to", "").strip()
    if assignee_id:
        assignee = db.session.get(User, assignee_id)
        if not assignee:
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
        all_with_due = Project.query.filter(Project.due_date.isnot(None)).all()
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
        base_query = Project.query
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
    if not current_user.is_admin:
        proj_query = proj_query.filter(_my_projects_filter())
    results["projects"] = proj_query.order_by(Project.updated_at.desc()).limit(25).all()

    # Documents (under user's projects, or admin sees all)
    if current_user.is_admin:
        accessible_project_ids = [p.id for p in Project.query.all()]
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

    # Comments
    if current_user.is_admin:
        comment_query = ProposalComment.query.filter(ProposalComment.body.ilike(like))
    else:
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
    if comment.author_id != current_user.id and not current_user.is_admin:
        abort(403)
    db.session.delete(comment)
    db.session.commit()
    flash("Comment deleted.", "success")
    return redirect(request.referrer or url_for("view_proposal", proposal_id=proposal_id))


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
    logs_query = ActivityLog.query.order_by(ActivityLog.created_at.desc())

    if role_filter in ("admin", "sales", "proposal"):
        role_user_ids = [u.id for u in User.query.filter_by(role=role_filter).all()]
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
            "a": "Create a new project, upload your RFP/RFQ documents, select the industry vertical and cost estimation options, then click 'Generate Proposal'. The AI will analyze your documents and produce a draft proposal in seconds."
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
            "a": "Company Standards are boilerplate content blocks (mission statement, certifications, safety record, past performance, etc.) that the AI automatically weaves into every proposal. Configure them in Settings > Proposal Setup."
        },
        {
            "q": "How does cost estimation work?",
            "a": "First, configure your staff sell rates, equipment price list, and travel rates in Settings > Proposal Setup. When generating a proposal, check the boxes for which cost estimates you want included. The AI will use your actual rates to build cost tables in the proposal."
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

@app.route("/api/chat", methods=["POST"])
@login_required
def chat_help():
    """Simple FAQ-based chatbot that matches user questions to predefined answers."""
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip().lower()

    if not message:
        return {"reply": "Please type a question and I'll do my best to help!"}

    # Simple keyword matching for help topics
    responses = {
        ("generate", "proposal", "create"): "To generate a proposal: Create a new project, upload your RFP/RFQ documents, choose cost estimation options, and click 'Generate Proposal'. The AI will analyze your docs and produce a draft in seconds.",
        ("upload", "file", "document", "format"): "You can upload PDF, Word (.docx), text (.txt), Markdown (.md), and Excel (.xlsx) files. Use the project page to upload RFP documents, and Settings for rate sheets.",
        ("learn", "teach", "finalize", "correction"): "After editing a proposal, click 'Finalize & Teach AI' in the editor. The system captures your editing patterns and uses them to improve future proposals.",
        ("rate", "pricing", "cost", "staff", "equipment", "travel"): "Configure your rates in Settings > Proposal Setup. Add staff hourly rates, equipment prices, and travel rates. The AI uses these when you check the cost estimation boxes during generation.",
        ("version", "history", "revert", "restore"): "Open the proposal editor to see Version History in the right sidebar. You can view any version and restore previous ones. Restoring creates a new version so nothing is ever lost.",
        ("redline", "tracked", "changes", "diff"): "Download Redline DOCX from the proposal view page. It shows AI-original vs your edits with red strikethrough (deletions) and blue underline (insertions).",
        ("api", "key", "provider", "model", "llm"): "Go to Settings > Profile & AI to configure your AI provider, model, and API key. Currently supports Anthropic Claude, OpenAI GPT, and Google Gemini.",
        ("vertical", "industry", "template"): "Verticals are industry-specific templates: Data Center, Life Science, Food & Beverage, and General. Choose during proposal generation or let the AI auto-detect from your RFP.",
        ("admin", "user", "manage"): "Admins can view company-wide metrics, manage user permissions, and track activity from the Admin panel in the sidebar.",
        ("standard", "boilerplate", "mission", "certification"): "Company Standards are reusable content blocks. Go to Settings > Proposal Setup to add mission statements, certifications, past performance, etc. The AI includes these in every proposal.",
    }

    best_match = None
    best_score = 0
    for keywords, response in responses.items():
        score = sum(1 for kw in keywords if kw in message)
        if score > best_score:
            best_score = score
            best_match = response

    if best_match and best_score > 0:
        return {"reply": best_match}

    return {"reply": "I'm not sure about that. Try checking the FAQ page for detailed answers, or rephrase your question. I can help with: generating proposals, uploading files, cost estimation, version history, redline exports, API setup, and company standards."}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from config.settings import FLASK_DEBUG, FLASK_HOST, FLASK_PORT
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)

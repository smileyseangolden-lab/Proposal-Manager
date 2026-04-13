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
    ClarificationItem,
    CompanyStandard,
    DocumentTag,
    EquipmentItem,
    Notification,
    Project,
    ProjectDocument,
    Proposal,
    ProposalCorrection,
    ProposalQuestion,
    ProposalVersion,
    ReviewComment,
    ReviewCycle,
    StaffRole,
    TravelExpenseRate,
    User,
    UserRateSheet,
    UserVerticalTemplate,
    VerticalClarificationTemplate,
    db,
)
from proposal_agent import analyze_addendum_impact, generate_proposal, regenerate_section
from proposal_export import markdown_to_docx, markdown_to_rfi_docx, markdown_to_redline_docx
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
    db.create_all()

    # Migrate existing project_documents table to add new columns if missing
    import sqlite3 as _sqlite3
    _db_path = str(Path(__file__).resolve().parent / "data" / "proposal_manager.db")
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

    # Migrate users table to add role column if missing
    _cur.execute("PRAGMA table_info(users)")
    _user_cols = {row[1] for row in _cur.fetchall()}
    if "role" not in _user_cols:
        _cur.execute('ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT "proposal"')
        # Backfill: set existing admins to admin role
        _cur.execute('UPDATE users SET role = "admin" WHERE is_admin = 1')
        _conn.commit()

    # Migrate projects table to add clarification_sub_status if missing
    _cur.execute("PRAGMA table_info(projects)")
    _proj_cols2 = {row[1] for row in _cur.fetchall()}
    if "clarification_sub_status" not in _proj_cols2:
        _cur.execute('ALTER TABLE projects ADD COLUMN clarification_sub_status VARCHAR(30) DEFAULT "none"')
        _conn.commit()

    # Migrate proposal_questions table to add resolution_path if missing
    _cur.execute("PRAGMA table_info(proposal_questions)")
    _pq_cols = {row[1] for row in _cur.fetchall()}
    if "resolution_path" not in _pq_cols:
        _cur.execute('ALTER TABLE proposal_questions ADD COLUMN resolution_path VARCHAR(20) DEFAULT "internal"')
        _conn.commit()

    _conn.close()


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

    return render_template(
        "settings.html",
        rate_sheets=rate_sheets,
        user_templates=user_templates,
        staff_roles=staff_roles,
        equipment_items=equipment_items,
        travel_rates=travel_rates,
        company_standards=company_standards,
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
    if not name:
        flash("Project name is required.", "error")
        return redirect(url_for("new_project"))

    project = Project(
        user_id=current_user.id,
        name=name,
        client_name=client,
    )
    db.session.add(project)
    db.session.commit()
    _log_activity("project_create", f"Created project: {name}", project.id)
    return redirect(url_for("project_upload", project_id=project.id))


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
                    resolution_path=q.get("resolution_path", "internal"),
                )
                db.session.add(pq)

                # Also create ClarificationItem entries for tracking
                ci = ClarificationItem(
                    project_id=project_id,
                    source="ai_detected",
                    resolution_path=q.get("resolution_path", "internal"),
                    category=q.get("category", "general"),
                    question=q["question"],
                    context=q.get("context", ""),
                    ai_suggestion=q.get("ai_suggestion", ""),
                    status="open",
                    created_by=current_user.id,
                )
                db.session.add(ci)

            # Update project sub-status
            project.clarification_sub_status = "clarification_pending"
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
        )
        db.session.add(proposal)
        db.session.flush()  # Get proposal.id before commit

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
    if not project or project.user_id != current_user.id:
        abort(404)

    new_status = request.form.get("status", project.status)
    dollar_amount = request.form.get("dollar_amount")

    project.status = new_status
    if dollar_amount:
        try:
            project.dollar_amount = float(dollar_amount.replace(",", "").replace("$", ""))
        except ValueError:
            pass

    if new_status == "submitted":
        project.submitted_at = datetime.now(timezone.utc)

    db.session.commit()
    _log_activity("project_status_update", f"Status → {new_status}", project_id)
    flash(f"Project status updated to {new_status}.", "success")
    return redirect(url_for("dashboard"))


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

    project = db.session.get(Project, proposal.project_id)
    if not project or (project.user_id != current_user.id and not current_user.is_admin):
        abort(404)

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

    return render_template(
        "proposal.html",
        meta=meta,
        proposal_html=proposal_html,
        action_items=action_items,
        proposal=proposal,
        project=project,
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

    users = User.query.order_by(User.display_name).all()

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

    users = User.query.order_by(User.display_name).all()

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
            user_api_key=current_user.api_key_encrypted or None,
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
        flash(f"Error analyzing addendum: {e}", "error")

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
            user_api_key=current_user.api_key_encrypted or None,
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
        flash(f"Error regenerating section: {e}", "error")

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

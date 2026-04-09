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
    EquipmentItem,
    Project,
    ProjectDocument,
    Proposal,
    ProposalCorrection,
    ProposalQuestion,
    ProposalVersion,
    StaffRole,
    TravelExpenseRate,
    User,
    UserRateSheet,
    UserVerticalTemplate,
    db,
)
from proposal_agent import generate_proposal
from proposal_export import markdown_to_docx, markdown_to_redline_docx
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


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, user_id)


with app.app_context():
    db.create_all()


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
    active_projects = Project.query.filter_by(user_id=current_user.id, status="active").order_by(Project.updated_at.desc()).all()
    past_projects = Project.query.filter(
        Project.user_id == current_user.id,
        Project.status.in_(["submitted", "won", "lost", "archived"]),
    ).order_by(Project.updated_at.desc()).all()

    # Stats
    total = Project.query.filter_by(user_id=current_user.id).count()
    won = Project.query.filter_by(user_id=current_user.id, status="won").count()
    lost = Project.query.filter_by(user_id=current_user.id, status="lost").count()
    decided = won + lost
    win_rate = round((won / decided) * 100) if decided > 0 else 0
    loss_rate = round((lost / decided) * 100) if decided > 0 else 0

    from sqlalchemy import func
    avg_dollar = db.session.query(func.avg(Project.dollar_amount)).filter(
        Project.user_id == current_user.id, Project.dollar_amount > 0
    ).scalar() or 0
    total_dollar = db.session.query(func.sum(Project.dollar_amount)).filter(
        Project.user_id == current_user.id, Project.dollar_amount > 0
    ).scalar() or 0

    total_proposals = Proposal.query.join(Project).filter(Project.user_id == current_user.id).count()

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

    return render_template(
        "dashboard.html",
        active_projects=active_projects,
        past_projects=past_projects,
        stats=stats,
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
        return redirect(url_for("settings") + "#profile")

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
    return redirect(url_for("settings"))


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
    return redirect(url_for("settings"))


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
    if not project or project.user_id != current_user.id:
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
    if not project or project.user_id != current_user.id:
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

    recent_activity = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(50).all()

    return render_template(
        "admin.html",
        users=users,
        user_stats=user_stats,
        company_stats=company_stats,
        recent_activity=recent_activity,
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
    if not current_user.is_admin:
        abort(403)
    if user_id == current_user.id:
        flash("You cannot remove your own admin access.", "error")
        return redirect(url_for("admin_panel"))

    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    user.is_admin = not user.is_admin
    db.session.commit()
    status = "granted" if user.is_admin else "revoked"
    flash(f"Admin access {status} for {user.username}.", "success")
    return redirect(url_for("admin_panel"))


# ---------------------------------------------------------------------------
# Document Library
# ---------------------------------------------------------------------------

@app.route("/documents")
@login_required
def document_library():
    """All documents across all user projects, grouped by project."""
    projects = Project.query.filter_by(user_id=current_user.id).order_by(Project.updated_at.desc()).all()

    project_docs = []
    total_docs = 0
    for p in projects:
        docs = ProjectDocument.query.filter_by(project_id=p.id).order_by(ProjectDocument.uploaded_at.desc()).all()
        if docs:
            project_docs.append({"project": p, "documents": docs})
            total_docs += len(docs)

    return render_template("document_library.html", project_docs=project_docs, total_docs=total_docs)


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

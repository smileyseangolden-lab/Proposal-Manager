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
    Project,
    ProjectDocument,
    Proposal,
    ProposalQuestion,
    User,
    UserRateSheet,
    UserVerticalTemplate,
    db,
)
from proposal_agent import generate_proposal
from proposal_export import markdown_to_docx
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

    return render_template(
        "settings.html",
        rate_sheets=rate_sheets,
        user_templates=user_templates,
        verticals=VERTICALS,
    )


@app.route("/settings/upload-rate-sheet", methods=["POST"])
@login_required
def upload_rate_sheet():
    file = request.files.get("rate_sheet")
    sheet_type = request.form.get("sheet_type", "labor_rates")

    if not file or not _allowed_file(file.filename, RATE_SHEET_EXTENSIONS):
        flash("Please upload an Excel file (.xlsx).", "error")
        return redirect(url_for("settings"))

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
    return redirect(url_for("settings"))


@app.route("/settings/upload-template", methods=["POST"])
@login_required
def upload_user_template():
    file = request.files.get("template_file")
    vertical = request.form.get("vertical", "general")
    template_type = request.form.get("template_type", "proposal")

    if not file or not _allowed_file(file.filename, TEMPLATE_EXTENSIONS):
        flash("Please upload a Word or PDF file.", "error")
        return redirect(url_for("settings"))

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
    return redirect(url_for("settings"))


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
    return render_template(
        "project_upload.html",
        project=project,
        documents=documents,
        verticals=VERTICALS,
    )


@app.route("/projects/<project_id>/generate", methods=["POST"])
@login_required
def project_generate(project_id):
    project = db.session.get(Project, project_id)
    if not project or project.user_id != current_user.id:
        abort(404)

    vertical = request.form.get("vertical", "auto")
    output_format = request.form.get("output_format", "docx")
    template_source = request.form.get("template_source", "default")  # default or user

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

    # Load user rate sheets
    rate_sheet_data = None
    active_sheets = UserRateSheet.query.filter_by(user_id=current_user.id, is_active=True).all()
    if active_sheets:
        rate_sheet_data = {}
        for sheet in active_sheets:
            try:
                rate_sheet_data[sheet.sheet_type] = parse_rate_sheet(sheet.file_path)
            except Exception:
                continue

    # Load user-specific or company-default templates
    user_templates = None
    if template_source == "user":
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

    # Check for company defaults if user chose default
    if template_source == "default" or not user_templates:
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

    try:
        result = generate_proposal(
            combined_text,
            vertical=vertical,
            rate_sheet_data=rate_sheet_data,
            user_templates=user_templates,
            company_name=current_user.company_name,
            user_api_key=current_user.api_key_encrypted or None,
            user_model=current_user.llm_model or None,
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

        user_stats.append({
            "user": user,
            "total_projects": total,
            "proposal_count": proposal_count,
            "won": won,
            "lost": lost,
            "win_rate": round((won / decided) * 100) if decided > 0 else 0,
            "total_dollar": total_dollar,
        })

    # Company-wide totals
    total_projects = Project.query.count()
    total_proposals = Proposal.query.count()
    total_won = Project.query.filter_by(status="won").count()
    total_lost = Project.query.filter_by(status="lost").count()
    total_decided = total_won + total_lost
    company_total_dollar = db.session.query(func.sum(Project.dollar_amount)).filter(
        Project.dollar_amount > 0
    ).scalar() or 0

    company_stats = {
        "total_projects": total_projects,
        "total_proposals": total_proposals,
        "total_won": total_won,
        "total_lost": total_lost,
        "win_rate": round((total_won / total_decided) * 100) if total_decided > 0 else 0,
        "total_dollar": company_total_dollar,
    }

    recent_activity = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(50).all()

    # Company default templates
    company_templates = UserVerticalTemplate.query.filter_by(is_company_default=True).order_by(UserVerticalTemplate.uploaded_at.desc()).all()

    return render_template(
        "admin.html",
        users=users,
        user_stats=user_stats,
        company_stats=company_stats,
        recent_activity=recent_activity,
        company_templates=company_templates,
        verticals=VERTICALS,
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
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from config.settings import FLASK_DEBUG, FLASK_HOST, FLASK_PORT
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)

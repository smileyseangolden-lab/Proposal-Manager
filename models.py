"""Database models for the Proposal Manager application."""

import uuid
from datetime import datetime, timezone

import bcrypt
from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _uuid():
    return uuid.uuid4().hex


def _utcnow():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(200), nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    display_name = db.Column(db.String(200), default="")
    company_name = db.Column(db.String(200), default="")
    font_preference = db.Column(db.String(100), default="Calibri")
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    # LLM settings — per-user overrides
    llm_provider = db.Column(db.String(50), default="anthropic")
    llm_model = db.Column(db.String(100), default="claude-opus-4-6")
    api_key_encrypted = db.Column(db.Text, default="")

    # Relationships
    projects = db.relationship("Project", backref="owner", lazy="dynamic")
    activity_logs = db.relationship("ActivityLog", backref="user", lazy="dynamic")

    def set_password(self, password: str):
        self.password_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

    def check_password(self, password: str) -> bool:
        return bcrypt.checkpw(
            password.encode("utf-8"), self.password_hash.encode("utf-8")
        )


class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(300), nullable=False)
    client_name = db.Column(db.String(300), default="")
    vertical = db.Column(db.String(50), default="general")
    vertical_label = db.Column(db.String(100), default="General")
    status = db.Column(db.String(30), default="active")  # active, submitted, won, lost, archived
    dollar_amount = db.Column(db.Float, default=0.0)
    output_format = db.Column(db.String(20), default="docx")  # docx, pdf, both
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    submitted_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    documents = db.relationship("ProjectDocument", backref="project", lazy="dynamic")
    proposals = db.relationship("Proposal", backref="project", lazy="dynamic")
    questions = db.relationship("ProposalQuestion", backref="project", lazy="dynamic")


class ProjectDocument(db.Model):
    """Uploaded documents for a project (RFP/RFQ, drawings, legal docs, etc.)."""
    __tablename__ = "project_documents"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"), nullable=False)
    filename = db.Column(db.String(500), nullable=False)
    original_filename = db.Column(db.String(500), nullable=False)
    file_type = db.Column(db.String(50), default="rfp")  # rfp, drawing, legal, supporting, rate_sheet
    file_path = db.Column(db.String(1000), nullable=False)
    file_size = db.Column(db.Integer, default=0)
    uploaded_at = db.Column(db.DateTime, default=_utcnow)


class Proposal(db.Model):
    """Generated proposals."""
    __tablename__ = "proposals"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"), nullable=False)
    job_id = db.Column(db.String(50), unique=True, nullable=False)
    document_type = db.Column(db.String(10), default="RFP")
    vertical = db.Column(db.String(50), default="general")
    vertical_label = db.Column(db.String(100), default="General")
    confidence_score = db.Column(db.Integer, default=0)
    action_items_count = db.Column(db.Integer, default=0)
    md_file = db.Column(db.String(500), default="")
    docx_file = db.Column(db.String(500), default="")
    pdf_file = db.Column(db.String(500), default="")
    generated_at = db.Column(db.DateTime, default=_utcnow)


class ProposalQuestion(db.Model):
    """Questions the AI asks the user during proposal generation."""
    __tablename__ = "proposal_questions"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"), nullable=False)
    question = db.Column(db.Text, nullable=False)
    context = db.Column(db.Text, default="")
    answer = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="pending")  # pending, answered, skipped
    created_at = db.Column(db.DateTime, default=_utcnow)
    answered_at = db.Column(db.DateTime, nullable=True)


class UserRateSheet(db.Model):
    """Uploaded rate/price sheets (Excel) for a user."""
    __tablename__ = "user_rate_sheets"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(300), nullable=False)
    sheet_type = db.Column(db.String(50), default="labor_rates")  # labor_rates, product_pricing
    file_path = db.Column(db.String(1000), nullable=False)
    original_filename = db.Column(db.String(500), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=_utcnow)
    is_active = db.Column(db.Boolean, default=True)

    user = db.relationship("User", backref="rate_sheets")


class UserVerticalTemplate(db.Model):
    """User-uploaded or admin-uploaded vertical templates and workflows."""
    __tablename__ = "user_vertical_templates"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    vertical = db.Column(db.String(50), nullable=False)
    template_type = db.Column(db.String(50), default="proposal")  # proposal, workflow, boilerplate
    name = db.Column(db.String(300), nullable=False)
    file_path = db.Column(db.String(1000), nullable=False)
    original_filename = db.Column(db.String(500), nullable=False)
    is_company_default = db.Column(db.Boolean, default=False)  # True = admin-set company default
    uploaded_at = db.Column(db.DateTime, default=_utcnow)

    user = db.relationship("User", backref="vertical_templates")


class ActivityLog(db.Model):
    """Track all user activity for admin reporting."""
    __tablename__ = "activity_logs"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    action = db.Column(db.String(100), nullable=False)
    detail = db.Column(db.Text, default="")
    project_id = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

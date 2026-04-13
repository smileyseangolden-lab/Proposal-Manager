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
    role = db.Column(db.String(20), default="proposal")  # admin, sales, proposal
    created_at = db.Column(db.DateTime, default=_utcnow)

    # LLM settings — per-user overrides
    llm_provider = db.Column(db.String(50), default="anthropic")
    llm_model = db.Column(db.String(100), default="claude-opus-4-6")
    api_key_encrypted = db.Column(db.Text, default="")

    # Relationships
    projects = db.relationship("Project", backref="owner", lazy="dynamic", foreign_keys="Project.user_id")
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
    clarification_sub_status = db.Column(db.String(30), default="none")  # none, clarification_pending, in_review, rfi_sent
    dollar_amount = db.Column(db.Float, default=0.0)
    output_format = db.Column(db.String(20), default="docx")  # docx, pdf, both
    assigned_to = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    submitted_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    documents = db.relationship("ProjectDocument", backref="project", lazy="dynamic")
    proposals = db.relationship("Proposal", backref="project", lazy="dynamic")
    assignee = db.relationship("User", foreign_keys=[assigned_to], backref="assigned_projects")
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
    is_reference = db.Column(db.Boolean, default=False)  # True = available across all projects
    notes = db.Column(db.Text, default="")
    version_group = db.Column(db.String(32), default="")  # Groups document versions together
    version_label = db.Column(db.String(100), default="")  # e.g., "Addendum 1", "Rev B"

    tags = db.relationship("DocumentTag", backref="document", lazy="dynamic", cascade="all, delete-orphan")


class DocumentTag(db.Model):
    """Tags for organizing and filtering documents."""
    __tablename__ = "document_tags"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    document_id = db.Column(db.String(32), db.ForeignKey("project_documents.id"), nullable=False)
    tag = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)


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
    resolution_path = db.Column(db.String(20), default="internal")  # infer, internal, customer
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


class StaffRole(db.Model):
    """Staff role types with hourly sell rates for proposal cost estimation."""
    __tablename__ = "staff_roles"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    role_name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(100), default="")  # e.g., Engineering, Management, Admin
    hourly_rate = db.Column(db.Float, nullable=False)
    overtime_rate = db.Column(db.Float, default=0.0)
    currency = db.Column(db.String(10), default="USD")
    description = db.Column(db.Text, default="")
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    user = db.relationship("User", backref="staff_roles")


class EquipmentItem(db.Model):
    """Equipment and materials price list for BOM estimation."""
    __tablename__ = "equipment_items"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    item_name = db.Column(db.String(300), nullable=False)
    category = db.Column(db.String(100), default="")  # e.g., Electrical, Mechanical, Software
    part_number = db.Column(db.String(100), default="")
    manufacturer = db.Column(db.String(200), default="")
    unit_cost = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(50), default="each")  # each, ft, m, lot, etc.
    currency = db.Column(db.String(10), default="USD")
    description = db.Column(db.Text, default="")
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    user = db.relationship("User", backref="equipment_items")


class TravelExpenseRate(db.Model):
    """Travel and expense rates for cost estimation."""
    __tablename__ = "travel_expense_rates"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    expense_type = db.Column(db.String(100), nullable=False)  # airfare, hotel, per_diem, mileage, rental_car, other
    description = db.Column(db.String(300), default="")
    rate = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(50), default="per day")  # per day, per mile, per trip, per night, etc.
    currency = db.Column(db.String(10), default="USD")
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    user = db.relationship("User", backref="travel_expense_rates")


class CompanyStandard(db.Model):
    """Company standards, posture, boilerplate content for auto-injection into proposals."""
    __tablename__ = "company_standards"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    category = db.Column(db.String(100), nullable=False)  # mission, certifications, past_performance, terms, safety, quality, etc.
    title = db.Column(db.String(300), nullable=False)
    content = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    user = db.relationship("User", backref="company_standards")


class ProposalCorrection(db.Model):
    """Stores AI-vs-human edit patterns for learning. Generated when a human-edited
    version is finalized, comparing it to the original AI output."""
    __tablename__ = "proposal_corrections"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    proposal_id = db.Column(db.String(32), db.ForeignKey("proposals.id"), nullable=False)
    vertical = db.Column(db.String(50), default="general")
    correction_summary = db.Column(db.Text, nullable=False)  # Natural language summary of changes
    original_snippet = db.Column(db.Text, default="")
    corrected_snippet = db.Column(db.Text, default="")
    correction_type = db.Column(db.String(50), default="general")  # tone, structure, pricing, scope, compliance, etc.
    created_at = db.Column(db.DateTime, default=_utcnow)

    user = db.relationship("User", backref="proposal_corrections")
    proposal = db.relationship("Proposal", backref="corrections")


class ProposalVersion(db.Model):
    """Version history for proposal edits. Each save creates a new version."""
    __tablename__ = "proposal_versions"
    __table_args__ = (
        db.UniqueConstraint("proposal_id", "version_number", name="uq_proposal_version"),
    )

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    proposal_id = db.Column(db.String(32), db.ForeignKey("proposals.id"), nullable=False)
    version_number = db.Column(db.Integer, nullable=False, default=1)
    markdown_content = db.Column(db.Text, nullable=False)
    edit_source = db.Column(db.String(20), default="ai")  # ai, human, human_web, human_import
    editor_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    change_summary = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=_utcnow)

    proposal = db.relationship("Proposal", backref="versions")
    editor = db.relationship("User")


class ClarificationItem(db.Model):
    """Tracks clarification questions throughout the proposal lifecycle.

    Can be AI-detected gaps, internal review questions, or customer-facing RFI items.
    Serves as the single source of truth for all open questions on a project.
    """
    __tablename__ = "clarification_items"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"), nullable=False)
    proposal_id = db.Column(db.String(32), db.ForeignKey("proposals.id"), nullable=True)

    # Classification
    source = db.Column(db.String(30), default="ai_detected")  # ai_detected, human_review, addendum
    resolution_path = db.Column(db.String(20), default="internal")  # infer, internal, customer
    category = db.Column(db.String(50), default="general")  # scope, pricing, compliance, schedule, technical, general
    priority = db.Column(db.String(20), default="medium")  # low, medium, high, critical
    is_parking_lot = db.Column(db.Boolean, default=False)  # Phase 4: non-blocking question

    # Content
    question = db.Column(db.Text, nullable=False)
    context = db.Column(db.Text, default="")
    ai_suggestion = db.Column(db.Text, default="")  # AI's proposed answer for 'infer' path items
    proposal_section = db.Column(db.String(300), default="")  # Which section this relates to

    # Assignment
    assigned_to_user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    assigned_to_role = db.Column(db.String(20), default="")  # admin, sales, proposal, or RACI roles like BDM, AE

    # Response tracking
    status = db.Column(db.String(30), default="draft")  # draft, open, sent, response_received, incorporated, resolved, skipped
    response = db.Column(db.Text, default="")
    responded_by = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    responded_at = db.Column(db.DateTime, nullable=True)
    incorporated_at = db.Column(db.DateTime, nullable=True)

    # RFI letter tracking (Phase 3)
    rfi_reference_id = db.Column(db.String(50), default="")  # e.g. RFI-001
    rfi_sent_at = db.Column(db.DateTime, nullable=True)

    # Confidence impact (Phase 4)
    confidence_impact = db.Column(db.Integer, default=0)  # How many points this drags the score down

    created_by = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    # Relationships
    project = db.relationship("Project", backref="clarification_items")
    proposal = db.relationship("Proposal", backref="clarification_items")
    assignee = db.relationship("User", foreign_keys=[assigned_to_user_id], backref="assigned_clarifications")
    responder = db.relationship("User", foreign_keys=[responded_by])
    creator = db.relationship("User", foreign_keys=[created_by])


class ReviewComment(db.Model):
    """Section-level review comments on proposals (Phase 2).

    Allows reviewers to attach typed comments/questions to specific
    proposal sections, with assignment and resolution tracking.
    """
    __tablename__ = "review_comments"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    proposal_id = db.Column(db.String(32), db.ForeignKey("proposals.id"), nullable=False)
    review_cycle_id = db.Column(db.String(32), db.ForeignKey("review_cycles.id"), nullable=True)

    # Location in proposal
    section_heading = db.Column(db.String(500), default="")  # Markdown heading the comment is attached to
    line_reference = db.Column(db.Text, default="")  # Quoted text the comment refers to

    # Content
    comment_type = db.Column(db.String(20), default="comment")  # comment, question, change_request, approval
    content = db.Column(db.Text, nullable=False)
    resolution_note = db.Column(db.Text, default="")

    # Assignment
    author_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    assigned_to_user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    assigned_to_role = db.Column(db.String(20), default="")

    # Status
    status = db.Column(db.String(20), default="open")  # open, resolved, wont_fix
    resolved_by = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)

    # Link to clarification register (questions/change_requests create ClarificationItems)
    clarification_item_id = db.Column(db.String(32), db.ForeignKey("clarification_items.id"), nullable=True)

    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    # Relationships
    proposal = db.relationship("Proposal", backref="review_comments")
    author = db.relationship("User", foreign_keys=[author_id], backref="authored_review_comments")
    assignee = db.relationship("User", foreign_keys=[assigned_to_user_id])
    resolver = db.relationship("User", foreign_keys=[resolved_by])
    review_cycle = db.relationship("ReviewCycle", backref="comments")
    clarification_item = db.relationship("ClarificationItem", backref="review_comments")


class ReviewCycle(db.Model):
    """Tracks review rounds for a proposal (Phase 2).

    Each review cycle (Review 1, Review 2, Final) groups review comments
    and tracks completion progress.
    """
    __tablename__ = "review_cycles"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    proposal_id = db.Column(db.String(32), db.ForeignKey("proposals.id"), nullable=False)
    cycle_number = db.Column(db.Integer, nullable=False, default=1)
    name = db.Column(db.String(100), default="")  # e.g. "Review 1", "Final Review"
    status = db.Column(db.String(20), default="active")  # active, completed
    started_by = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    started_at = db.Column(db.DateTime, default=_utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    proposal = db.relationship("Proposal", backref="review_cycles")
    initiator = db.relationship("User", foreign_keys=[started_by])


class VerticalClarificationTemplate(db.Model):
    """Pre-defined clarification questions per vertical (Phase 4).

    Different industries have different common gaps. This stores
    vertical-specific questions the AI should always check for.
    """
    __tablename__ = "vertical_clarification_templates"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    vertical = db.Column(db.String(50), nullable=False)
    question = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), default="general")
    priority = db.Column(db.String(20), default="medium")
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow)


class Notification(db.Model):
    """In-app notifications for role-based alerts."""
    __tablename__ = "notifications"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    category = db.Column(db.String(50), nullable=False)  # proposal_generated, rfp_uploaded, assignment, role_change
    title = db.Column(db.String(300), nullable=False)
    message = db.Column(db.Text, default="")
    link = db.Column(db.String(500), default="")
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    user = db.relationship("User", backref="notifications")


class ActivityLog(db.Model):
    """Track all user activity for admin reporting."""
    __tablename__ = "activity_logs"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    action = db.Column(db.String(100), nullable=False)
    detail = db.Column(db.Text, default="")
    project_id = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

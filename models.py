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

    # Multi-stakeholder review lifecycle (Part 3)
    # States: draft, in_review, revision_requested, internally_approved,
    #         submitted_to_customer, customer_feedback, customer_approved,
    #         customer_declined, won, lost
    review_status = db.Column(db.String(40), default="draft")
    review_deadline = db.Column(db.DateTime, nullable=True)


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


# ---------------------------------------------------------------------------
# Part 3: Multi-Stakeholder Review & Revision Workflow
# ---------------------------------------------------------------------------


class ProposalReviewer(db.Model):
    """A stakeholder assigned to review a specific proposal.

    Review roles are per-proposal labels (engineering, accounting, sales, legal, custom),
    NOT global User roles. Any user can be assigned with any review_role on a given
    proposal. A user may only appear once per proposal; to wear multiple hats,
    use 'other' or pick the most relevant role.
    """
    __tablename__ = "proposal_reviewers"
    __table_args__ = (
        db.UniqueConstraint("proposal_id", "user_id",
                            name="uq_proposal_reviewer"),
    )

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    proposal_id = db.Column(db.String(32), db.ForeignKey("proposals.id"), nullable=False)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    # engineering, accounting, sales, legal, operations, other
    review_role = db.Column(db.String(40), nullable=False, default="engineering")
    is_required = db.Column(db.Boolean, default=True)
    assigned_at = db.Column(db.DateTime, default=_utcnow)
    assigned_by = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    deadline = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, default="")

    proposal = db.relationship("Proposal", backref="reviewers")
    user = db.relationship("User", foreign_keys=[user_id], backref="review_assignments")
    assigner = db.relationship("User", foreign_keys=[assigned_by])


class ProposalApproval(db.Model):
    """An approval/request-changes decision a reviewer files against a specific
    proposal version. Approvals are tied to version_id so re-reviews of new
    versions are unambiguous and the audit history is preserved."""
    __tablename__ = "proposal_approvals"
    __table_args__ = (
        db.UniqueConstraint("proposal_id", "version_id", "user_id",
                            name="uq_proposal_approval"),
    )

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    proposal_id = db.Column(db.String(32), db.ForeignKey("proposals.id"), nullable=False)
    version_id = db.Column(db.String(32), db.ForeignKey("proposal_versions.id"), nullable=False)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    review_role = db.Column(db.String(40), default="engineering")
    decision = db.Column(db.String(30), nullable=False)  # approved, requested_changes
    note = db.Column(db.Text, default="")
    decided_at = db.Column(db.DateTime, default=_utcnow)

    proposal = db.relationship("Proposal", backref="approvals")
    version = db.relationship("ProposalVersion", backref="approvals")
    user = db.relationship("User")


class RevisionRequest(db.Model):
    """A structured revision request filed by an internal reviewer or sourced
    from a customer. The AI consumes these in batch when the owner triggers
    a new version generation."""
    __tablename__ = "revision_requests"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    proposal_id = db.Column(db.String(32), db.ForeignKey("proposals.id"), nullable=False)
    author_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    # internal_engineering, internal_accounting, internal_sales, internal_legal,
    # internal_other, customer, other
    source = db.Column(db.String(40), nullable=False, default="internal_other")
    # pricing, scope, resources, schedule, terms, compliance, tone, structure, other
    category = db.Column(db.String(40), default="other")
    directive = db.Column(db.Text, nullable=False)
    target_section = db.Column(db.String(200), default="")
    # pending, applied, deferred, withdrawn
    status = db.Column(db.String(30), default="pending")
    applied_in_version_id = db.Column(db.String(32), db.ForeignKey("proposal_versions.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    proposal = db.relationship("Proposal", backref="revision_requests")
    author = db.relationship("User")
    applied_in_version = db.relationship("ProposalVersion", foreign_keys=[applied_in_version_id])


class ProposalRevisionBatch(db.Model):
    """Groups the revision requests that were applied in a single AI revision
    run. Provides auditability: 'v3 was generated by applying these N requests.'"""
    __tablename__ = "proposal_revision_batches"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    proposal_id = db.Column(db.String(32), db.ForeignKey("proposals.id"), nullable=False)
    from_version_id = db.Column(db.String(32), db.ForeignKey("proposal_versions.id"), nullable=False)
    to_version_id = db.Column(db.String(32), db.ForeignKey("proposal_versions.id"), nullable=False)
    triggered_by = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    request_count = db.Column(db.Integer, default=0)
    ai_change_summary = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=_utcnow)

    proposal = db.relationship("Proposal", backref="revision_batches")
    from_version = db.relationship("ProposalVersion", foreign_keys=[from_version_id])
    to_version = db.relationship("ProposalVersion", foreign_keys=[to_version_id])
    user = db.relationship("User")


class ProposalStatusHistory(db.Model):
    """Audit trail of every lifecycle transition on a proposal's review_status."""
    __tablename__ = "proposal_status_history"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    proposal_id = db.Column(db.String(32), db.ForeignKey("proposals.id"), nullable=False)
    from_status = db.Column(db.String(40), default="")
    to_status = db.Column(db.String(40), nullable=False)
    actor_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    note = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=_utcnow)

    proposal = db.relationship("Proposal", backref="status_history")
    actor = db.relationship("User")


class RevisionTemplate(db.Model):
    """Per-user parameterized revision request templates (e.g., 'Bump margins by X%').
    Used as presets so reviewers don't retype common directives."""
    __tablename__ = "revision_templates"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(40), default="other")
    # Directive body, may contain {placeholder} tokens
    directive_template = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=_utcnow)

    user = db.relationship("User", backref="revision_templates")

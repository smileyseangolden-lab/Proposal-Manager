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


class Organization(db.Model):
    """A company workspace (tenant). All posture, projects, and members are
    scoped to an organization. Created at signup or via invitation."""
    __tablename__ = "organizations"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    name = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    # Billing (Phase 5)
    plan = db.Column(db.String(30), default="free")  # free, pro, business
    stripe_customer_id = db.Column(db.String(100), default="")
    stripe_subscription_id = db.Column(db.String(100), default="")
    billing_status = db.Column(db.String(30), default="")  # active, past_due, canceled
    trial_ends_at = db.Column(db.DateTime, nullable=True)

    # Integrations (Phase 6)
    slack_webhook_url = db.Column(db.String(1000), default="")
    outbound_webhook_url = db.Column(db.String(1000), default="")

    members = db.relationship("User", backref="organization", lazy="dynamic",
                              foreign_keys="User.org_id")


class OrgInvitation(db.Model):
    """An email invitation to join an organization with a given role."""
    __tablename__ = "org_invitations"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    org_id = db.Column(db.String(32), db.ForeignKey("organizations.id"), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default="proposal")  # admin, sales, proposal
    token = db.Column(db.String(64), unique=True, nullable=False, default=lambda: uuid.uuid4().hex + uuid.uuid4().hex)
    invited_by = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
    accepted_at = db.Column(db.DateTime, nullable=True)
    accepted_user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    organization = db.relationship("Organization", backref="invitations")
    inviter = db.relationship("User", foreign_keys=[invited_by])


class UserToken(db.Model):
    """Single-use, time-limited tokens for password reset and email verification."""
    __tablename__ = "user_tokens"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    purpose = db.Column(db.String(20), nullable=False)  # reset, verify
    token = db.Column(db.String(64), unique=True, nullable=False,
                      default=lambda: uuid.uuid4().hex + uuid.uuid4().hex)
    created_at = db.Column(db.DateTime, default=_utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
    used_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User")


class BackgroundJob(db.Model):
    """DB-backed background job for long-running AI work (generation, revision,
    scope drafting). A small in-process worker pool claims and runs these."""
    __tablename__ = "background_jobs"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    org_id = db.Column(db.String(32), nullable=True)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    kind = db.Column(db.String(50), nullable=False)  # generate_proposal, revise_proposal, draft_scope
    status = db.Column(db.String(20), default="queued")  # queued, running, done, failed
    phase = db.Column(db.String(50), default="")
    message = db.Column(db.Text, default="")
    payload = db.Column(db.Text, default="{}")  # JSON
    result = db.Column(db.Text, default="{}")  # JSON (e.g. {"redirect": "/proposal/..."} )
    error = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=_utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User")


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    org_id = db.Column(db.String(32), db.ForeignKey("organizations.id"), nullable=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(200), nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    display_name = db.Column(db.String(200), default="")
    company_name = db.Column(db.String(200), default="")
    font_preference = db.Column(db.String(100), default="Calibri")
    is_admin = db.Column(db.Boolean, default=False)
    role = db.Column(db.String(20), default="proposal")  # admin, sales, proposal
    email_verified = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    # Login throttling — cross-worker, per-account (complements the in-process
    # per-IP limiter, which doesn't survive multiple gunicorn workers).
    failed_login_count = db.Column(db.Integer, default=0)
    lockout_until = db.Column(db.DateTime, nullable=True)

    # LLM settings — per-user overrides
    llm_provider = db.Column(db.String(50), default="anthropic")
    llm_model = db.Column(db.String(100), default="claude-opus-4-6")
    api_key_encrypted = db.Column(db.Text, default="")

    # Company logo / branding
    company_logo_path = db.Column(db.String(1000), default="")
    company_logo_original_name = db.Column(db.String(500), default="")
    company_logo_use_in_proposals = db.Column(db.Boolean, default=True)
    company_logo_placement = db.Column(db.String(20), default="top_left")  # top_left, center
    company_logo_show_on_cover = db.Column(db.Boolean, default=True)

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
    org_id = db.Column(db.String(32), db.ForeignKey("organizations.id"), nullable=True)
    name = db.Column(db.String(300), nullable=False)
    client_name = db.Column(db.String(300), default="")
    client_email = db.Column(db.String(200), default="")  # customer contact for sends
    request_type = db.Column(db.String(10), default="")  # rfp, rfq, rom, ''
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

    # Deadline / calendar tracking
    due_date = db.Column(db.DateTime, nullable=True)

    # Win/Loss analysis (captured when project is marked won or lost)
    close_reason = db.Column(db.Text, default="")  # Narrative reason for win/loss
    close_category = db.Column(db.String(50), default="")  # price, scope, schedule, relationship, technical, compliance, other
    competitor_name = db.Column(db.String(300), default="")
    closed_at = db.Column(db.DateTime, nullable=True)

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


class ProjectScope(db.Model):
    """AI-drafted Scope of Work for a project, reviewed and approved by a human
    before proposal generation. One scope per project (regenerating replaces it)."""
    __tablename__ = "project_scopes"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"), unique=True, nullable=False)
    status = db.Column(db.String(20), default="draft")  # draft, approved
    ai_summary = db.Column(db.Text, default="")
    vertical = db.Column(db.String(50), default="general")
    vertical_label = db.Column(db.String(100), default="General")
    generated_at = db.Column(db.DateTime, default=_utcnow)
    approved_at = db.Column(db.DateTime, nullable=True)
    approved_by = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)

    project = db.relationship("Project", backref=db.backref("scope", uselist=False))
    approver = db.relationship("User")


class ScopeItem(db.Model):
    """A single line item in the Scope of Work. AI-proposed items can be
    included or removed by the human reviewer; humans can add their own."""
    __tablename__ = "scope_items"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    scope_id = db.Column(db.String(32), db.ForeignKey("project_scopes.id"), nullable=False)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"), nullable=False)
    item_text = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), default="general")  # engineering, installation, commissioning, documentation, management, general
    source = db.Column(db.String(10), default="ai")  # ai, human
    status = db.Column(db.String(20), default="included")  # included, removed
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=_utcnow)

    scope = db.relationship("ProjectScope", backref=db.backref("items", lazy="dynamic", cascade="all, delete-orphan"))


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
    org_id = db.Column(db.String(32), db.ForeignKey("organizations.id"), nullable=True)
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
    org_id = db.Column(db.String(32), db.ForeignKey("organizations.id"), nullable=True)
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
    org_id = db.Column(db.String(32), db.ForeignKey("organizations.id"), nullable=True)
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
    org_id = db.Column(db.String(32), db.ForeignKey("organizations.id"), nullable=True)
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
    org_id = db.Column(db.String(32), db.ForeignKey("organizations.id"), nullable=True)
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
    org_id = db.Column(db.String(32), db.ForeignKey("organizations.id"), nullable=True)
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
    org_id = db.Column(db.String(32), db.ForeignKey("organizations.id"), nullable=True)
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


class ProposalComment(db.Model):
    """Team review comments on a proposal. Supports section anchors and resolution."""
    __tablename__ = "proposal_comments"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    proposal_id = db.Column(db.String(32), db.ForeignKey("proposals.id"), nullable=False)
    author_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=False)
    section_anchor = db.Column(db.String(300), default="")  # Heading text or selector the comment refers to
    body = db.Column(db.Text, nullable=False)
    is_resolved = db.Column(db.Boolean, default=False)
    resolved_by = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    proposal = db.relationship("Proposal", backref="comments")
    author = db.relationship("User", foreign_keys=[author_id], backref="proposal_comments")
    resolver = db.relationship("User", foreign_keys=[resolved_by])


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
    org_id = db.Column(db.String(32), db.ForeignKey("organizations.id"), nullable=True)
    name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(40), default="other")
    # Directive body, may contain {placeholder} tokens
    directive_template = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=_utcnow)

    user = db.relationship("User", backref="revision_templates")


# ---------------------------------------------------------------------------
# Phase 3: Customer share portal
# ---------------------------------------------------------------------------


class ProposalShare(db.Model):
    """A secure, tokenized share of a proposal with a customer. Renders a
    read-only branded view at /p/<token>; tracks views and optionally lets the
    customer comment or record an accept/decline decision."""
    __tablename__ = "proposal_shares"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    proposal_id = db.Column(db.String(32), db.ForeignKey("proposals.id"), nullable=False)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False,
                      default=lambda: uuid.uuid4().hex + uuid.uuid4().hex)
    created_by = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    customer_email = db.Column(db.String(200), default="")
    version_number = db.Column(db.Integer, default=0)  # snapshot version shown

    allow_comments = db.Column(db.Boolean, default=True)
    allow_decision = db.Column(db.Boolean, default=True)

    view_count = db.Column(db.Integer, default=0)
    last_viewed_at = db.Column(db.DateTime, nullable=True)

    # Customer's decision recorded through the portal
    decision = db.Column(db.String(20), default="")  # accepted, declined
    decision_note = db.Column(db.Text, default="")
    decided_at = db.Column(db.DateTime, nullable=True)

    revoked_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    proposal = db.relationship("Proposal", backref="shares")
    project = db.relationship("Project", backref="shares")
    creator = db.relationship("User")


class ShareView(db.Model):
    """A single customer view of a shared proposal (for open analytics)."""
    __tablename__ = "share_views"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    share_id = db.Column(db.String(32), db.ForeignKey("proposal_shares.id"), nullable=False)
    viewed_at = db.Column(db.DateTime, default=_utcnow)
    ip = db.Column(db.String(64), default="")
    user_agent = db.Column(db.String(400), default="")

    share = db.relationship("ProposalShare", backref="views")


# ---------------------------------------------------------------------------
# Phase 4: Structured pricing estimate
# ---------------------------------------------------------------------------


class ProposalEstimate(db.Model):
    """A structured, editable cost estimate for a proposal. The AI drafts line
    items from the RFP + the org's rates; humans edit them in a grid with live
    totals; the estimate renders into the proposal's Pricing section."""
    __tablename__ = "proposal_estimates"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    proposal_id = db.Column(db.String(32), db.ForeignKey("proposals.id"), nullable=False)
    project_id = db.Column(db.String(32), db.ForeignKey("projects.id"), nullable=False)
    org_id = db.Column(db.String(32), db.ForeignKey("organizations.id"), nullable=True)
    currency = db.Column(db.String(10), default="USD")
    markup_pct = db.Column(db.Float, default=0.0)  # applied to subtotal
    status = db.Column(db.String(20), default="draft")  # draft, final
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    proposal = db.relationship("Proposal", backref=db.backref("estimate", uselist=False))
    items = db.relationship("EstimateLineItem", backref="estimate",
                            lazy="dynamic", cascade="all, delete-orphan")


class EstimateLineItem(db.Model):
    """One line in a ProposalEstimate. total = quantity * unit_cost.
    For labor, quantity is hours and unit_cost is the hourly rate."""
    __tablename__ = "estimate_line_items"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    estimate_id = db.Column(db.String(32), db.ForeignKey("proposal_estimates.id"), nullable=False)
    kind = db.Column(db.String(20), default="labor")  # labor, equipment, travel, other
    description = db.Column(db.String(400), nullable=False)
    quantity = db.Column(db.Float, default=0.0)
    unit = db.Column(db.String(40), default="")  # hrs, each, trip, ...
    unit_cost = db.Column(db.Float, default=0.0)
    sort_order = db.Column(db.Integer, default=0)

    @property
    def total(self) -> float:
        return (self.quantity or 0) * (self.unit_cost or 0)


# ---------------------------------------------------------------------------
# AI cost metering
# ---------------------------------------------------------------------------


class LlmUsage(db.Model):
    """One row per LLM API call: token counts + estimated cost. Written after
    each generation/revision/etc. so AI spend can be metered per org (monthly
    budget enforcement) and reported platform-wide. Deliberately cross-tenant."""
    __tablename__ = "llm_usage"

    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    org_id = db.Column(db.String(32), db.ForeignKey("organizations.id"), nullable=True, index=True)
    user_id = db.Column(db.String(32), db.ForeignKey("users.id"), nullable=True)
    job_id = db.Column(db.String(32), nullable=True)
    kind = db.Column(db.String(50), default="")  # generate_proposal, revise_proposal, ...
    provider = db.Column(db.String(50), default="anthropic")
    model = db.Column(db.String(100), default="")
    input_tokens = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    est_cost_usd = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)

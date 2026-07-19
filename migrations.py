"""Lightweight, dialect-agnostic schema migrations.

Works on both SQLite (dev) and PostgreSQL (production). Strategy:
  1. db.create_all() creates any brand-new tables from the models.
  2. A declarative list of (table, column, ddl_type) pairs adds columns that
     were introduced after a deployment's tables already existed.
  3. Data backfills bring pre-multi-tenancy rows into the org model: every
     user without an organization is grouped into a single default org
     (preserving the old "everyone in one workspace" behavior), and org_id
     is stamped onto their projects and posture rows.
"""

import logging

from sqlalchemy import inspect, text

from models import Organization, Project, User, db

logger = logging.getLogger(__name__)

# Columns added after initial schema. ALTER TABLE ... ADD COLUMN with these
# types must be valid on BOTH SQLite and PostgreSQL — booleans use the TRUE/FALSE
# keywords because PostgreSQL rejects integer literals (0/1) for a boolean default.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # Legacy (pre-Phase-1) migrations, preserved from the old SQLite-only block
    ("project_documents", "is_reference", "BOOLEAN DEFAULT FALSE"),
    ("project_documents", "notes", "TEXT DEFAULT ''"),
    ("project_documents", "version_group", "VARCHAR(32) DEFAULT ''"),
    ("project_documents", "version_label", "VARCHAR(100) DEFAULT ''"),
    ("projects", "assigned_to", "VARCHAR(32)"),
    ("projects", "due_date", "TIMESTAMP"),
    ("projects", "close_reason", "TEXT DEFAULT ''"),
    ("projects", "close_category", "VARCHAR(50) DEFAULT ''"),
    ("projects", "competitor_name", "VARCHAR(300) DEFAULT ''"),
    ("projects", "closed_at", "TIMESTAMP"),
    ("users", "role", "VARCHAR(20) DEFAULT 'proposal'"),
    ("users", "company_logo_path", "VARCHAR(1000) DEFAULT ''"),
    ("users", "company_logo_original_name", "VARCHAR(500) DEFAULT ''"),
    ("users", "company_logo_use_in_proposals", "BOOLEAN DEFAULT TRUE"),
    ("users", "company_logo_placement", "VARCHAR(20) DEFAULT 'top_left'"),
    ("users", "company_logo_show_on_cover", "BOOLEAN DEFAULT TRUE"),
    ("proposals", "review_status", "VARCHAR(40) DEFAULT 'draft'"),
    ("proposals", "review_deadline", "TIMESTAMP"),
    ("projects", "clarification_sub_status", "VARCHAR(30) DEFAULT 'none'"),
    ("proposal_questions", "resolution_path", "VARCHAR(20) DEFAULT 'internal'"),
    # Phase 1: multi-tenancy
    ("users", "org_id", "VARCHAR(32)"),
    ("projects", "org_id", "VARCHAR(32)"),
    ("user_rate_sheets", "org_id", "VARCHAR(32)"),
    ("user_vertical_templates", "org_id", "VARCHAR(32)"),
    ("staff_roles", "org_id", "VARCHAR(32)"),
    ("equipment_items", "org_id", "VARCHAR(32)"),
    ("travel_expense_rates", "org_id", "VARCHAR(32)"),
    ("company_standards", "org_id", "VARCHAR(32)"),
    ("proposal_corrections", "org_id", "VARCHAR(32)"),
    ("revision_templates", "org_id", "VARCHAR(32)"),
    # Phase 2: auth hardening
    ("users", "email_verified", "BOOLEAN DEFAULT FALSE"),
    # Phase 3: customer send + ROM
    ("projects", "request_type", "VARCHAR(10) DEFAULT ''"),
    ("projects", "client_email", "VARCHAR(200) DEFAULT ''"),
    ("proposals", "pdf_file", "VARCHAR(500) DEFAULT ''"),
    # Phase 5: billing
    ("organizations", "plan", "VARCHAR(30) DEFAULT 'free'"),
    ("organizations", "stripe_customer_id", "VARCHAR(100) DEFAULT ''"),
    ("organizations", "stripe_subscription_id", "VARCHAR(100) DEFAULT ''"),
    ("organizations", "billing_status", "VARCHAR(30) DEFAULT ''"),
    ("organizations", "trial_ends_at", "TIMESTAMP"),
    # Phase 6: integrations
    ("organizations", "slack_webhook_url", "VARCHAR(1000) DEFAULT ''"),
    ("organizations", "outbound_webhook_url", "VARCHAR(1000) DEFAULT ''"),
    # Hardening: cross-worker login throttling
    ("users", "failed_login_count", "INTEGER DEFAULT 0"),
    ("users", "lockout_until", "TIMESTAMP"),
    # Platform-admin dashboard
    ("users", "platform_owner", "BOOLEAN DEFAULT FALSE"),
    # P1 hardening: member offboarding
    ("users", "is_active", "BOOLEAN DEFAULT TRUE"),
    # P1: org-level branding (proposals branded per workspace, not per user)
    ("organizations", "logo_path", "VARCHAR(1000) DEFAULT ''"),
    ("organizations", "logo_original_name", "VARCHAR(500) DEFAULT ''"),
    ("organizations", "logo_use_in_proposals", "BOOLEAN DEFAULT TRUE"),
    ("organizations", "logo_placement", "VARCHAR(20) DEFAULT 'top_left'"),
    ("organizations", "logo_show_on_cover", "BOOLEAN DEFAULT TRUE"),
    # P1: parse receipt captured at upload (NULL = never checked)
    ("project_documents", "text_chars", "INTEGER"),
]

# Indexes on hot filter / foreign-key columns. CREATE INDEX IF NOT EXISTS is
# valid on SQLite and PostgreSQL and idempotent, so this runs safely every boot.
# (name, table, column)
_INDEX_MIGRATIONS: list[tuple[str, str, str]] = [
    ("ix_projects_org_id", "projects", "org_id"),
    ("ix_projects_user_id", "projects", "user_id"),
    ("ix_projects_assigned_to", "projects", "assigned_to"),
    ("ix_projects_status", "projects", "status"),
    ("ix_users_org_id", "users", "org_id"),
    ("ix_proposals_project_id", "proposals", "project_id"),
    ("ix_project_documents_project_id", "project_documents", "project_id"),
    ("ix_proposal_versions_proposal_id", "proposal_versions", "proposal_id"),
    ("ix_clarification_items_project_id", "clarification_items", "project_id"),
    ("ix_notifications_user_id", "notifications", "user_id"),
    ("ix_activity_logs_user_id", "activity_logs", "user_id"),
    ("ix_background_jobs_org_id", "background_jobs", "org_id"),
    ("ix_background_jobs_status", "background_jobs", "status"),
    ("ix_background_jobs_created_at", "background_jobs", "created_at"),
]

# Tables that carry a per-user org_id needing backfill from the owning user.
_ORG_BACKFILL_TABLES = [
    "projects",
    "user_rate_sheets",
    "user_vertical_templates",
    "staff_roles",
    "equipment_items",
    "travel_expense_rates",
    "company_standards",
    "proposal_corrections",
    "revision_templates",
]


def _existing_columns(table: str) -> set[str]:
    inspector = inspect(db.engine)
    try:
        return {c["name"] for c in inspector.get_columns(table)}
    except Exception:
        return set()


def _add_missing_columns():
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    cols_cache: dict[str, set[str]] = {}
    for table, column, ddl_type in _COLUMN_MIGRATIONS:
        if table not in tables:
            continue
        if table not in cols_cache:
            cols_cache[table] = _existing_columns(table)
        if column in cols_cache[table]:
            continue
        # Each ALTER runs in its own transaction so a single failed/legacy
        # migration can't abort the whole schema bootstrap and brick startup
        # for a live paid database.
        try:
            db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
            db.session.commit()
            cols_cache[table].add(column)
        except Exception:
            db.session.rollback()
            logger.exception("Migration: failed to add column %s.%s", table, column)


def _add_indexes():
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    for name, table, column in _INDEX_MIGRATIONS:
        if table not in tables:
            continue
        try:
            db.session.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})"))
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Migration: failed to create index %s", name)


def _backfill_organizations():
    """Group all pre-tenancy users into one default org, matching the old
    single-workspace behavior, and stamp org_id onto their data rows."""
    orphans = User.query.filter(User.org_id.is_(None)).all()
    if orphans:
        # Name the default org after the first admin's company, if any
        name = ""
        for u in orphans:
            if u.is_admin and (u.company_name or "").strip():
                name = u.company_name.strip()
                break
        if not name:
            for u in orphans:
                if (u.company_name or "").strip():
                    name = u.company_name.strip()
                    break
        org = Organization(name=name or "My Workspace")
        db.session.add(org)
        db.session.flush()
        for u in orphans:
            u.org_id = org.id
        db.session.commit()

    # Stamp org_id on all org-scoped rows from their owning user
    for table in _ORG_BACKFILL_TABLES:
        db.session.execute(text(
            f"UPDATE {table} SET org_id = "
            f"(SELECT org_id FROM users WHERE users.id = {table}.user_id) "
            f"WHERE org_id IS NULL"
        ))
    db.session.commit()

    # Backfill legacy role column (previously done in raw SQLite)
    db.session.execute(text(
        "UPDATE users SET role = 'admin' WHERE is_admin AND (role IS NULL OR role = '' OR role = 'proposal')"
    ))
    db.session.commit()


def _backfill_active_flag():
    """Rows that predate users.is_active get NULL on some engines — a NULL is
    falsy to flask-login and would lock every legacy user out. Force TRUE."""
    db.session.execute(text("UPDATE users SET is_active = TRUE WHERE is_active IS NULL"))
    db.session.commit()


def _backfill_org_branding():
    """Move per-user branding up to the organization (one-time).

    Historically the logo/company identity lived on User rows, so proposals
    were branded by whoever owned the project. Copy the best candidate (an
    admin's logo, else any member's) onto the org so branding is consistent
    workspace-wide. User columns are left in place but are no longer read."""
    orgs = Organization.query.filter(
        db.or_(Organization.logo_path.is_(None), Organization.logo_path == "")
    ).all()
    changed = False
    for org in orgs:
        donor = (
            User.query.filter(User.org_id == org.id,
                              User.company_logo_path.isnot(None),
                              User.company_logo_path != "")
            .order_by(User.is_admin.desc(), User.created_at.asc())
            .first()
        )
        if not donor:
            continue
        org.logo_path = donor.company_logo_path
        org.logo_original_name = donor.company_logo_original_name or ""
        org.logo_use_in_proposals = bool(donor.company_logo_use_in_proposals)
        org.logo_placement = donor.company_logo_placement or "top_left"
        org.logo_show_on_cover = bool(donor.company_logo_show_on_cover)
        changed = True

    # Orgs still carrying the auto-generated "<name>'s Workspace" label pick up
    # the admin's company name for proposal branding, if one was ever set.
    for org in Organization.query.filter(Organization.name.like("%'s Workspace")).all():
        admin = (User.query.filter(User.org_id == org.id, User.is_admin.is_(True))
                 .order_by(User.created_at.asc()).first())
        if admin and (admin.company_name or "").strip():
            org.name = admin.company_name.strip()
            changed = True
    if changed:
        db.session.commit()


def _add_unique_email_index():
    """Case-insensitive unique index on users.email (P1: duplicate emails break
    password reset and verification). Best-effort: if legacy duplicates exist
    the index can't be created — log loudly so the operator can dedupe, while
    the app-level uniqueness checks still stop NEW duplicates."""
    try:
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email_lower ON users (lower(email))"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "Migration: could not create unique email index — duplicate emails "
            "likely exist in users; dedupe them manually."
        )


def ensure_schema():
    """Create tables, add missing columns, and run data backfills.
    Must be called inside an app context."""
    try:
        db.create_all()
    except Exception as e:
        # Tolerate races between gunicorn workers bootstrapping concurrently
        if "already exists" not in str(e).lower():
            raise
        db.session.rollback()
    _add_missing_columns()
    _add_indexes()
    _backfill_organizations()
    _backfill_active_flag()
    _backfill_org_branding()
    _add_unique_email_index()

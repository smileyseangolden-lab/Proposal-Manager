"""Application settings loaded from environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
VERTICALS_DIR = BASE_DIR / "verticals"
TEMPLATES_DIR = BASE_DIR / "templates" / "proposal_boilerplate"
REFERENCE_DIR = BASE_DIR / "reference_documents"
UPLOADS_DIR = BASE_DIR / "uploads"
GENERATED_DIR = BASE_DIR / "generated_proposals"
WORKFLOW_PATH = BASE_DIR / "config" / "workflow.md"

# Industry verticals
VERTICALS = {
    "data_center": {
        "label": "Data Center / Mission Critical",
        "description": "MCT proposals for hyperscale, colocation, and OSI data center projects (BMS, EPMS)",
        "dir": VERTICALS_DIR / "data_center",
    },
    "life_science": {
        "label": "Life Science & Pharmaceutical",
        "description": "Proposals for pharma, biotech, and life science facilities (GMP, FDA, EU Annex)",
        "dir": VERTICALS_DIR / "life_science",
    },
    "food_beverage": {
        "label": "Food & Beverage / CPG",
        "description": "Proposals for food, beverage, and consumer packaged goods facilities (FSMA, SQF, HACCP)",
        "dir": VERTICALS_DIR / "food_beverage",
    },
    "general": {
        "label": "General / Other",
        "description": "General proposals that do not fall into a specific industry vertical",
        "dir": VERTICALS_DIR / "general",
    },
}
DEFAULT_VERTICAL = "general"

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-8")
# Small/fast model for the vertical classifier — it runs inside generation and
# scope drafting, so latency and cost matter more than depth there.
CLAUDE_CLASSIFIER_MODEL = os.getenv("CLAUDE_CLASSIFIER_MODEL", "claude-haiku-4-5")

# Database — Postgres in production via DATABASE_URL; SQLite fallback for dev.
_raw_db_url = os.getenv("DATABASE_URL", "").strip()
if _raw_db_url.startswith("postgres://"):
    # Heroku/DO-style URLs use the deprecated scheme SQLAlchemy rejects
    _raw_db_url = _raw_db_url.replace("postgres://", "postgresql://", 1)
DATABASE_URL = _raw_db_url or f"sqlite:///{BASE_DIR / 'data' / 'proposal_manager.db'}"

# Object storage (S3-compatible: AWS S3, DigitalOcean Spaces, MinIO…).
# When configured, uploads and generated files are mirrored to the bucket so
# they survive ephemeral disks; unset = local filesystem only.
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_REGION = os.getenv("S3_REGION", "")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "")  # for Spaces/MinIO
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", os.getenv("AWS_ACCESS_KEY_ID", ""))
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", os.getenv("AWS_SECRET_ACCESS_KEY", ""))

# Background jobs: set JOBS_INLINE=true to run jobs synchronously (tests/dev)
JOBS_INLINE = os.getenv("JOBS_INLINE", "false").lower() == "true"

# Deployment environment. Set APP_ENV=production to enable strict, fail-closed
# secret checks (see _verify_production_secrets in app.py).
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"

# Self-hosted mode: allow switching plans directly without Stripe (single-tenant
# install). In hosted/SaaS mode this stays false so a missing Stripe key can't
# turn the paywall off (any admin could otherwise self-upgrade for free).
SELF_HOSTED = os.getenv("SELF_HOSTED", "false").lower() == "true"

# Number of trusted reverse-proxy hops in front of the app (e.g. nginx = 1).
# Enables ProxyFix so request.remote_addr is the real client IP for rate
# limiting. Keep 0 unless the app is actually behind a proxy (otherwise clients
# could spoof X-Forwarded-For).
TRUST_PROXY_HOPS = int(os.getenv("TRUST_PROXY_HOPS", "0"))

# Platform-owner allowlist for the cross-tenant /platform-admin dashboard.
# Comma-separated emails. This is the bootstrap path (no owner exists in the DB
# at first deploy) and a break-glass backstop; the User.platform_owner column is
# the durable grant. Either one grants access.
PLATFORM_OWNER_EMAILS = {
    e.strip().lower() for e in os.getenv("PLATFORM_OWNER_EMAILS", "").split(",") if e.strip()
}

# Secrets known to be insecure defaults — refused in production.
INSECURE_SECRETS = {
    "",
    "dev-secret-change-me",
    "change-this-to-a-random-secret-key",
    "your-api-key-here",
}

# Flask
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"

# Upload limits
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))
ALLOWED_EXTENSIONS = {"pdf", "docx", "doc", "txt", "md"}

# Output
DEFAULT_OUTPUT_FORMAT = os.getenv("DEFAULT_OUTPUT_FORMAT", "docx")

# Branding — change these to rebrand the entire app
APP_NAME = os.getenv("APP_NAME", "Proposal Manager")
APP_SHORT_NAME = os.getenv("APP_SHORT_NAME", "PM")
APP_COMPANY = os.getenv("APP_COMPANY", "Proposal Manager")
APP_TAGLINE = os.getenv("APP_TAGLINE", "AI-Powered Proposal Platform")
APP_FOOTER = os.getenv("APP_FOOTER", "Proposal Manager — AI-Powered Proposal Platform")

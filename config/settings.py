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
CLAUDE_MODEL = "claude-opus-4-6"

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

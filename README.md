# Proposal Manager Agent

An AI-powered proposal generation tool for your intranet. Upload an RFP or RFQ document and get a complete proposal draft generated using your company templates, boilerplate, and past proposals as reference.

**Powered by Claude Opus 4.6**

---

## Quick Start

### 1. Prerequisites

- Python 3.11+ (or Docker)
- An [Anthropic API key](https://console.anthropic.com/)

### 2. Setup

```bash
# Clone and enter the repo
git clone <repo-url> && cd Proposal-Manager

# Create a virtual environment
python -m venv .venv && source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure your environment
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### 3. Run

```bash
python app.py
```

The application will be available at **http://localhost:5000**.

### Docker (recommended for intranet deployment)

```bash
cp .env.example .env
# Edit .env with your API key

docker compose up -d
```

---

## How It Works

1. **Upload** — A team member uploads an RFP/RFQ document (PDF, DOCX, or TXT) through the web interface.
2. **Parse** — The agent extracts all text and identifies the document type.
3. **Analyze** — Requirements, evaluation criteria, deliverables, and constraints are identified.
4. **Match** — Past proposals and reference documents are searched for relevant content.
5. **Generate** — A complete proposal is drafted using your templates, tailored to the specific RFP/RFQ.
6. **Review** — The generated proposal is presented with action items that require human input.
7. **Download** — The proposal can be downloaded as DOCX or Markdown.

---

## Project Structure

```
Proposal-Manager/
├── app.py                     # Flask web application
├── proposal_agent.py          # Core AI agent (Claude Opus 4.6)
├── proposal_export.py         # DOCX export utility
├── document_parser.py         # PDF/DOCX/text parser
├── config/
│   ├── settings.py            # Application configuration
│   └── workflow.md            # Agent workflow definition
├── templates/
│   └── proposal_boilerplate/  # Your proposal templates
│       ├── rfp_response_template.md
│       ├── rfq_response_template.md
│       └── company_boilerplate.md
├── reference_documents/
│   ├── sample_rfps/           # Sample RFP documents for reference
│   ├── sample_rfqs/           # Sample RFQ documents for reference
│   └── past_proposals/        # Past winning proposals
├── uploads/                   # Uploaded RFP/RFQ files (gitignored)
├── generated_proposals/       # Generated output (gitignored)
├── web_templates/             # HTML templates (Jinja2)
├── static/                    # CSS and JavaScript
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Customizing for Your Company

### Templates

Edit the files in `templates/proposal_boilerplate/`:

- **`company_boilerplate.md`** — Your company overview, past performance, certifications, insurance, and standard terms. **Update this first.**
- **`rfp_response_template.md`** — The structural template for RFP responses.
- **`rfq_response_template.md`** — The structural template for RFQ/quotation responses.

### Reference Documents

Add your own documents to `reference_documents/`:

- **`sample_rfps/`** — Drop in example RFP documents your company has received.
- **`sample_rfqs/`** — Drop in example RFQ documents.
- **`past_proposals/`** — Add your best past proposals. The agent uses these as a reference for tone, depth, and structure.

Supported formats: `.md`, `.txt`, `.pdf`, `.docx`

### Workflow

The agent's step-by-step process is defined in `config/workflow.md`. You can modify the workflow to match your company's proposal development process.

---

## Action Items

Every generated proposal includes `[ACTION REQUIRED]` placeholders for items that need human input:

- Specific pricing figures
- Named personnel and resumes
- Project-specific dates
- Customer-specific references
- Certifications to verify

The web interface highlights these in a summary panel so the proposal team knows exactly what to complete before submission.

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key | (required) |
| `FLASK_SECRET_KEY` | Flask session secret | `dev-secret-change-me` |
| `FLASK_HOST` | Bind address | `0.0.0.0` |
| `FLASK_PORT` | Port number | `5000` |
| `FLASK_DEBUG` | Debug mode | `false` |
| `MAX_UPLOAD_SIZE_MB` | Max upload size | `50` |
| `DEFAULT_OUTPUT_FORMAT` | Output format | `docx` |
# CloserAI

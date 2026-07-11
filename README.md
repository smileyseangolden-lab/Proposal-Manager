# Proposal Manager

An AI-powered proposal platform. Upload an RFP or RFQ document and get a complete proposal draft generated using your company templates, boilerplate, and past proposals as reference.

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

### DigitalOcean App Platform

The app works on DigitalOcean App Platform out of the box, using either the
Dockerfile or the Python buildpack (a `Procfile` is included). In both cases
gunicorn binds to the `$PORT` the platform injects.

1. Create an App from this repository (App Platform auto-detects the Dockerfile).
2. Set environment variables: `ANTHROPIC_API_KEY` (required) and
   `FLASK_SECRET_KEY` (set to a long random string). Optionally `APP_NAME`,
   `MAX_UPLOAD_SIZE_MB`, etc.
3. Deploy. The database schema is created automatically on first boot.

> **Persistence note:** the app stores its SQLite database in `data/` and
> uploaded files in `uploads/` / `generated_proposals/`. App Platform
> containers have ephemeral disks — attach a volume (or run on a Droplet with
> `docker compose`, which bind-mounts these directories) if you need data to
> survive redeploys.

---

## Production Deployment Notes (Hostinger VPS reference setup)

The production deployment at `srv1338704` runs this app alongside a separate
CloserAI stack on the same Docker host. These notes document the network path
end-to-end so the next developer doesn't hit the same pitfalls.

### Port map

| Layer | Binding | Purpose |
|---|---|---|
| Hostinger cloud firewall | TCP **5000** inbound allowed from `0.0.0.0/0` | Public entry point |
| Host nginx | `listen 5000;` (public, all interfaces) | Reverse proxy |
| Host nginx `proxy_pass` | `http://127.0.0.1:15010` | Forwards to Docker |
| Docker published port | `127.0.0.1:15010:5000` | Host → container |
| Container (gunicorn) | `0.0.0.0:5000` inside container | Flask app |

The app is reachable in a browser at `http://<vps-ip>:5000`. **Port 15010 is
loopback-only** and not directly reachable from outside.

### nginx config location

`/etc/nginx/sites-available/docker-apps` contains the `server` block for this
app. If you change the `127.0.0.1:15010` published port in
`docker-compose.yml`, update `proxy_pass` in that file too and run:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Make sure nginx is enabled at boot: `sudo systemctl enable nginx`.

### Hostinger cloud firewall

Hostinger runs a cloud firewall in front of the VPS that is **separate** from
`ufw`/`iptables` inside the VM. Any inbound port the app needs must be opened
in **hPanel → VPS → Security/Networking → Firewall** and synchronized to the
VPS. Currently TCP **5000** is allowed from `0.0.0.0/0`. `ufw` and `iptables`
on the VPS itself are intentionally open — the cloud firewall is the
authoritative perimeter.

### Why port 15010 instead of 15000

An earlier deployment published the container on `127.0.0.1:15000`. After an
unclean container shutdown, `dockerd`'s internal port-reservation map kept
holding 15000 even though no container was running, causing every subsequent
`docker compose up -d` to fail with:

```
failed to bind host port 127.0.0.1:15000/tcp: address already in use
```

Neither `docker container prune`, `docker network prune`, nor `docker rm -f`
released the reservation — only a full `sudo systemctl restart docker` would
have cleared it. To avoid bouncing the co-located CloserAI stack, the
published port was changed to **15010** as a sidestep. If you ever see this
error recur, the fastest non-disruptive fix is another port change; the
nuclear option is restarting the Docker daemon.

### Diagnostic quick reference

```bash
# Is the container up and on the expected port?
docker compose ps
sudo ss -tlnp | grep 15010       # should show dockerd listening

# Container-direct health check (bypasses nginx)
curl -I http://127.0.0.1:15010/login

# Through-nginx health check
curl -I http://127.0.0.1:5000/login

# Is nginx running and enabled?
sudo systemctl status nginx --no-pager

# Tail container logs
docker compose logs -f --tail=50
```

---

## How It Works — the Phase Flow

Every project moves through a visible chevron phase flow:

**Upload → Scope of Work → Draft Proposal → Internal Review → Send to Customer → Negotiate → Awarded / Not Awarded → Storage**

1. **Upload** — Drop an RFP/RFQ (PDF, DOCX, TXT) on the dashboard hero or a project page. Dropping a file on the dashboard creates the project for you.
2. **Scope of Work** — Optionally have the AI extract a Scope of Work checklist from the RFP. Accept or strike each item, add your own, then approve. The generated proposal covers exactly the approved scope.
3. **Draft Proposal** — The AI drafts against your Proposal Posture (templates, standards, rates, branding) with cost estimates from your configured rates.
4. **Internal Review** — Assign reviewers, collect approvals and revision requests, and batch-apply feedback to generate new versions. View changes inline with the Clean/Redlines toggle.
5. **Send to Customer** — Run the pre-flight check, then mark the proposal as submitted.
6. **Negotiate** — Log customer feedback (typed or AI-parsed from an email) and apply it to generate revised versions.
7. **Awarded / Not Awarded** — Record the outcome with reason category and competitor for win/loss reporting.
8. **Storage** — Archive the project; documents stay searchable in the Document Library.

### Key pages

- **Dashboard** — serif greeting, "Start a new proposal session" drop zone, and a "Pick up where you left off" list of every open item sorted by impact.
- **Proposals** — the full pipeline with quick-filter pills (Overdue, Unassigned, In Review, Awaiting Customer, Won, Lost…), sortable table or Kanban board view, and CSV export.
- **Proposal Posture** — your templates, company standards & terms, staff/equipment/travel rates, branding, and revision presets, organized as accordion categories.
- **Setup Wizard** — a 9-step checklist that walks new workspaces through posture setup.

---

## Part 2 Features — Sales Intelligence & Collaboration

On top of the core proposal workflow, Proposal Manager includes:

- **Deadlines & Calendar** — Assign due dates to projects; view them in a monthly calendar, with overdue and "due soon" widgets on the dashboard.
- **Win/Loss Analysis** — When closing a project as won or lost, capture the reason, category (price, scope, schedule, relationship, technical, compliance), and competitor name. Data feeds into reports for retrospective analysis.
- **Reports Dashboard** — `/reports` shows win rate, pipeline value, closed-project trend over the last 6 months, vertical performance breakdown, top competitors, and reason-category breakdowns for wins and losses.
- **Proposal Review Comments** — Team members can post inline review comments on any proposal, optionally anchored to a specific section. Comments can be resolved, unresolved, or deleted. Comment authors and project owners/assignees are notified automatically.
- **Global Search** — A top-bar search box at `/search` searches projects, proposal content (all versions), documents, notes, version labels, and review comments — all scoped to projects the user owns or is assigned to (admins see everything).

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


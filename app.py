"""Flask web application for the Proposal Manager Agent.

Provides an intranet-accessible interface where the proposal team can:
1. Select an industry vertical (or let the system auto-detect).
2. Upload RFP/RFQ documents.
3. Trigger AI-powered proposal generation.
4. Download the generated proposal as DOCX or Markdown.
"""

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import markdown as md
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

from config.settings import (
    ALLOWED_EXTENSIONS,
    DEFAULT_OUTPUT_FORMAT,
    FLASK_SECRET_KEY,
    GENERATED_DIR,
    MAX_UPLOAD_SIZE_MB,
    TEMPLATES_DIR,
    UPLOADS_DIR,
    VERTICALS,
)
from document_parser import parse_document
from proposal_agent import generate_proposal
from proposal_export import markdown_to_docx

app = Flask(__name__, template_folder="web_templates", static_folder="static")
app.secret_key = FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE_MB * 1024 * 1024

# Ensure directories exist
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
GENERATED_DIR.mkdir(parents=True, exist_ok=True)


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def index():
    """Landing page with upload form."""
    recent = _get_recent_proposals()
    return render_template(
        "index.html",
        recent_proposals=recent,
        verticals=VERTICALS,
    )


@app.route("/upload", methods=["POST"])
def upload():
    """Handle file upload and trigger proposal generation."""
    if "rfp_file" not in request.files:
        flash("No file selected.", "error")
        return redirect(url_for("index"))

    file = request.files["rfp_file"]
    if file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("index"))

    if not _allowed_file(file.filename):
        flash(
            f"Unsupported file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
            "error",
        )
        return redirect(url_for("index"))

    # Get selected vertical
    vertical = request.form.get("vertical", "auto")

    # Save uploaded file
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    safe_name = secure_filename(file.filename)
    upload_path = UPLOADS_DIR / f"{job_id}_{safe_name}"
    file.save(str(upload_path))

    try:
        # Parse the document
        rfp_text = parse_document(str(upload_path))
        if not rfp_text.strip():
            flash("Could not extract text from the uploaded document.", "error")
            return redirect(url_for("index"))

        # Generate the proposal with vertical selection
        result = generate_proposal(rfp_text, vertical=vertical)

        # Save Markdown output
        md_filename = f"proposal_{job_id}.md"
        md_path = GENERATED_DIR / md_filename
        md_path.write_text(result["proposal_markdown"], encoding="utf-8")

        # Save DOCX output
        docx_filename = f"proposal_{job_id}.docx"
        docx_path = GENERATED_DIR / docx_filename
        markdown_to_docx(result["proposal_markdown"], str(docx_path))

        # Save metadata
        meta = {
            "job_id": job_id,
            "source_file": safe_name,
            "document_type": result["document_type"],
            "vertical": result["vertical"],
            "vertical_label": result["vertical_label"],
            "confidence_score": result["confidence_score"],
            "action_items_count": len(result["action_items"]),
            "generated_at": result["generated_at"],
            "md_file": md_filename,
            "docx_file": docx_filename,
        }
        meta_path = GENERATED_DIR / f"proposal_{job_id}_meta.json"
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        return redirect(url_for("view_proposal", job_id=job_id))

    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))
    except Exception as e:
        flash(f"An error occurred during proposal generation: {e}", "error")
        return redirect(url_for("index"))


@app.route("/proposal/<job_id>")
def view_proposal(job_id: str):
    """View a generated proposal."""
    meta_path = GENERATED_DIR / f"proposal_{job_id}_meta.json"
    if not meta_path.exists():
        flash("Proposal not found.", "error")
        return redirect(url_for("index"))

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    md_path = GENERATED_DIR / meta["md_file"]
    proposal_md = md_path.read_text(encoding="utf-8")
    proposal_html = md.markdown(proposal_md, extensions=["tables", "fenced_code"])

    # Extract action items from the markdown
    action_items = re.findall(r"\[ACTION REQUIRED:\s*(.+?)\]", proposal_md)

    return render_template(
        "proposal.html",
        meta=meta,
        proposal_html=proposal_html,
        action_items=action_items,
        job_id=job_id,
    )


@app.route("/download/<job_id>/<fmt>")
def download(job_id: str, fmt: str):
    """Download a generated proposal as DOCX or Markdown."""
    meta_path = GENERATED_DIR / f"proposal_{job_id}_meta.json"
    if not meta_path.exists():
        flash("Proposal not found.", "error")
        return redirect(url_for("index"))

    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    if fmt == "docx":
        file_path = GENERATED_DIR / meta["docx_file"]
        mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif fmt == "md":
        file_path = GENERATED_DIR / meta["md_file"]
        mimetype = "text/markdown"
    else:
        flash("Invalid format.", "error")
        return redirect(url_for("view_proposal", job_id=job_id))

    return send_file(
        str(file_path),
        mimetype=mimetype,
        as_attachment=True,
        download_name=file_path.name,
    )


def _get_recent_proposals(limit: int = 10) -> list[dict]:
    """Get metadata for recently generated proposals."""
    metas = []
    for meta_file in sorted(GENERATED_DIR.glob("*_meta.json"), reverse=True):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            metas.append(meta)
        except Exception:
            continue
        if len(metas) >= limit:
            break
    return metas


if __name__ == "__main__":
    from config.settings import FLASK_DEBUG, FLASK_HOST, FLASK_PORT

    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)

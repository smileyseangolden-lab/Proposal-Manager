"""Per-document triage analysis using Claude Haiku 4.5.

For each ``ProjectDocument`` belonging to a bid package, this module:

1. Loads the document text via ``parse_document``.
2. Detects scanned PDFs / OCR-needed cases and flags them without calling Claude.
3. Calls Claude Haiku with a strict JSON-output prompt that returns
   ``{trade, document_type, addendum_label, synopsis, key_entities, confidence}``.
4. Validates the response against a small schema and persists a
   ``DocumentAnalysis`` row.

Costs roughly $0.001 per document at current Haiku 4.5 pricing for the
~3-5k-character inputs typical of bid-package items.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import anthropic

from config.settings import ANTHROPIC_API_KEY
from context_retrieval import retrieve_context
from document_parser import parse_document
from models import DocumentAnalysis, EtgKnowledgeAsset, ProjectDocument, db


# Cheap classifier for fast, low-cost triage. Override per-user via
# ``User.llm_model`` if a power user wants Opus everywhere.
DEFAULT_TRIAGE_MODEL = "claude-haiku-4-5-20251001"

# Hard caps so a single weird document can't blow the budget.
MAX_INPUT_CHARS = 16000  # text fed into the prompt
MIN_TEXT_FOR_LLM = 200  # below this, we treat as "needs_review"
MAX_OUTPUT_TOKENS = 800

# Default trade and document-type values we accept. The taxonomy can be
# overridden by editing the system-seeded EtgKnowledgeAsset rows in Settings.
_FALLBACK_TRADES = {
    "general", "electrical", "instrumentation", "automation_controls",
    "process_mechanical", "piping_pid", "structural_civil", "hvac",
    "building_automation", "networking_cybersecurity",
    "validation_qualification", "commissioning", "safety", "project_management",
}
_FALLBACK_DOC_TYPES = {
    "rfp_narrative", "specification", "datasheet", "drawing",
    "sequence_of_operations", "io_list", "bill_of_materials", "schedule",
    "addendum", "contract_legal", "submittal", "vendor_proposal",
    "rfp_qa_log", "checklist", "report", "uncategorized",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_document(
    document_id: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    vertical: str | None = None,
) -> DocumentAnalysis:
    """Run the full triage pass on a single document. Idempotent.

    Returns the upserted ``DocumentAnalysis`` row regardless of outcome.
    """
    document = db.session.get(ProjectDocument, document_id)
    if document is None:
        raise ValueError(f"ProjectDocument {document_id} not found")

    analysis = _get_or_create_analysis(document)
    analysis.status = "analyzing"
    analysis.error_message = ""
    db.session.commit()

    try:
        text = _safe_parse(document.file_path)
    except Exception as exc:
        analysis.status = "failed"
        analysis.error_message = f"Failed to extract text: {exc}"
        analysis.analyzed_at = datetime.now(timezone.utc)
        db.session.commit()
        return analysis

    analysis.text_length = len(text)

    # Empty / scanned PDFs: flag and stop. The AE can re-run after OCR.
    if len(text.strip()) < MIN_TEXT_FOR_LLM:
        analysis.needs_ocr = document.file_path.lower().endswith(".pdf")
        analysis.status = "needs_review"
        analysis.synopsis = (
            "Insufficient text was extractable from this document. "
            "It may be a scanned PDF, an empty file, or in an unsupported format. "
            "Please review manually."
        )
        analysis.confidence = 0.0
        analysis.analyzed_at = datetime.now(timezone.utc)
        db.session.commit()
        return analysis

    chosen_model = model or DEFAULT_TRIAGE_MODEL

    prompt_text = _build_user_prompt(document, text, vertical=vertical)
    system_prompt = _build_system_prompt(vertical=vertical, user_id=document.project.user_id)

    try:
        result = _call_claude(
            api_key=api_key,
            model=chosen_model,
            system_prompt=system_prompt,
            user_prompt=prompt_text,
        )
    except anthropic.APIError as exc:
        analysis.status = "failed"
        analysis.error_message = f"AI API error: {exc}"
        analysis.analyzed_at = datetime.now(timezone.utc)
        analysis.llm_model = chosen_model
        db.session.commit()
        return analysis
    except Exception as exc:
        analysis.status = "failed"
        analysis.error_message = f"AI call failed: {exc}"
        analysis.analyzed_at = datetime.now(timezone.utc)
        analysis.llm_model = chosen_model
        db.session.commit()
        return analysis

    parsed = _parse_response(result)
    if parsed is None:
        analysis.status = "failed"
        analysis.error_message = "Could not parse JSON from AI response"
        analysis.analyzed_at = datetime.now(timezone.utc)
        analysis.llm_model = chosen_model
        db.session.commit()
        return analysis

    analysis.trade = _normalize_trade(parsed.get("trade", ""))
    analysis.document_type_detected = _normalize_doc_type(parsed.get("document_type", ""))
    analysis.addendum_label = (parsed.get("addendum_label") or "").strip()[:80]
    analysis.synopsis = (parsed.get("synopsis") or "").strip()
    analysis.key_entities = json.dumps(parsed.get("key_entities") or {}, ensure_ascii=False)
    try:
        analysis.confidence = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        analysis.confidence = 0.5

    analysis.status = "analyzed"
    analysis.llm_model = chosen_model
    analysis.analyzed_at = datetime.now(timezone.utc)
    db.session.commit()
    return analysis


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _get_or_create_analysis(document: ProjectDocument) -> DocumentAnalysis:
    analysis = DocumentAnalysis.query.filter_by(document_id=document.id).first()
    if analysis is not None:
        return analysis
    analysis = DocumentAnalysis(
        document_id=document.id,
        project_id=document.project_id,
        bid_package_id=document.bid_package_id,
    )
    db.session.add(analysis)
    db.session.flush()
    return analysis


def _safe_parse(path: str) -> str:
    try:
        return parse_document(path) or ""
    except ValueError:
        # Unsupported extension (e.g., .dwg). Treat as empty so the
        # needs_review branch fires.
        return ""


def _build_system_prompt(*, vertical: str | None, user_id: str | None) -> str:
    taxonomy = retrieve_context(
        ["taxonomy"],
        user_id=user_id,
        max_chars=2000,
    )
    company = retrieve_context(
        ["company_profile", "capabilities"],
        user_id=user_id,
        vertical=vertical,
        max_chars=3000,
    )

    body = """You are the ETG Pre-Proposal Triage Analyst. Your job is to read a single document from a customer bid package and produce a short structured classification used by Application Engineers to navigate the package.

Be factual and concise. Do NOT promote, market, or speculate. If you cannot tell, choose the closest match and lower your confidence score.

Return a single JSON object only. No prose before or after. No markdown fences. Schema:

{
  "trade": "<one of the trade keys>",
  "document_type": "<one of the document_type keys>",
  "addendum_label": "<e.g. 'Base', 'Addendum 1', 'Rev B', or empty string>",
  "synopsis": "<25-35 word factual summary in plain English>",
  "key_entities": {
    "systems": [<short string identifiers like 'H1', 'S1', 'BMS-01'>],
    "io_count": <integer if explicitly stated, else null>,
    "instrument_count": <integer if explicitly stated, else null>,
    "valve_count": <integer if explicitly stated, else null>,
    "notes": "<brief free-text about anything else worth flagging>"
  },
  "confidence": <float 0.0..1.0>
}
"""

    if taxonomy:
        body += "\n## Taxonomy (use only these values)\n" + taxonomy
    if company:
        body += "\n\n## ETG Context (background — do not echo)\n" + company

    return body


def _build_user_prompt(document: ProjectDocument, text: str, *, vertical: str | None) -> str:
    truncated = text[:MAX_INPUT_CHARS]
    note = "" if len(text) <= MAX_INPUT_CHARS else "\n\n[Document text was truncated for length.]"
    vertical_line = f"Vertical (hint): {vertical}\n" if vertical else ""
    return f"""Document filename: {document.original_filename}
Relative path in package: {document.relative_path or document.original_filename}
Detected size: {document.file_size} bytes
{vertical_line}
--- BEGIN DOCUMENT TEXT ---
{truncated}{note}
--- END DOCUMENT TEXT ---

Return the JSON object now."""


def _call_claude(
    *,
    api_key: str | None,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> str:
    key = api_key or ANTHROPIC_API_KEY
    if not key:
        raise RuntimeError("No Anthropic API key configured")

    client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=2)
    msg = client.messages.create(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    parts = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_response(raw: str) -> dict | None:
    if not raw:
        return None
    candidate = raw.strip()
    if candidate.startswith("```"):
        # Strip code fences if the model insisted on them.
        candidate = candidate.strip("`")
        candidate = re.sub(r"^json\n", "", candidate, flags=re.IGNORECASE)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK_RE.search(candidate)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _allowed_set(section: str, key: str, fallback: set[str]) -> set[str]:
    """Pull the live taxonomy from EtgKnowledgeAsset; fall back to the seed list."""
    asset = EtgKnowledgeAsset.query.filter_by(
        user_id=None, section="taxonomy", title=key
    ).first()
    if asset is None or not asset.content_md:
        return set(fallback)
    items = set()
    for line in asset.content_md.splitlines():
        s = line.strip().lstrip("-* ").strip()
        if s:
            items.add(s)
    return items or set(fallback)


def _normalize_trade(value: str) -> str:
    raw = (value or "").strip().lower().replace(" ", "_")
    if not raw:
        return "general"
    allowed = _allowed_set("taxonomy", "Trades", _FALLBACK_TRADES)
    if raw in allowed:
        return raw
    # Soft match: take the first allowed value that appears as a substring.
    for cand in allowed:
        if cand in raw or raw in cand:
            return cand
    return "general"


def _normalize_doc_type(value: str) -> str:
    raw = (value or "").strip().lower().replace(" ", "_")
    if not raw:
        return "uncategorized"
    allowed = _allowed_set("taxonomy", "Document Types", _FALLBACK_DOC_TYPES)
    if raw in allowed:
        return raw
    for cand in allowed:
        if cand in raw or raw in cand:
            return cand
    return "uncategorized"

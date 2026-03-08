"""Proposal generation agent powered by Claude Opus 4.6.

This module implements the core agent that reads an uploaded RFP/RFQ,
follows the vertical-specific workflow, and generates a complete proposal
draft using the appropriate templates and reference material.

Supports:
- Vertical-specific workflows and templates
- User-uploaded templates (overriding defaults)
- Rate/price sheet context for pricing guidance
- Interactive Q&A (returns questions when clarification is needed)
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from config.settings import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    REFERENCE_DIR,
    TEMPLATES_DIR,
    VERTICALS,
    WORKFLOW_PATH,
)
from document_parser import (
    load_reference_documents,
    load_templates,
    load_vertical_resources,
)


def _build_system_prompt(vertical_key: str, vertical_resources: dict,
                         global_templates: dict[str, str],
                         global_references: dict[str, list[str]],
                         rate_sheet_text: str = "",
                         user_template_text: str = "",
                         company_name: str = "") -> str:
    """Assemble the system prompt with all context."""

    vertical_label = VERTICALS.get(vertical_key, {}).get("label", "General")
    workflow = vertical_resources.get("workflow", "")
    vertical_templates = vertical_resources.get("templates", {})
    vertical_ref_proposals = vertical_resources.get("reference_proposals", [])

    company_line = f"You are generating this proposal on behalf of **{company_name}**.\n\n" if company_name else ""

    # Compile vertical-specific template text
    template_block = ""

    # User-uploaded templates take highest priority
    if user_template_text:
        template_block += f"\n### User-Uploaded Template (HIGHEST PRIORITY — use this structure)\n```\n{user_template_text}\n```\n"

    for name, content in vertical_templates.items():
        template_block += f"\n### Vertical Template: {name}\n```\n{content}\n```\n"

    for name, content in global_templates.items():
        template_block += f"\n### Global Template: {name}\n```\n{content}\n```\n"

    # Reference material
    MAX_REF_CHARS = 12000
    ref_block = ""

    if vertical_ref_proposals:
        ref_block += f"\n### {vertical_label} Reference Proposals\n"
        for i, doc_text in enumerate(vertical_ref_proposals, 1):
            truncated = doc_text[:MAX_REF_CHARS]
            if len(doc_text) > MAX_REF_CHARS:
                truncated += "\n[...truncated for length...]"
            ref_block += f"\n#### Reference Proposal {i}\n```\n{truncated}\n```\n"

    for category, docs in global_references.items():
        if docs:
            ref_block += f"\n### {category.replace('_', ' ').title()}\n"
            for i, doc_text in enumerate(docs, 1):
                truncated = doc_text[:MAX_REF_CHARS]
                if len(doc_text) > MAX_REF_CHARS:
                    truncated += "\n[...truncated for length...]"
                ref_block += f"\n#### Document {i}\n```\n{truncated}\n```\n"

    # Rate sheet block
    rate_block = ""
    if rate_sheet_text:
        rate_block = f"""
## Rate & Price Sheet Data

The following rate/price sheet data has been provided by the user. Use these
rates and prices as reference when structuring the pricing section. Do NOT
fabricate rates — only use the rates provided below. If a rate is not available
for a specific role or product, use an `[ACTION REQUIRED]` placeholder.

```
{rate_sheet_text[:8000]}
```
"""

    return f"""You are the Proposal Manager Agent — an expert proposal writer that generates
professional proposals in response to customer RFP (Request for Proposal) and
RFQ (Request for Quotation) documents.

{company_line}## Industry Vertical

You are generating a **{vertical_label}** proposal. Use the vertical-specific
workflow, templates, and terminology appropriate for this industry. The vertical-
specific templates take precedence over global templates when both are available.
User-uploaded templates take highest precedence.

## Your Workflow

Follow this workflow precisely when generating proposals:

{workflow}

## Proposal Templates & Boilerplate

{template_block}

## Reference Material

{ref_block}

{rate_block}

## Interactive Clarification

If you encounter information in the RFP/RFQ that is ambiguous, contradictory,
or critical to the proposal but missing, you may ask the user for clarification.
To do this, include a section at the VERY END of your output titled
"## CLARIFICATION QUESTIONS" with numbered questions. Each question should be
on its own line prefixed with "Q:" and include context about why you need this
information. Only ask questions that are truly critical to producing an accurate
proposal — do not ask about information you can reasonably infer or mark as
ACTION REQUIRED.

## Output Rules

1. Output the complete proposal in well-structured Markdown.
2. Use the highest-priority template available as your primary structure.
3. Fill in every section with tailored content that directly addresses the
   requirements in the uploaded document.
4. For any information that requires human input (pricing figures, specific
   personnel names, customer-specific dates, etc.), insert a clearly visible
   placeholder: `[ACTION REQUIRED: description of what's needed]`.
   - When possible, tag the placeholder with the responsible RACI role:
     `[ACTION REQUIRED: BDM — description]` or
     `[ACTION REQUIRED: AE — description]` etc.
5. Generate a compliance matrix mapping every requirement to your response.
6. At the end (before any CLARIFICATION QUESTIONS), provide:
   - A consolidated list of all ACTION REQUIRED items, grouped by responsible role.
   - A confidence score (0-100%) for how completely the RFP/RFQ was addressed.
7. Write in professional, persuasive business English.
8. Do NOT fabricate specific pricing numbers, personnel names, or certifications
   — always use ACTION REQUIRED placeholders for these.
9. Do NOT fabricate instrument counts, quantities, or material specifications.
10. Do NOT invent margin assumptions or name subcontractors without input.
11. If rate/price sheet data was provided, reference those rates where applicable
    in the pricing section structure.
12. Today's date is {datetime.now(timezone.utc).strftime("%B %d, %Y")}.
"""


def generate_proposal(rfp_text: str, vertical: str = "auto",
                      rate_sheet_data: dict = None,
                      user_templates: dict = None,
                      company_name: str = "",
                      user_api_key: str = None,
                      user_model: str = None,
                      answered_questions: list = None,
                      progress_callback=None) -> dict:
    """Generate a proposal from the given RFP/RFQ text.

    Args:
        rfp_text: The extracted text content of the uploaded RFP/RFQ document.
        vertical: The industry vertical key or 'auto' for auto-detection.
        rate_sheet_data: Parsed rate sheet data (dict with 'raw_text' key).
        user_templates: User-uploaded template text by type.
        company_name: User's company name for branding.
        user_api_key: User's own API key (overrides global).
        user_model: User's selected model (overrides global).
        answered_questions: Previously answered Q&A pairs.
        progress_callback: Optional callable(phase, message).

    Returns:
        dict with proposal_markdown, action_items, confidence_score,
        document_type, vertical, vertical_label, generated_at, and
        optionally 'questions' if clarification is needed.
    """
    api_key = user_api_key or ANTHROPIC_API_KEY
    model = user_model or CLAUDE_MODEL

    if not api_key:
        raise RuntimeError(
            "No API key configured. Go to Settings to add your Anthropic API key."
        )

    def _report(phase: str, message: str):
        if progress_callback:
            progress_callback(phase, message)

    # Resolve vertical
    if vertical == "auto":
        from document_parser import detect_vertical
        _report("detection", "Analyzing document to detect industry vertical...")
        vertical = detect_vertical(rfp_text)

    vertical_label = VERTICALS.get(vertical, {}).get("label", "General")
    _report("init", f"Loading {vertical_label} templates and reference documents...")

    # Load resources
    vertical_resources = load_vertical_resources(vertical)
    global_templates = load_templates(TEMPLATES_DIR)
    global_references = load_reference_documents(REFERENCE_DIR)

    # Build rate sheet context
    rate_sheet_text = ""
    if rate_sheet_data:
        for sheet_type, data in rate_sheet_data.items():
            if isinstance(data, dict) and "raw_text" in data:
                rate_sheet_text += f"\n### {sheet_type.replace('_', ' ').title()}\n{data['raw_text']}\n"

    # Build user template context
    user_template_text = ""
    if user_templates:
        for ttype, text in user_templates.items():
            user_template_text += f"\n### {ttype.replace('_', ' ').title()}\n{text}\n"

    system_prompt = _build_system_prompt(
        vertical, vertical_resources, global_templates, global_references,
        rate_sheet_text=rate_sheet_text,
        user_template_text=user_template_text,
        company_name=company_name,
    )

    _report("analysis", f"Generating {vertical_label} proposal...")

    # Build messages
    user_message = (
        f"Please generate a complete {vertical_label} proposal in "
        "response to the following RFP/RFQ document. Follow the "
        "workflow exactly.\n\n"
        "---BEGIN DOCUMENT---\n"
        f"{rfp_text}\n"
        "---END DOCUMENT---"
    )

    # Include answered questions as additional context
    if answered_questions:
        qa_block = "\n\n---PREVIOUSLY ANSWERED QUESTIONS---\n"
        for qa in answered_questions:
            qa_block += f"\nQ: {qa['question']}\nA: {qa['answer']}\n"
        user_message += qa_block

    client = anthropic.Anthropic(api_key=api_key)

    proposal_text = ""
    with client.messages.stream(
        model=model,
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            proposal_text += text

    _report("post_processing", "Extracting action items and finalizing...")

    # Extract action items
    action_items = re.findall(r"\[ACTION REQUIRED:\s*(.+?)\]", proposal_text)

    # Extract clarification questions
    questions = _extract_questions(proposal_text)

    # Remove the questions section from the final proposal text
    proposal_text = re.sub(
        r"\n## CLARIFICATION QUESTIONS.*$", "", proposal_text, flags=re.DOTALL
    ).strip()

    confidence_score = _extract_confidence_score(proposal_text)
    doc_type = _detect_document_type(rfp_text)

    _report("complete", "Proposal generation complete.")

    result = {
        "proposal_markdown": proposal_text,
        "action_items": action_items,
        "confidence_score": confidence_score,
        "document_type": doc_type,
        "vertical": vertical,
        "vertical_label": vertical_label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    if questions:
        result["questions"] = questions

    return result


def _extract_questions(text: str) -> list[dict]:
    """Extract clarification questions from the proposal output."""
    match = re.search(r"## CLARIFICATION QUESTIONS\s*\n(.*)", text, re.DOTALL)
    if not match:
        return []

    questions_text = match.group(1)
    questions = []
    for line in questions_text.strip().split("\n"):
        line = line.strip()
        if line.startswith("Q:") or line.startswith("Q "):
            q_text = line[2:].strip().lstrip(":").strip()
            if q_text:
                questions.append({"question": q_text, "context": ""})
        elif re.match(r"^\d+[\.\)]\s*", line):
            q_text = re.sub(r"^\d+[\.\)]\s*", "", line).strip()
            if q_text:
                questions.append({"question": q_text, "context": ""})

    return questions


def _extract_confidence_score(text: str) -> int:
    match = re.search(r"[Cc]onfidence\s*[Ss]core[:\s]*(\d{1,3})%?", text)
    if match:
        return min(int(match.group(1)), 100)
    return 0


def _detect_document_type(text: str) -> str:
    text_lower = text.lower()
    rfp_signals = text_lower.count("request for proposal") + text_lower.count("rfp")
    rfq_signals = text_lower.count("request for quotation") + text_lower.count(
        "request for quote"
    ) + text_lower.count("rfq")
    return "RFQ" if rfq_signals > rfp_signals else "RFP"

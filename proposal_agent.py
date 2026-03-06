"""Proposal generation agent powered by Claude Opus 4.6.

This module implements the core agent that reads an uploaded RFP/RFQ,
follows the vertical-specific workflow, and generates a complete proposal
draft using the appropriate templates and reference material.
"""

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
                         global_references: dict[str, list[str]]) -> str:
    """Assemble the system prompt with vertical-specific workflow, templates, and references."""

    vertical_label = VERTICALS.get(vertical_key, {}).get("label", "General")
    workflow = vertical_resources.get("workflow", "")
    vertical_templates = vertical_resources.get("templates", {})
    vertical_ref_proposals = vertical_resources.get("reference_proposals", [])

    # Compile vertical-specific template text
    template_block = ""
    for name, content in vertical_templates.items():
        template_block += f"\n### Vertical Template: {name}\n```\n{content}\n```\n"

    # Also include global templates as supplementary
    for name, content in global_templates.items():
        template_block += f"\n### Global Template: {name}\n```\n{content}\n```\n"

    # Compile reference material (truncate very long documents to stay in context)
    MAX_REF_CHARS = 12000
    ref_block = ""

    # Vertical-specific reference proposals first
    if vertical_ref_proposals:
        ref_block += f"\n### {vertical_label} Reference Proposals\n"
        for i, doc_text in enumerate(vertical_ref_proposals, 1):
            truncated = doc_text[:MAX_REF_CHARS]
            if len(doc_text) > MAX_REF_CHARS:
                truncated += "\n[...truncated for length...]"
            ref_block += f"\n#### Reference Proposal {i}\n```\n{truncated}\n```\n"

    # Global reference documents
    for category, docs in global_references.items():
        if docs:
            ref_block += f"\n### {category.replace('_', ' ').title()}\n"
            for i, doc_text in enumerate(docs, 1):
                truncated = doc_text[:MAX_REF_CHARS]
                if len(doc_text) > MAX_REF_CHARS:
                    truncated += "\n[...truncated for length...]"
                ref_block += f"\n#### Document {i}\n```\n{truncated}\n```\n"

    return f"""You are the Proposal Manager Agent — an expert proposal writer that generates
professional proposals in response to customer RFP (Request for Proposal) and
RFQ (Request for Quotation) documents.

## Industry Vertical

You are generating a **{vertical_label}** proposal. Use the vertical-specific
workflow, templates, and terminology appropriate for this industry. The vertical-
specific templates take precedence over global templates when both are available.

## Your Workflow

Follow this workflow precisely when generating proposals:

{workflow}

## Proposal Templates & Boilerplate

Use these templates as the structural foundation for your output. The vertical-
specific templates should be used as the primary structure. Global templates
provide supplementary boilerplate content.

{template_block}

## Reference Material

Use the following reference proposals and documents for tone, structure, and
level of detail. Adapt — do not copy verbatim.

{ref_block}

## Output Rules

1. Output the complete proposal in well-structured Markdown.
2. Use the vertical-specific template as your primary structure.
3. Fill in every section with tailored content that directly addresses the
   requirements in the uploaded document.
4. For any information that requires human input (pricing figures, specific
   personnel names, customer-specific dates, etc.), insert a clearly visible
   placeholder: `[ACTION REQUIRED: description of what's needed]`.
   - When possible, tag the placeholder with the responsible RACI role:
     `[ACTION REQUIRED: BDM — description]` or
     `[ACTION REQUIRED: AE — description]` etc.
5. Generate a compliance matrix mapping every requirement to your response.
6. At the end, provide:
   - A consolidated list of all ACTION REQUIRED items, grouped by responsible role.
   - A confidence score (0-100%) for how completely the RFP/RFQ was addressed.
7. Write in professional, persuasive business English.
8. Do NOT fabricate specific pricing numbers, personnel names, or certifications
   — always use ACTION REQUIRED placeholders for these.
9. Do NOT fabricate instrument counts, quantities, or material specifications.
10. Do NOT invent margin assumptions or name subcontractors without input.
11. Today's date is {datetime.now(timezone.utc).strftime("%B %d, %Y")}.
"""


def generate_proposal(rfp_text: str, vertical: str = "auto",
                      progress_callback=None) -> dict:
    """Generate a proposal from the given RFP/RFQ text.

    Args:
        rfp_text: The extracted text content of the uploaded RFP/RFQ document.
        vertical: The industry vertical key ('data_center', 'life_science',
            'food_beverage', 'general') or 'auto' for auto-detection.
        progress_callback: Optional callable(phase: str, message: str) for
            streaming progress updates to the UI.

    Returns:
        dict with keys:
            - proposal_markdown: The full generated proposal as Markdown text.
            - action_items: List of action-required items extracted from the proposal.
            - confidence_score: Integer 0-100.
            - document_type: 'RFP' or 'RFQ'.
            - vertical: The vertical key used for generation.
            - vertical_label: The human-readable vertical label.
            - generated_at: ISO timestamp.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env file."
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

    # Load vertical-specific resources
    vertical_resources = load_vertical_resources(vertical)

    # Load global resources as supplementary context
    global_templates = load_templates(TEMPLATES_DIR)
    global_references = load_reference_documents(REFERENCE_DIR)

    system_prompt = _build_system_prompt(
        vertical, vertical_resources, global_templates, global_references
    )

    _report("analysis", f"Generating {vertical_label} proposal...")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Stream the response for long-running generation
    proposal_text = ""
    with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Please generate a complete {vertical_label} proposal in "
                    "response to the following RFP/RFQ document. Follow the "
                    "workflow exactly.\n\n"
                    "---BEGIN DOCUMENT---\n"
                    f"{rfp_text}\n"
                    "---END DOCUMENT---"
                ),
            }
        ],
    ) as stream:
        for text in stream.text_stream:
            proposal_text += text

    _report("post_processing", "Extracting action items and finalizing...")

    # Extract action items from the generated proposal
    action_items = re.findall(
        r"\[ACTION REQUIRED:\s*(.+?)\]", proposal_text
    )

    # Try to extract the confidence score from the proposal text
    confidence_score = _extract_confidence_score(proposal_text)

    # Detect document type
    doc_type = _detect_document_type(rfp_text)

    _report("complete", "Proposal generation complete.")

    return {
        "proposal_markdown": proposal_text,
        "action_items": action_items,
        "confidence_score": confidence_score,
        "document_type": doc_type,
        "vertical": vertical,
        "vertical_label": vertical_label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _extract_confidence_score(text: str) -> int:
    """Extract confidence score from the proposal text."""
    match = re.search(r"[Cc]onfidence\s*[Ss]core[:\s]*(\d{1,3})%?", text)
    if match:
        return min(int(match.group(1)), 100)
    return 0


def _detect_document_type(text: str) -> str:
    """Detect whether the uploaded document is an RFP or RFQ."""
    text_lower = text.lower()
    rfp_signals = text_lower.count("request for proposal") + text_lower.count("rfp")
    rfq_signals = text_lower.count("request for quotation") + text_lower.count(
        "request for quote"
    ) + text_lower.count("rfq")
    return "RFQ" if rfq_signals > rfp_signals else "RFP"

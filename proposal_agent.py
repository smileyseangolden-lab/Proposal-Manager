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


# Generous defaults so slow networks / large proposals don't look like outages.
_CLIENT_TIMEOUT_SECONDS = 120.0
_CLIENT_MAX_RETRIES = 3


def _make_client(api_key: str) -> anthropic.Anthropic:
    """Create an Anthropic client with connection-friendly defaults."""
    return anthropic.Anthropic(
        api_key=api_key,
        timeout=_CLIENT_TIMEOUT_SECONDS,
        max_retries=_CLIENT_MAX_RETRIES,
    )


def friendly_api_error(exc: BaseException) -> str:
    """Map Anthropic SDK exceptions to actionable user-facing messages."""
    if isinstance(exc, anthropic.APIConnectionError):
        return (
            "Could not reach the Anthropic API (connection error). "
            "Check that the server has outbound network access to "
            "api.anthropic.com and that any firewall, proxy, or DNS "
            "settings allow HTTPS to that host."
        )
    if isinstance(exc, anthropic.AuthenticationError):
        return (
            "Anthropic rejected the API key. Re-enter your key in "
            "Settings > Profile & AI."
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        return (
            "Your Anthropic account doesn't have access to the requested "
            "model. Pick a different model in Settings > Profile & AI."
        )
    if isinstance(exc, anthropic.NotFoundError):
        return (
            "The selected AI model is not available. Choose a different "
            "model in Settings > Profile & AI."
        )
    if isinstance(exc, anthropic.RateLimitError):
        return (
            "Anthropic API rate limit hit. Wait a minute and try again, "
            "or lower the request volume."
        )
    if isinstance(exc, anthropic.APIStatusError):
        return (
            f"Anthropic API error (status {exc.status_code}): "
            f"{getattr(exc, 'message', str(exc))}"
        )
    return str(exc) or exc.__class__.__name__


def _build_system_prompt(vertical_key: str, vertical_resources: dict,
                         global_templates: dict[str, str],
                         global_references: dict[str, list[str]],
                         rate_sheet_text: str = "",
                         user_template_text: str = "",
                         company_name: str = "",
                         cost_options: dict = None,
                         staff_roles_data: list = None,
                         equipment_data: list = None,
                         travel_data: list = None,
                         past_corrections: list = None,
                         company_standards: list = None,
                         approved_sow: str = "") -> str:
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

    # Build cost estimation instructions
    cost_estimation_block = ""
    if cost_options:
        sections = []

        if cost_options.get("include_staff_types") and staff_roles_data:
            roles_table = "| Role | Category | Hourly Rate | OT Rate | Description |\n|------|----------|-------------|---------|-------------|\n"
            for r in staff_roles_data:
                ot = f"${r['overtime_rate']:.2f}" if r['overtime_rate'] else "N/A"
                desc = r['description'] or ''
                roles_table += f"| {r['role_name']} | {r['category']} | ${r['hourly_rate']:.2f} | {ot} | {desc} |\n"
            sections.append(f"""
### Staff Type Estimation (REQUESTED)
The user has requested that you estimate what types of staff are needed for this project.
Using the RFP/RFQ requirements, identify which of the following staff roles would be needed:

{roles_table}

Include a **Staffing Plan** section in the proposal with a table showing:
- Recommended staff roles from the list above
- Quantity of each role needed
- Justification for why each role is needed based on the RFP scope
""")

        if cost_options.get("include_staff_hours") and staff_roles_data:
            sections.append("""
### Staff Hours Estimation (REQUESTED)
The user has requested a detailed staff hours breakdown. For each recommended staff role,
estimate the number of hours needed based on the project scope in the RFP/RFQ.

Include a **Labor Cost Estimate** table with:
| Role | Qty | Hours per Person | Total Hours | Hourly Rate | Total Cost |

Calculate subtotals per role and a grand total for labor.
Use the hourly rates from the staff roles table above — do NOT fabricate rates.
If overtime is expected, include a separate OT line using the OT rate.
""")

        if cost_options.get("include_equipment_bom") and equipment_data:
            equip_table = "| Item | Category | Part # | Manufacturer | Unit Cost | Unit |\n|------|----------|--------|-------------|-----------|------|\n"
            for e in equipment_data:
                equip_table += f"| {e['item_name']} | {e['category']} | {e['part_number'] or 'N/A'} | {e['manufacturer'] or 'N/A'} | ${e['unit_cost']:.2f} | {e['unit']} |\n"
            sections.append(f"""
### Equipment / Bill of Materials Estimation (REQUESTED)
The user has requested an equipment and materials estimate. Using the RFP/RFQ scope,
identify which items from the user's price list would be needed:

{equip_table}

Include a **Bill of Materials / Equipment Estimate** table with:
| Item | Part # | Qty | Unit Cost | Total Cost |

Only include items that are relevant to the RFP scope. If additional items are needed
that are NOT in the user's price list, add them with `[ACTION REQUIRED: pricing needed]`.
Calculate subtotals by category and a grand total.
""")

        if cost_options.get("include_travel_expenses") and travel_data:
            travel_table = "| Expense Type | Rate | Unit | Description |\n|-------------|------|------|-------------|\n"
            for t in travel_data:
                travel_table += f"| {t['expense_type']} | ${t['rate']:.2f} | {t['unit']} | {t['description'] or ''} |\n"
            sections.append(f"""
### Travel & Expense Estimation (REQUESTED)
The user has requested a travel and expense estimate. Based on the project scope,
location, and duration inferred from the RFP/RFQ, estimate travel costs using these rates:

{travel_table}

Include a **Travel & Expenses Estimate** table with:
| Expense Type | Rate | Qty/Duration | Total Cost |

Consider: number of trips, number of personnel traveling, project duration,
and site location when estimating quantities. Calculate a grand total.
""")

        if sections:
            cost_estimation_block = f"""
## Cost Estimation Instructions

The user has selected the following cost estimation options. You MUST include these
sections in the proposal. Use the user's actual rates — do NOT fabricate pricing.

{"".join(sections)}

### Cost Summary
After the individual cost sections, include a **Total Project Cost Summary** table:
| Category | Estimated Cost |
|----------|---------------|
| Labor | $X |
| Equipment/Materials | $X |
| Travel & Expenses | $X |
| **Total Estimated Cost** | **$X** |

Only include categories that were requested above.
"""

    # Build company standards block
    standards_block = ""
    if company_standards:
        standards_items = []
        for s in company_standards:
            standards_items.append(f"### {s['category']}: {s['title']}\n{s['content']}")
        joined_standards = "\n\n".join(standards_items)
        standards_block = f"""
## Company Standards & Posture

The following are the company's standard boilerplate content, certifications,
past performance narratives, and other reusable sections. You MUST incorporate
relevant standards into the proposal where appropriate. Use the exact content
provided — do not paraphrase certifications or modify factual claims.

{joined_standards}
"""

    # Build AI learning block from past corrections
    learning_block = ""
    if past_corrections:
        correction_items = []
        for c in past_corrections:
            item = f"- **{c['type'].title()}**: {c['summary']}"
            if c.get('original') and c.get('corrected'):
                item += f"\n  - AI wrote: \"{c['original'][:200]}...\""
                item += f"\n  - Human changed to: \"{c['corrected'][:200]}...\""
            correction_items.append(item)
        joined_corrections = "\n".join(correction_items)
        learning_block = f"""
## Learning from Past Corrections

The user has previously reviewed and edited AI-generated proposals. Below are
patterns from those corrections. LEARN from these and adjust your output
accordingly to match the user's preferences and style:

{joined_corrections}

Apply these lessons: match the user's preferred tone, detail level, structure,
and content choices. Avoid repeating the same mistakes identified above.
"""

    # Approved Scope of Work block — when the user has pre-approved a SOW,
    # treat it as authoritative and do not expand or contradict it.
    sow_block = ""
    if approved_sow and approved_sow.strip():
        sow_block = f"""
## Approved Scope of Work (LOCKED)

The user has reviewed and approved the following Scope of Work. Treat In Scope,
Out of Scope, and Assumptions as authoritative and binding.

- Do NOT add work that is outside the "In Scope" section.
- Do NOT remove or silently narrow items listed in "In Scope".
- Do NOT rewrite items from "Out of Scope" as if they were in scope.
- Keep the stated "Assumptions" intact in the proposal (typically in an
  Assumptions section near the end).
- If the RFP requires something that contradicts the approved SOW, flag it as
  `[ACTION REQUIRED: SOW conflict — <short description>]` rather than silently
  resolving it.

```
{approved_sow.strip()[:12000]}
```
"""

    return f"""You are the Proposal Manager Agent — an expert proposal writer that generates
professional proposals in response to customer RFP (Request for Proposal) and
RFQ (Request for Quotation) documents.

{company_line}{sow_block}## Industry Vertical

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

{cost_estimation_block}

{standards_block}

{learning_block}

## Interactive Clarification

If you encounter information in the RFP/RFQ that is ambiguous, contradictory,
or critical to the proposal but missing, you MUST categorize each gap into one
of three resolution paths and include them in a section at the VERY END of your
output titled "## CLARIFICATION QUESTIONS".

Each question MUST be on its own line with this format:
  [RESOLUTION_PATH] Q: Your question here

Where RESOLUTION_PATH is one of:
- **INFER** — You can likely answer this yourself from context, company standards,
  past proposals, or general industry knowledge. Include your proposed answer and
  ask the user to confirm or override. Format:
  [INFER] Q: question text | SUGGESTED: your proposed answer here
- **INTERNAL** — The proposal team likely knows this (margin targets, preferred
  subs, internal resource availability, etc.). This stays within the team.
  [INTERNAL] Q: question text
- **CUSTOMER** — Only the customer/issuer of the RFP can answer this (missing
  specs, contradictory requirements, ambiguous scope, etc.). These will be
  collected into a formal RFI letter to send back to the customer.
  [CUSTOMER] Q: question text

Also tag each question with a category in parentheses at the end:
(scope), (pricing), (compliance), (schedule), (technical), or (general).

Example:
  [INFER] Q: The RFP does not specify the commissioning standard. Based on the
  data center vertical, should we assume ASHRAE Guideline 0? | SUGGESTED: Use
  ASHRAE Guideline 0 per industry standard (compliance)
  [CUSTOMER] Q: Section 3.2 specifies "redundant power" but does not indicate
  N+1 or 2N topology. Which redundancy level is required? (technical)
  [INTERNAL] Q: What margin target should be used for this pursuit? (pricing)

Only ask questions that are truly critical to producing an accurate proposal.
For INFER items, proceed with your suggested answer in the proposal body and
mark it with `[ASSUMED: description — pending confirmation]` so reviewers can
spot assumptions easily.

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
                      progress_callback=None,
                      cost_options: dict = None,
                      staff_roles_data: list = None,
                      equipment_data: list = None,
                      travel_data: list = None,
                      past_corrections: list = None,
                      company_standards: list = None,
                      approved_sow: str = "") -> dict:
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
        cost_options: Dict of booleans for which cost sections to include.
        staff_roles_data: List of staff role dicts from user's settings.
        equipment_data: List of equipment item dicts from user's settings.
        travel_data: List of travel expense rate dicts from user's settings.
        past_corrections: List of correction dicts from past human edits.
        company_standards: List of company standard dicts for auto-injection.

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
        cost_options=cost_options,
        staff_roles_data=staff_roles_data,
        equipment_data=equipment_data,
        travel_data=travel_data,
        past_corrections=past_corrections,
        company_standards=company_standards,
        approved_sow=approved_sow,
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

    client = _make_client(api_key)

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


def _build_sow_system_prompt(vertical_key: str,
                             vertical_resources: dict,
                             company_name: str = "",
                             company_standards: list = None,
                             staff_roles_data: list = None,
                             equipment_data: list = None,
                             travel_data: list = None,
                             user_template_text: str = "",
                             rate_sheet_text: str = "",
                             has_drawings: bool = False) -> str:
    """Assemble the system prompt for Scope of Work generation.

    Deliberately excludes the past-proposals reference library so the SOW is
    an independent assessment of the source documents, not a remix of prior
    proposal output.
    """
    vertical_label = VERTICALS.get(vertical_key, {}).get("label", "General")
    workflow = vertical_resources.get("workflow", "")
    vertical_templates = vertical_resources.get("templates", {})

    company_line = f"You are drafting this Scope of Work on behalf of **{company_name}**.\n\n" if company_name else ""

    # Capability signal blocks — framed as "what we can actually deliver"
    capability_parts: list[str] = []

    if staff_roles_data:
        roles = ", ".join(
            f"{r['role_name']}" + (f" ({r['category']})" if r.get('category') else "")
            for r in staff_roles_data[:40]
        )
        capability_parts.append(f"**In-house staff roles:** {roles}.")

    if equipment_data:
        cats = {}
        for e in equipment_data[:80]:
            cats.setdefault(e.get('category') or 'other', []).append(e.get('item_name', ''))
        lines = [f"- {cat}: {', '.join(items[:10])}" for cat, items in cats.items()]
        capability_parts.append("**Stocked equipment / materials (by category):**\n" + "\n".join(lines))

    if travel_data:
        types = ", ".join(sorted({t['expense_type'] for t in travel_data}))
        capability_parts.append(f"**Travel expense categories available:** {types}.")

    if company_standards:
        std_lines = [f"- {s['category']}: {s['title']}" for s in company_standards[:30]]
        capability_parts.append("**Company standards & posture:**\n" + "\n".join(std_lines))

    if rate_sheet_text:
        capability_parts.append(
            "**Rate / price sheet excerpt (for context only — do NOT price the SOW):**\n"
            f"```\n{rate_sheet_text[:3000]}\n```"
        )

    capability_block = ""
    if capability_parts:
        capability_block = "\n## Company Capability Signal\n\n" + "\n\n".join(capability_parts) + "\n"

    template_block = ""
    if user_template_text:
        template_block += f"\n### User-Uploaded Template (for tone/structure reference)\n```\n{user_template_text[:4000]}\n```\n"
    for name, content in vertical_templates.items():
        template_block += f"\n### Vertical Template: {name}\n```\n{content[:4000]}\n```\n"

    drawings_note = ""
    if has_drawings:
        drawings_note = (
            "\n**Engineering drawings have been attached as images.** Treat them "
            "as authoritative visual references. Use them to infer physical "
            "scope, equipment footprint, routing, quantities you can count, and "
            "work that clearly belongs to other trades (e.g., structural, "
            "architectural, or process-mechanical work drawn but outside our "
            "service lines). Cite drawings by filename/page when relevant.\n"
        )

    return f"""You are the Proposal Manager Scope-of-Work Agent. You produce an independent,
pre-proposal Scope of Work from customer source documents (RFPs, specs,
drawings, supporting files) and the company's own capability profile.

This is NOT a sales document and NOT a proposal. It is an internal
review artifact the sales team will edit and approve before a proposal is
drafted.

{company_line}## Industry Vertical

This project is classified as **{vertical_label}**.

## Workflow Context (reference only)

{workflow}

## Proposal Templates (tone/structure only — do not fill in or price)

{template_block}

{capability_block}
{drawings_note}

## Your Task

1. Read the attached customer source documents end-to-end.
2. Identify every distinct piece of work the customer is asking for.
3. For each piece of work, decide whether it falls within THIS company's
   capabilities and service lines based on the capability signal above.
4. Produce three lists:
   - **In Scope** — work this company will perform.
   - **Out of Scope** — work explicitly NOT being performed, including items
     that appear to belong to other contractors, integrators, suppliers, or
     trades (e.g., general construction, architectural, process-mechanical,
     commissioning agents, owner-furnished equipment).
   - **Assumptions** — conditions, dependencies, access, schedule, site
     readiness, owner-furnished items, code/standard selections, or
     clarifications the scope relies on.

## Rules

- Base your assessment ONLY on the attached customer documents and the
  company capability signal above. Do NOT reference past proposals.
- Tie each In Scope and Out of Scope item back to a specific RFP reference
  (section number, filename, or short quoted phrase) when possible.
- Prefer specific, verifiable scope statements over marketing language.
- Do NOT fabricate quantities, pricing, or model numbers.
- If a piece of work is ambiguous, place it in Assumptions with a clarifying
  statement rather than silently including or excluding it.
- Keep each bullet crisp and atomic (one deliverable per bullet).

## Output Format (STRICT)

Return ONLY a JSON object, no preamble, matching this schema:

```
{{
  "in_scope": [
    {{"text": "...", "rfp_reference": "..." }}
  ],
  "out_of_scope": [
    {{"text": "...", "rfp_reference": "..." }}
  ],
  "assumptions": [
    {{"text": "..." }}
  ]
}}
```

- `rfp_reference` is optional; leave it out or empty-string when no citation
  is possible.
- Do not wrap the JSON in code fences. Return the JSON object by itself.
- Aim for 8-20 items per section. Fewer is fine; do not pad.

Today's date is {datetime.now(timezone.utc).strftime("%B %d, %Y")}.
"""


def generate_sow(rfp_text: str,
                 vertical: str = "auto",
                 company_name: str = "",
                 user_api_key: str = None,
                 user_model: str = None,
                 company_standards: list = None,
                 staff_roles_data: list = None,
                 equipment_data: list = None,
                 travel_data: list = None,
                 user_templates: dict = None,
                 rate_sheet_data: dict = None,
                 drawing_images: list = None,
                 progress_callback=None) -> dict:
    """Generate an independent Scope of Work from customer documents + company context.

    Args:
        rfp_text: Combined text of all customer documents (RFP, specs, etc.).
        vertical: Vertical key or 'auto'.
        company_name: Branding — "on behalf of X".
        user_api_key, user_model: Per-user overrides.
        company_standards, staff_roles_data, equipment_data, travel_data:
            Company capability signal loaded from settings.
        user_templates: Dict mapping template_type -> text (for tone/structure).
        rate_sheet_data: Parsed rate sheet data (raw_text used only as context).
        drawing_images: Optional list of dicts {media_type, data, source_name}
            returned by drawing_ingest.load_drawing_images(). When supplied,
            a multimodal user message is sent and the SOW prompt references
            the attached drawings.
        progress_callback: Optional callable(phase, message).

    Returns:
        dict with keys:
            - in_scope: list[dict] — [{"text": ..., "rfp_reference": ...}]
            - out_of_scope: list[dict]
            - assumptions: list[dict] — [{"text": ...}]
            - raw: str — the unparsed AI response
            - vertical, vertical_label, model, generated_at
            - drawings_count: int
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

    if vertical == "auto":
        from document_parser import detect_vertical
        _report("detection", "Detecting vertical for SOW...")
        vertical = detect_vertical(rfp_text)

    vertical_label = VERTICALS.get(vertical, {}).get("label", "General")
    _report("init", f"Loading {vertical_label} context for SOW...")

    vertical_resources = load_vertical_resources(vertical)

    rate_sheet_text = ""
    if rate_sheet_data:
        for sheet_type, data in rate_sheet_data.items():
            if isinstance(data, dict) and "raw_text" in data:
                rate_sheet_text += f"\n### {sheet_type.replace('_', ' ').title()}\n{data['raw_text']}\n"

    user_template_text = ""
    if user_templates:
        for ttype, text in user_templates.items():
            user_template_text += f"\n### {ttype.replace('_', ' ').title()}\n{text}\n"

    images = drawing_images or []
    system_prompt = _build_sow_system_prompt(
        vertical,
        vertical_resources,
        company_name=company_name,
        company_standards=company_standards,
        staff_roles_data=staff_roles_data,
        equipment_data=equipment_data,
        travel_data=travel_data,
        user_template_text=user_template_text,
        rate_sheet_text=rate_sheet_text,
        has_drawings=bool(images),
    )

    _report("analysis", f"Generating {vertical_label} Scope of Work...")

    user_content: list = []
    for img in images:
        user_content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["media_type"],
                "data": img["data"],
            },
        })
    text_intro = (
        f"Please produce a Scope of Work for a {vertical_label} project based "
        "on the following customer documents"
    )
    if images:
        text_intro += " and the attached engineering drawings"
    text_intro += ". Return JSON only per the schema in the system prompt."
    user_content.append({
        "type": "text",
        "text": (
            f"{text_intro}\n\n"
            "---BEGIN CUSTOMER DOCUMENTS---\n"
            f"{rfp_text}\n"
            "---END CUSTOMER DOCUMENTS---"
        ),
    })

    client = _make_client(api_key)

    response_text = ""
    with client.messages.stream(
        model=model,
        max_tokens=8000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    ) as stream:
        for chunk in stream.text_stream:
            response_text += chunk

    _report("post_processing", "Parsing SOW sections...")
    parsed = _parse_sow_response(response_text)

    return {
        "in_scope": parsed["in_scope"],
        "out_of_scope": parsed["out_of_scope"],
        "assumptions": parsed["assumptions"],
        "raw": response_text,
        "vertical": vertical,
        "vertical_label": vertical_label,
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "drawings_count": len(images),
    }


def _parse_sow_response(text: str) -> dict:
    """Extract the three SOW sections from the AI's JSON response.

    Tolerates the model wrapping JSON in code fences or adding a short preamble.
    Falls back to an empty structure if parsing fails — the raw response is
    preserved by the caller so a user can still manually recover the content.
    """
    empty = {"in_scope": [], "out_of_scope": [], "assumptions": []}
    if not text or not text.strip():
        return empty

    # Find the first {...} block that parses as JSON.
    candidates: list[str] = []
    # Strip code fences if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    # Also try the greediest outer brace match.
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))

    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        def _norm(items, keep_ref=True):
            out = []
            if not isinstance(items, list):
                return out
            for item in items:
                if isinstance(item, str):
                    txt = item.strip()
                    if txt:
                        out.append({"text": txt, "rfp_reference": ""} if keep_ref else {"text": txt})
                elif isinstance(item, dict):
                    txt = (item.get("text") or "").strip()
                    if not txt:
                        continue
                    if keep_ref:
                        out.append({
                            "text": txt,
                            "rfp_reference": (item.get("rfp_reference") or "").strip(),
                        })
                    else:
                        out.append({"text": txt})
            return out

        return {
            "in_scope": _norm(data.get("in_scope", []), keep_ref=True),
            "out_of_scope": _norm(data.get("out_of_scope", []), keep_ref=True),
            "assumptions": _norm(data.get("assumptions", []), keep_ref=False),
        }

    return empty


def sow_items_to_markdown(items: list[dict], include_ref: bool = True) -> str:
    """Render a list of SOW items back to a markdown bullet list."""
    lines: list[str] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        text = (it.get("text") or "").strip()
        if not text:
            continue
        ref = (it.get("rfp_reference") or "").strip() if include_ref else ""
        if ref:
            lines.append(f"- {text} _(ref: {ref})_")
        else:
            lines.append(f"- {text}")
    return "\n".join(lines)


def sow_markdown_to_items(md_text: str) -> list[dict]:
    """Parse a markdown bullet list back into structured items.

    Accepts `- item`, `* item`, and `1. item` bullets; trailing `(ref: ...)`
    or `_(ref: ...)_` annotations are captured as rfp_reference.
    """
    items: list[dict] = []
    if not md_text:
        return items
    for raw in md_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(?:[-*]|\d+\.)\s+(.*)$", line)
        if m:
            body = m.group(1).strip()
        else:
            body = line
        ref = ""
        ref_match = re.search(r"_?\(ref:\s*(.+?)\)_?\s*$", body)
        if ref_match:
            ref = ref_match.group(1).strip()
            body = body[:ref_match.start()].rstrip()
        if body:
            items.append({"text": body, "rfp_reference": ref})
    return items


def assemble_sow_markdown(in_scope_md: str, out_of_scope_md: str,
                          assumptions_md: str, heading_prefix: str = "") -> str:
    """Assemble the three SOW sections into a single markdown document."""
    h = heading_prefix
    parts = [f"# {h}Scope of Work\n" if h else "# Scope of Work\n"]
    parts.append("## In Scope\n")
    parts.append((in_scope_md or "_No in-scope items._") + "\n")
    parts.append("## Out of Scope\n")
    parts.append((out_of_scope_md or "_No out-of-scope items listed._") + "\n")
    parts.append("## Assumptions\n")
    parts.append((assumptions_md or "_No assumptions listed._") + "\n")
    return "\n".join(parts)


def revise_proposal(current_markdown: str,
                    revision_requests: list[dict],
                    vertical: str = "general",
                    company_name: str = "",
                    user_api_key: str = None,
                    user_model: str = None,
                    rate_sheet_text: str = "",
                    company_standards: list = None,
                    past_corrections: list = None) -> dict:
    """Revise an existing proposal by applying a batch of structured revision requests.

    Args:
        current_markdown: The current proposal markdown content.
        revision_requests: List of dicts with keys:
            - source (internal_engineering, customer, etc.)
            - category (pricing, scope, ...)
            - directive (natural-language ask)
            - target_section (optional)
            - author_label (reviewer name/role for the prompt)
        vertical: Vertical key for context.
        company_name: Company name for branding.
        user_api_key: User's Anthropic API key.
        user_model: Override model name.
        rate_sheet_text: Rate sheet context (preserved from original generation).
        company_standards: Company standards list for preservation.
        past_corrections: Learning context.

    Returns:
        dict with:
            - revised_markdown: The new proposal markdown.
            - change_log: List of dicts describing what the AI did.
            - ai_summary: A short human-readable summary of changes.
    """
    api_key = user_api_key or ANTHROPIC_API_KEY
    model = user_model or CLAUDE_MODEL

    if not api_key:
        raise RuntimeError(
            "No API key configured. Go to Settings to add your Anthropic API key."
        )

    if not revision_requests:
        raise ValueError("No revision requests to apply.")

    vertical_label = VERTICALS.get(vertical, {}).get("label", "General")
    company_line = f"You are revising this proposal on behalf of **{company_name}**.\n\n" if company_name else ""

    # Group requests by source/role for clear instruction blocks
    grouped: dict[str, list[dict]] = {}
    for req in revision_requests:
        key = req.get("author_label") or req.get("source", "reviewer").replace("_", " ").title()
        grouped.setdefault(key, []).append(req)

    request_block = ""
    for group_name, reqs in grouped.items():
        request_block += f"\n### {group_name}\n"
        for r in reqs:
            cat = (r.get("category") or "other").title()
            section = r.get("target_section") or ""
            directive = r.get("directive", "").strip()
            tag = f"[{cat}]"
            if section:
                tag += f" [Section: {section}]"
            request_block += f"- {tag} {directive}\n"

    standards_block = ""
    if company_standards:
        lines = []
        for s in company_standards:
            lines.append(f"- {s['category']}: {s['title']}")
        standards_block = "\n## Preserved Company Standards\nThese certifications and claims must remain intact:\n" + "\n".join(lines) + "\n"

    learning_block = ""
    if past_corrections:
        items = []
        for c in past_corrections[:5]:
            items.append(f"- {c.get('type', 'general').title()}: {c.get('summary', '')}")
        learning_block = "\n## Style Preferences Learned From Past Edits\n" + "\n".join(items) + "\n"

    rate_block = ""
    if rate_sheet_text:
        rate_block = f"\n## Rate & Price Sheet (still in effect)\n```\n{rate_sheet_text[:6000]}\n```\n"

    system_prompt = f"""You are the Proposal Manager Revision Agent. You revise an EXISTING
{vertical_label} proposal by applying a batch of structured change requests from
reviewers. You do not rewrite from scratch.

{company_line}## Your Rules

1. Apply EVERY revision request precisely. Preserve everything else byte-for-byte
   when possible, only modifying what the requests demand.
2. Keep all `[ACTION REQUIRED: ...]` markers that remain applicable.
3. If a request requires recalculating totals (e.g., margin bumps, added staff),
   recalculate any affected totals and subtotals in pricing tables.
4. Preserve all section headings, tables, and the overall structure unless a
   request explicitly asks you to change the structure.
5. Do NOT fabricate pricing, personnel names, or certifications. Use
   `[ACTION REQUIRED]` markers for anything the request does not specify.
6. If two requests directly conflict, apply neither and note the conflict in
   the change log.
7. After the revised proposal, emit a structured change log.

## Output Format

Your response MUST contain exactly two sections separated by the marker
`=====CHANGE_LOG=====`:

1. The revised proposal markdown (no preamble, start directly with the proposal).
2. A JSON array `change_log` after the marker, one entry per request:
   ```json
   [
     {{"request_index": 1, "applied": true, "action": "Increased buyout margin by 1%", "sections_touched": ["Pricing"]}},
     {{"request_index": 2, "applied": false, "reason": "Conflicts with request 3"}}
   ]
   ```
   After the JSON, on a new line, write a single-sentence plain-English summary.

{rate_block}
{standards_block}
{learning_block}
"""

    user_message = f"""Please revise the following proposal by applying the revision requests below.

---BEGIN CURRENT PROPOSAL---
{current_markdown}
---END CURRENT PROPOSAL---

---REVISION REQUESTS---
{request_block}
---END REVISION REQUESTS---

Return the revised proposal followed by the =====CHANGE_LOG===== marker and the JSON change log.
"""

    client = _make_client(api_key)

    full_text = ""
    with client.messages.stream(
        model=model,
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for chunk in stream.text_stream:
            full_text += chunk

    return _parse_revision_response(full_text, revision_requests)


def _parse_revision_response(full_text: str, revision_requests: list[dict]) -> dict:
    """Split the AI's revision output into markdown and a structured change log."""
    marker = "=====CHANGE_LOG====="
    if marker in full_text:
        revised_markdown, log_section = full_text.split(marker, 1)
    else:
        revised_markdown, log_section = full_text, ""

    revised_markdown = revised_markdown.strip()
    log_section = log_section.strip()

    change_log: list[dict] = []
    ai_summary = ""

    # Try to extract JSON array from the log section
    json_match = re.search(r"\[.*\]", log_section, re.DOTALL)
    if json_match:
        try:
            change_log = json.loads(json_match.group(0))
        except (json.JSONDecodeError, ValueError):
            change_log = []
        # Anything after the JSON is a natural-language summary
        after_json = log_section[json_match.end():].strip()
        if after_json:
            # Strip code fences if any
            after_json = re.sub(r"^```.*?\n", "", after_json).strip("` \n")
            ai_summary = after_json.split("\n", 1)[0]

    # Fallback: if no structured log, treat every request as applied
    if not change_log:
        change_log = [
            {"request_index": i + 1, "applied": True, "action": "Applied"}
            for i in range(len(revision_requests))
        ]

    if not ai_summary:
        applied_count = sum(1 for c in change_log if c.get("applied"))
        ai_summary = f"Applied {applied_count} of {len(revision_requests)} revision request(s)."

    return {
        "revised_markdown": revised_markdown,
        "change_log": change_log,
        "ai_summary": ai_summary,
    }


def parse_customer_email(email_text: str,
                         user_api_key: str = None,
                         user_model: str = None) -> list[dict]:
    """Parse a raw customer email into a list of structured revision request drafts.

    Returns a list of dicts with 'directive', 'category', and optional
    'target_section' keys. Sales can then accept/edit before committing.

    Raises RuntimeError if no API key is configured.
    """
    api_key = user_api_key or ANTHROPIC_API_KEY
    model = user_model or CLAUDE_MODEL

    if not api_key:
        raise RuntimeError(
            "No API key configured. Go to Settings to add your Anthropic API key."
        )

    if not email_text or not email_text.strip():
        return []

    system_prompt = """You extract actionable proposal revision requests from customer emails.

Given the raw text of a customer's email responding to a proposal, identify every
distinct change the customer is asking for. For each, output:
- directive: a concise, imperative instruction (e.g., "Lower the T&M labor rate by 10%")
- category: one of pricing, scope, resources, schedule, terms, compliance, tone, structure, other
- target_section: the proposal section name if the customer mentions one, else ""

Output format: a JSON array ONLY, no preamble. Example:
[
  {"directive": "Lower T&M labor rate by 10%", "category": "pricing", "target_section": "Pricing"},
  {"directive": "Add a 6-month warranty clause", "category": "terms", "target_section": ""}
]

Rules:
- If the email is just a thank-you or has no revision requests, output `[]`.
- Do NOT invent requests the customer did not make.
- Keep directives short and specific.
- Aim for 1-10 requests per email; combine near-duplicates.
"""

    client = _make_client(api_key)

    response_text = ""
    with client.messages.stream(
        model=model,
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": f"Customer email:\n\n{email_text[:8000]}"}],
    ) as stream:
        for chunk in stream.text_stream:
            response_text += chunk

    # Extract the JSON array
    json_match = re.search(r"\[.*\]", response_text, re.DOTALL)
    if not json_match:
        return []

    try:
        raw = json.loads(json_match.group(0))
    except (json.JSONDecodeError, ValueError):
        return []

    results: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        directive = (item.get("directive") or "").strip()
        if not directive:
            continue
        category = (item.get("category") or "other").strip().lower()
        if category not in {"pricing", "scope", "resources", "schedule", "terms",
                            "compliance", "tone", "structure", "other"}:
            category = "other"
        results.append({
            "directive": directive,
            "category": category,
            "target_section": (item.get("target_section") or "").strip(),
        })

    return results


def preflight_check_proposal(markdown_content: str,
                             user_api_key: str = None,
                             user_model: str = None) -> dict:
    """Run a pre-flight sanity check on a proposal before sending to the customer.

    Returns a dict with:
        - action_items: list of unresolved [ACTION REQUIRED] markers
        - warnings: list of natural-language warnings from the AI
        - ready: bool, True if no critical issues
    """
    # Local action-item scan (no API call needed)
    action_items = re.findall(r"\[ACTION REQUIRED:\s*(.+?)\]", markdown_content)

    warnings: list[str] = []

    if action_items:
        warnings.append(f"{len(action_items)} unresolved [ACTION REQUIRED] marker(s) still in the proposal.")

    # Basic structural checks
    if "## Pricing" not in markdown_content and "## Cost" not in markdown_content and "## Price" not in markdown_content:
        warnings.append("No Pricing/Cost section header detected — customer may expect one.")

    if len(markdown_content.strip()) < 500:
        warnings.append("Proposal content is unusually short (<500 chars).")

    # Check for obviously unreplaced placeholders
    if re.search(r"\bTBD\b", markdown_content) or re.search(r"\bXXX\b", markdown_content):
        warnings.append("Contains TBD/XXX placeholders.")

    # Try an AI-based deeper check (graceful degradation if no key)
    api_key = user_api_key or ANTHROPIC_API_KEY
    model = user_model or CLAUDE_MODEL
    if api_key:
        try:
            client = _make_client(api_key)
            system_prompt = (
                "You are a proposal quality reviewer. Look at this proposal and "
                "identify any issues that could embarrass the sender if submitted "
                "to a customer: inconsistent numbers, obvious typos in key places, "
                "missing totals, ambiguous scope statements. Respond with a JSON "
                "array of short warning strings, e.g. "
                '["Labor total ($50k) does not match line items ($48k)"]. '
                "If the proposal looks ready, respond with []."
            )
            result_text = ""
            with client.messages.stream(
                model=model,
                max_tokens=1000,
                system=system_prompt,
                messages=[{"role": "user", "content": markdown_content[:12000]}],
            ) as stream:
                for chunk in stream.text_stream:
                    result_text += chunk
            json_match = re.search(r"\[.*\]", result_text, re.DOTALL)
            if json_match:
                try:
                    ai_warnings = json.loads(json_match.group(0))
                    if isinstance(ai_warnings, list):
                        for w in ai_warnings:
                            if isinstance(w, str) and w.strip():
                                warnings.append(w.strip())
                except (json.JSONDecodeError, ValueError):
                    pass
        except Exception as e:
            # Don't fail pre-flight, but make the failure visible so users
            # aren't told "all clear" when the AI review never actually ran.
            warnings.append(f"AI review could not run — {friendly_api_error(e)}")

    return {
        "action_items": action_items,
        "warnings": warnings,
        "ready": len(warnings) == 0,
    }


def _extract_questions(text: str) -> list[dict]:
    """Extract clarification questions from the proposal output.

    Parses the new categorized format:
      [INFER] Q: question | SUGGESTED: answer (category)
      [INTERNAL] Q: question (category)
      [CUSTOMER] Q: question (category)

    Falls back to legacy format (plain Q: lines) for backwards compatibility.
    """
    match = re.search(r"## CLARIFICATION QUESTIONS\s*\n(.*)", text, re.DOTALL)
    if not match:
        return []

    questions_text = match.group(1)
    questions = []

    for line in questions_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        # New categorized format: [INFER|INTERNAL|CUSTOMER] Q: ...
        cat_match = re.match(
            r"\[(INFER|INTERNAL|CUSTOMER)\]\s*Q:\s*(.+)", line, re.IGNORECASE
        )
        if cat_match:
            resolution_path = cat_match.group(1).lower()
            raw_question = cat_match.group(2).strip()

            # Extract category tag like (scope), (pricing), etc.
            category = "general"
            cat_tag = re.search(r"\((\w+)\)\s*$", raw_question)
            if cat_tag:
                category = cat_tag.group(1).lower()
                raw_question = raw_question[:cat_tag.start()].strip()

            # Extract AI suggestion for INFER items
            ai_suggestion = ""
            if resolution_path == "infer" and "| SUGGESTED:" in raw_question:
                parts = raw_question.split("| SUGGESTED:", 1)
                raw_question = parts[0].strip()
                ai_suggestion = parts[1].strip()

            questions.append({
                "question": raw_question,
                "context": "",
                "resolution_path": resolution_path,
                "category": category,
                "ai_suggestion": ai_suggestion,
            })
            continue

        # Legacy format: Q: question or numbered list
        if line.startswith("Q:") or line.startswith("Q "):
            q_text = line[2:].strip().lstrip(":").strip()
            if q_text:
                questions.append({
                    "question": q_text,
                    "context": "",
                    "resolution_path": "internal",
                    "category": "general",
                    "ai_suggestion": "",
                })
        elif re.match(r"^\d+[\.\)]\s*", line):
            q_text = re.sub(r"^\d+[\.\)]\s*", "", line).strip()
            if q_text:
                questions.append({
                    "question": q_text,
                    "context": "",
                    "resolution_path": "internal",
                    "category": "general",
                    "ai_suggestion": "",
                })

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


def regenerate_section(
    full_proposal_md: str,
    section_heading: str,
    clarification_answer: str,
    original_rfp_text: str = "",
    company_name: str = "",
    user_api_key: str = None,
    user_model: str = None,
) -> dict:
    """Regenerate a specific section of the proposal using new clarification info.

    Instead of regenerating the entire proposal, this targets just the affected
    section and returns the updated markdown for that section only.

    Args:
        full_proposal_md: The current full proposal markdown.
        section_heading: The heading of the section to regenerate (e.g. "## Scope of Work").
        clarification_answer: The new information/answer that should be incorporated.
        original_rfp_text: Optional RFP text for additional context.
        company_name: Company name for branding.
        user_api_key: User's API key override.
        user_model: User's model override.

    Returns:
        dict with 'section_markdown' (the regenerated section) and 'section_heading'.
    """
    api_key = user_api_key or ANTHROPIC_API_KEY
    model = user_model or CLAUDE_MODEL

    if not api_key:
        raise RuntimeError("No API key configured.")

    # Extract the target section from the proposal
    section_pattern = re.escape(section_heading)
    match = re.search(
        rf"({section_pattern}.*?)(?=\n##\s|\Z)", full_proposal_md, re.DOTALL
    )
    current_section = match.group(1).strip() if match else ""

    system_prompt = f"""You are a proposal editor assistant. Your task is to revise a SINGLE SECTION
of an existing proposal based on new clarification information.

{f'You are writing on behalf of **{company_name}**.' if company_name else ''}

## Rules
1. Only output the revised section — do NOT output the entire proposal.
2. Maintain the same markdown heading level and formatting style.
3. Incorporate the new information naturally into the existing content.
4. Remove any [ACTION REQUIRED] or [ASSUMED] placeholders that are resolved
   by the new information.
5. Do NOT fabricate pricing, personnel names, or specifications.
6. Keep the same tone and detail level as the surrounding proposal.
7. Today's date is {datetime.now(timezone.utc).strftime("%B %d, %Y")}.
"""

    user_message = f"""Please revise the following proposal section based on the new clarification answer provided.

## Current Section
```
{current_section}
```

## New Clarification Information
{clarification_answer}

{f'## Original RFP Context (for reference){chr(10)}```{chr(10)}{original_rfp_text[:4000]}{chr(10)}```' if original_rfp_text else ''}

Output ONLY the revised section in markdown, starting with the section heading.
"""

    client = _make_client(api_key)
    result_text = ""
    with client.messages.stream(
        model=model,
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            result_text += text

    return {
        "section_markdown": result_text.strip(),
        "section_heading": section_heading,
    }


def analyze_addendum_impact(
    original_rfp_text: str,
    addendum_text: str,
    current_proposal_md: str,
    user_api_key: str = None,
    user_model: str = None,
) -> dict:
    """Analyze an addendum/amendment against the original RFP and current proposal.

    Identifies what changed in the addendum and which proposal sections need updating.

    Args:
        original_rfp_text: The original RFP document text.
        addendum_text: The addendum/amendment document text.
        current_proposal_md: The current proposal markdown.
        user_api_key: User's API key override.
        user_model: User's model override.

    Returns:
        dict with 'changes' (list of change dicts) and 'summary'.
    """
    api_key = user_api_key or ANTHROPIC_API_KEY
    model = user_model or CLAUDE_MODEL

    if not api_key:
        raise RuntimeError("No API key configured.")

    system_prompt = """You are an expert proposal analyst. Your task is to analyze an RFP addendum
and determine its impact on an existing proposal.

## Output Format
Return a JSON object with this structure:
{
  "summary": "Brief overall summary of addendum changes",
  "changes": [
    {
      "addendum_item": "What the addendum says",
      "impact_description": "How this affects the proposal",
      "affected_sections": ["## Section Heading 1", "## Section Heading 2"],
      "severity": "high|medium|low",
      "action_needed": "What specifically needs to change",
      "can_ai_resolve": true/false,
      "suggested_resolution": "If AI can resolve, the suggested text change"
    }
  ]
}

## Rules
- Be specific about which proposal sections are affected.
- Use the exact heading text from the proposal for affected_sections.
- Classify severity: high = changes scope/pricing/compliance, medium = changes details, low = cosmetic/minor.
- If the addendum merely clarifies something already handled correctly, note it but set severity to low.
- Do NOT fabricate impacts — only report genuine changes.
"""

    user_message = f"""Analyze the following addendum against the original RFP and current proposal.

## Original RFP (first 6000 chars)
```
{original_rfp_text[:6000]}
```

## Addendum
```
{addendum_text[:6000]}
```

## Current Proposal (first 8000 chars)
```
{current_proposal_md[:8000]}
```

Return your analysis as the JSON object described in the instructions.
"""

    client = _make_client(api_key)
    result_text = ""
    with client.messages.stream(
        model=model,
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            result_text += text

    # Parse JSON from response
    try:
        # Try to extract JSON from markdown code blocks if present
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", result_text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group(1))
        else:
            result = json.loads(result_text)
    except (json.JSONDecodeError, AttributeError):
        result = {
            "summary": "Could not parse addendum analysis. Raw output included.",
            "changes": [],
            "raw_output": result_text,
        }

    return result

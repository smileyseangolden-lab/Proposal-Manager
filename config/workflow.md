# Proposal Generation Workflow

This document defines the step-by-step process the Proposal Manager Agent follows
when generating a proposal from an uploaded RFP/RFQ document.

---

## Phase 1: Document Intake & Analysis

### Step 1.1 — Parse the Uploaded Document
- Accept the uploaded RFP/RFQ in PDF, DOCX, or plain-text format.
- Extract all text content while preserving section structure.
- Identify document type (RFP vs RFQ) from headers, titles, and content cues.

### Step 1.2 — Extract Key Requirements
From the parsed document, identify and extract:
- **Solicitation metadata**: issuing organization, solicitation number, due date,
  point of contact, submission instructions.
- **Scope of work**: what goods/services are being requested.
- **Technical requirements**: specifications, standards, certifications, compliance items.
- **Evaluation criteria**: how proposals will be scored/evaluated.
- **Deliverables & milestones**: what must be delivered and when.
- **Terms & conditions**: contractual, legal, or insurance requirements.
- **Pricing structure**: how pricing should be presented (line items, T&M, firm-fixed, etc.).
- **Page/format constraints**: page limits, font requirements, section ordering mandated
  by the issuer.

### Step 1.3 — Requirement Classification
Classify each extracted requirement as:
- **Mandatory** (must comply)
- **Desired** (should address if possible)
- **Informational** (no response needed)

---

## Phase 2: Reference & Template Selection

### Step 2.1 — Match Against Past Proposals
- Search the `reference_documents/past_proposals/` library for proposals that addressed
  similar scope, industry, or customer.
- Rank matches by relevance to inform tone, depth, and structure.

### Step 2.2 — Select Proposal Template
- Choose the appropriate template from `templates/proposal_boilerplate/` based on:
  - Document type (RFP response vs RFQ response)
  - Pricing model (fixed-price, T&M, hybrid)
  - Complexity level (simple quote vs full technical proposal)

### Step 2.3 — Load Boilerplate Sections
- Pull standard boilerplate for sections like:
  - Company overview / About Us
  - Past performance / Case studies
  - Quality assurance approach
  - Safety & compliance
  - Terms & conditions acceptance

---

## Phase 3: Proposal Drafting

### Step 3.1 — Build Proposal Outline
- Generate a section-by-section outline that maps to the RFP/RFQ requirements.
- Ensure every mandatory requirement has a corresponding proposal section.
- Respect any section ordering or formatting mandated by the issuer.

### Step 3.2 — Draft Each Section
For each section in the outline:
1. Pull relevant boilerplate as a starting point.
2. Tailor content to address the specific requirement from the RFP/RFQ.
3. Incorporate relevant details from matched past proposals.
4. Use professional, persuasive language appropriate for the audience.
5. Include placeholders (marked with `[ACTION REQUIRED]`) for:
   - Specific pricing figures
   - Named personnel / resumes
   - Project-specific dates or timelines
   - Customer-specific references the team must verify

### Step 3.3 — Executive Summary
- Draft a compelling executive summary that:
  - Demonstrates understanding of the customer's needs.
  - Highlights key differentiators and value proposition.
  - Summarizes the proposed approach and benefits.

### Step 3.4 — Pricing Section
- Structure pricing according to the format requested in the RFP/RFQ.
- Insert placeholder line items with `[ACTION REQUIRED: Enter pricing]` tags.
- Include any standard terms (payment schedule, validity period).

---

## Phase 4: Compliance & Quality Review

### Step 4.1 — Compliance Matrix
- Generate a compliance matrix mapping every RFP/RFQ requirement to the
  corresponding proposal section and page.
- Flag any requirements that could not be fully addressed with
  `[ACTION REQUIRED: Team review needed]`.

### Step 4.2 — Completeness Check
- Verify all mandatory requirements have a response.
- Verify all requested attachments/appendices are referenced.
- Check for internal consistency (dates, names, figures).

### Step 4.3 — Formatting Validation
- Ensure the proposal follows the selected template formatting.
- Respect any page limits or font requirements from the RFP/RFQ.
- Validate section numbering and cross-references.

---

## Phase 5: Output Generation

### Step 5.1 — Assemble Final Document
- Combine all drafted sections into the final proposal document.
- Insert table of contents.
- Apply consistent formatting from the template.

### Step 5.2 — Generate Supporting Artifacts
- **Compliance matrix** (as a separate table)
- **Requirements traceability summary**
- **Action items list** (all `[ACTION REQUIRED]` items consolidated)

### Step 5.3 — Deliver Output
- Save the generated proposal as DOCX and/or Markdown.
- Present the user with:
  - The full generated proposal (downloadable).
  - The compliance matrix.
  - A summary of action items requiring human input.
  - A confidence score for how completely the RFP/RFQ was addressed.

---

## Notes for the Proposal Team
- All `[ACTION REQUIRED]` placeholders MUST be reviewed and completed by a team member
  before submission.
- The agent uses past proposals as stylistic and structural references — always verify
  that carried-over content is accurate for the current opportunity.
- Pricing is NEVER auto-populated with real figures; it always requires human entry.

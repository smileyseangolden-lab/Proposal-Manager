# Enhanced Overview of MCT AE Flow — AI Agent Training Document

**Document Purpose:** This document provides the detailed workflow narrative for E Tech Group's Mission Critical Team (MCT) proposal process. It is designed to train an AI agent to understand each step of the process and produce first-draft MCT/Data Center proposals from RFPs and RFQs.

**Version:** 3.0 — Updated to incorporate the MCT Proposal RACI Matrix (r2)
**Date:** February 19, 2026

---

## Key Roles in the MCT Proposal Process

The MCT proposal process involves a cross-functional team. The following roles participate, with responsibilities defined by the MCT Proposal RACI Matrix. The RACI designations are: R = Responsible (does the work), A = Accountable (owns the outcome), C = Consulted (provides input), I = Informed (kept in the loop).

| Role | Abbreviation | Primary Responsibility in the Proposal Process |
|------|-------------|------------------------------------------------|
| Business Development Manager | BDM | Owns client relationship, defines win theme, leads margin reviews, submits proposal to client |
| Proposal Manager | Prop Mgr | Accountable for the overall proposal process — orchestrates the pursuit from kickoff through submission. Owns project schedule, price summary, scope/schedule/quality narrative, and bidding workbook |
| Application Engineer | App Eng / AE | Technical execution — performs material takeoffs, builds the estimate, develops LV Sub SOW, drafts instrumentation scope, writes exceptions/clarifications |
| Procurement | Proc | Sources and manages subcontractors, instrument vendors, and other buyout items (trailers, etc), as needed. Assembles and distributes sub bid packages, sends RFQs to vendors, manages sub meetings, receives and vets sub proposals and vendor quotes |
| Operations Director | Ops Director | Consulted on key decisions including subcontractor selection, RFIs, material takeoffs, and margin reviews |
| Operations PM | Ops PM | Consulted on subcontractor selection, estimate categories, including all labor hours, with a focus on GC hours (all management / supervisor hours) and material takeoffs |
| Operations Engineer | Ops Eng | Jointly Responsible with AE for LV Sub SOW development and RFI formulation; Consulted on estimates and material takeoffs; participates in sub proposal vetting |
| Project Controls | PC | Responsible for administrative setup (vault folder, NDAs, prequal, bond requirements); supports document management. QA/QC of estimate before final review. |
| Executive Team | Executive | CEO, CFO, CRO, President, MCT BU VP, MCT BU Sales VP — conducts margin reviews and approves final pricing |

---

## Process Overview

The MCT proposal process consists of the following phases, expanded from the original nine steps to reflect the full RACI task structure:

1. Administrative Setup & RFP Receipt
2. Review Prints and Specs for Material Takeoff and Counts
3. Develop SOW for Low Voltage Sub
4. Subcontractor Procurement & Bid Management
5. Get Quotes from Suppliers / Material Estimation
6. Input into Estimate
7. Create Proposal
8. Review (Peer Review, VP Review, Executive Margin Review)
9. Submit Proposal

---

## Step 1: Administrative Setup & RFP Receipt

### 1A: Receive and Organize the RFP

**RACI:** BDM (A for vetting), Prop Mgr (A for organization), PC (R for admin setup), AE (R for document upload and review)

**Inputs:**
The RFP/RFQ is received by the Business Development team through various channels depending on the client type and relationship — platforms like Amazon Concentric or BuildingConnected, or via direct email from the client or General Contractor (GC). The channel varies by client type (Colocation vs. Hyperscale). The MCT team does not distinguish between an RFP and an RFQ — both are treated the same way.

A typical bid package includes: design drawings, specifications, a schedule, scope of work document(s), safety bulletins, supplemental documents, sometimes a bill of materials, bid response forms specific to the client, load banking quantities, mechanical breakdowns, safety plans, site logistics, and worker and safety instructions. The contents vary significantly between clients.

**Administrative Actions (before technical work begins):**

| Action | Responsible | Accountable | Notes |
|--------|-------------|-------------|-------|
| Create Vault Folder | PC | BDM | Central repository for all proposal documents |
| Fill out Vetting Tool | Prop Mgr | BDM | Initial project assessment |
| Assign Proposal Pursuit Team | Prop Mgr | BDM | Determines who will work on this pursuit |
| Create Submittals to Client Folder | Prop Mgr | BDM | Folder for final deliverables |
| Upload all RFP/Bid Docs to Proposal Folder | AE | Prop Mgr | AE ensures all documents are accessible |
| Upload Estimate Spreadsheet | AE | Prop Mgr | Blank template for the correct program |
| Finish Proposal Action Item List | AE | Prop Mgr | Tracks all tasks and due dates |
| Review RFQ Docs | AE | Prop Mgr | Technical review of the full bid package; Ops PM and Ops Eng are consulted |
| Prequal | PC | BDM | Prequalification requirements if applicable |

### 1B: Initial Scoping and Bid/No-Bid Decision

When the AE reviews the RFP package, they contact the BDM to get a better understanding of the project context. The AE immediately begins scoping the effort by determining several key factors.

**Key Decisions at This Step:**

- **System type:** Is this a BMS project, an EPMS project, or both?
- **Delivery model:** Is this a turnkey bid (E Tech procures materials and executes) or labor-only?
- **Program assignment:** Is this a Hyperscale, OSI, or Colocation project? (The specific group assignment is determined by BD and the Program Directors — the AE group is not privy to that process.)
- **Bid/No-Bid evaluation:** The team assesses whether to proceed based on: project timing, project size (megawatts), availability of low voltage contractors and other contractors at that location, whether the site location is suitable for E Tech Group's capabilities, and whether it logistically makes sense to pursue.
- **Win Theme:** The BDM is Responsible for defining the win theme for the pursuit — Quality, Experience, or Value Engineering. This strategic framing informs how the AI agent should position the entire proposal.

**Output:** A go/no-go decision, an assigned pursuit team with defined roles per the RACI, a defined win theme, and an organized document repository with all RFP materials accessible to the team.

---

## Step 2: Review Prints and Specs for Material Takeoff and Counts

**RACI:** AE (R), Prop Mgr (A), Ops Director and Ops Eng (C/R)

**Inputs:**
The full bid package from Step 1, specifically the design drawings and specifications. The AE reviews multiple drawing sets including: Mechanical, Architecture, Telecom, Controls and Automation, Electrical, Plumbing, and Technology. Most of these drawing sets are always present regardless of project type. Additional drawing sets may also be included (e.g., structural, civil, fire protection).

**Process:**
The AE physically goes sheet by sheet through every drawing set, marking up and counting every relevant item: instruments (temperature sensors, humidity sensors, flow meters, control valves, differential pressure sensors, etc.), panels (PLC panels, RIO panels, leak detection panels, etc.), gateways, VAV controllers, network devices, cable runs, and conduit paths. For each item, the AE documents: the quantity, its location in the facility, the signal type, wiring type, where it is wired to (destination panel), who is supplying it (E Tech, client/OFCI, or LV Sub), and who is installing it.

The AE is identifying everything that falls within E Tech's controls scope — specifically anything related to Controls, BMS, BAS, EPMS, PMS, and low voltage electrical. Life-supporting devices such as fire or hydrogen sensors tied to life safety systems are excluded from E Tech's scope (though standalone hydrogen sensors for environmental monitoring may be included).

The takeoff also captures:

- **Panel counts and details:** Number of panels, Bill of Materials (BOM) requirements, panel print needs, which instruments connect to each panel, and what OFCI equipment connects to the panels.
- **BMS OFCI interfaced items:** What needs to be wired and installed on owner-furnished equipment, what instruments are installed on OFCI equipment, and what data needs to be collected from OFCI equipment.
- **Software requirements:** Software platforms required (e.g., Ignition, Rockwell), licensing needs and costs.
- **Programming scope:** Standard monitoring points for BMS and EPMS, I/O counts per panel and per system.
- **Wire and cable requirements:** Cable types, colors (per client specifications), conduit requirements including sizing and fill rates.

Some AEs use the BMS/EPMS Automation Worksheet (a standardized scope matrix spreadsheet) to organize their takeoff — this worksheet maps every device by facility area (CRAH Gallery, Data Hall, Electrical Rooms, Mechanical Room, etc.) with columns for quantity, who provides it, who installs it, media/protocol, destination panel, points per unit, hard-wire runs, and PLC I/O types. It also includes a built-in man-hours calculator. However, some AEs go straight to the program-specific estimate workbook without using the worksheet — both approaches are acceptable.

**RFI Process:** If critical details are missing or ambiguous, the AE formulates Requests for Information (RFIs) and submits them to the customer. Per the RACI, the AE is Responsible and the Ops Eng is jointly Responsible/Consulted on takeoffs and RFI development. The Prop Mgr is Accountable and the Ops Director is Consulted.

**Key Decisions at This Step:**

- Interpreting ambiguous drawings — the AE must make professional judgments when drawings are unclear or incomplete. The AE should consult with the Ops Eng when the scope is unclear. The Ops Eng can escalate to their engineering management or SMEs, if needed.
- Identifying missing information and deciding whether to submit RFIs or make assumptions.
- Making assumptions about quantities when information is incomplete.
- Asking project engineers for help on technical questions that require specialized expertise.
- Determining which contractors to send scope packages to for subcontractor bids. The Procurement Manager is responsible for recommending subcontractors. The BDM and BU Leadership decide which subcontractors will be asked to bid.

**Typical Duration:** A day or two for E Tech's internal takeoff work, but the overall process can stretch to weeks depending on project size and the time required to get bids back from contractors (which is outside E Tech's control).

**Output:** A completed material takeoff with instrument counts, panel counts, cable/conduit quantities, I/O counts, software requirements, and a categorized list of all scope items with ownership assignments (E Tech vs. OFCI vs. LV Sub). This data feeds directly into the estimate workbook (Step 6) and informs the LV Sub SOW (Step 3).

---

## Step 3: Develop SOW for Low Voltage Sub

**RACI:** AE (R), Ops Eng (R/C — jointly Responsible), Prop Mgr (A)

**Inputs:**
The RFP specifications, design drawings, project timeline, and the material takeoff data from Step 2. The AE starts from a standard LV Sub SOW template — there is one template used for all MCT programs.

**Process:**
The AE drafts the Scope of Work for the Low Voltage Subcontractor (LV Sub), with the Operations Engineer jointly Responsible or Consulted on the content. This is a standalone deliverable — a detailed document that tells the LV Sub exactly what they are responsible for procuring, installing, and testing.

Key information that flows from the RFP into the LV Sub SOW includes: specific cable color requirements, conduit fill rates, connector types, and safety staffing requirements. These requirements are always dictated by the client specifications — the AE does not set these based on E Tech standards.

The scope boundary between E Tech and the LV Sub is consistent: the LV Sub does conduit and wire installation; E Tech does commissioning and programming. Specifically, the LV Sub is typically responsible for: procurement of raceways/conduit/cables, installation of conduit and accessories, cable pulling and termination, installation of EPMS control panels (physical mounting), and verification of proper wiring (continuity checks, labeling). E Tech retains responsibility for: construction/field supervision, programming, point-to-point commissioning, system commissioning support, and as-built documentation.

**Review Gate:** After the AE finishes building the LV Sub SOW and prior to distribution for subcontractor bids, it is reviewed by an Ops Engineer and Procurement Manager.

**Timeline:** The target is to get LV Sub bids back within 7-10 business days of SOW distribution. Ten days is a reasonable deadline, but accelerated project and bidding schedule may not allow for that. After 7 business days, the team starts requesting updates from the subs.

**Output:** A completed LV Sub SOW document, reviewed by an Ops Eng, ready to be handed to Procurement for sub bid package assembly and distribution.

---

## Step 4: Subcontractor Procurement & Bid Management

**RACI:** Procurement (R for most tasks), Prop Mgr (A), AE (C on package assembly, R on proposal vetting), Ops Eng (R/C on proposal vetting), BDM/Ops Director/Executive (C* on sub selection — conditional)

**Inputs:**
The completed LV Sub SOW from Step 3, plus relevant client drawings, standards, and specifications for assembling the sub bid package.

**Process:**
This step is managed primarily by Procurement, with the Prop Mgr as the Accountable owner. The RACI defines the following task flow:

| Action | Responsible | Accountable | Consulted |
|--------|-------------|-------------|-----------|
| Source Subcontractors | Procurement | Prop Mgr | Ops Director, Ops PM, Ops Eng |
| Send NDAs to Subcontractors | PC | Prop Mgr | — |
| Develop Sub RFQ and assemble Sub bid docs | Procurement | Prop Mgr | AE, Ops Eng |
| Create Dropbox link for RFQ Docs | Procurement | Prop Mgr | AE |
| Send RFQs to Subcontractors | Procurement | Prop Mgr | — |
| Create Sub Meeting Agenda | Procurement | Prop Mgr | AE, Ops Eng |
| Set up Sub Meetings | Procurement | Prop Mgr | — |
| Meetings / Subcontractor Support | Procurement | Prop Mgr | AE |
| Receive Subcontract Proposals | Procurement | Prop Mgr | — |
| Vet Subcontract Proposal | AE, Procurement, Ops Eng | Prop Mgr | — |
| Select Subcontractor | Procurement | Prop Mgr | BDM, Ops Director, Ops PM, Executive* |

*Note: BDM, Ops Director, and Executive are conditionally consulted on sub selection (marked C* in the RACI), meaning their involvement depends on the project's strategic importance or size.*

The team targets at least 2 bids, but sometimes only 1 is obtainable depending on location and availability. The format of LV Sub quotes varies depending on the GC or end user being supported. E Tech tries to have the LV contractor follow the same format or breakout structure that E Tech is being asked to provide to the customer.

**Selection Criteria:**
LV Sub selection is based on: local presence, price, customer knowledge (familiarity with the client's standards and expectations), and client preference. Sometimes multiple bids are not required if the right contractor is already identified. Especially if that contractor has recently completed a successful project with E Tech.

**If Bids Come in Over Budget:**
The Prop Mgr, AE, and Ops Eng go through bid leveling to identify scope gaps driving the price. The Procurement Mgr and Ops Director should be consulted. If the gap cannot be resolved and the bid number is firm, E Tech submits the scope as an alternate in the proposal so the customer can choose whether they want E Tech to carry that scope or not.

**What the AI Agent Should Know:** This step is always a "Needs Input" flag. The AI agent cannot solicit or evaluate LV Sub bids, but it CAN generate the LV Sub SOW (Step 3) and identify the information needed for the bid package. The AI agent should structure the proposal pricing section to accommodate the sub's cost. E Tech defines the sub's scope and any assumptions or exclusions agreed upon during bid leveling.

**Output:** A selected LV Sub with a reviewed and leveled quote, ready to be input into the estimate workbook.

---

## Step 5: Get Quotes from Suppliers / Material Estimation

**RACI:** Procurement (R for vendor RFQs and material pricing — instruments, panels), AE (R for expense estimation), Prop Mgr (A)

**Inputs:**
The material takeoff from Step 2, which identifies all equipment and materials that need to be procured.

**Process:**
Per the RACI, Procurement is Responsible for sending RFQs to vendors and pricing materials. The Prop Mgr is Accountable and the Ops Director is Consulted.

Categories typically quoted include: instruments, panels, control hardware, software licenses, valves, breakers, gateways, IT/OT hardware, and MCCs (Motor Control Centers).

The AE determines what to quote vs. what is OFCI based on what is specified in the RFQ or in site standards. Ops Eng is consulted and reviews the instrument take-offs. E Tech maintains a preferred supplier list; if the needed supplier is not on the list, the AE or Procurement will find an appropriate supplier. In some cases, the customer has mandated suppliers or approved manufacturer lists specified in the specifications.

Supplier quotes typically come back as line items. The AE needs to map those line items into the customer's required workbook or breakout format — this is a manual process.

**Material Estimation (per the RACI):**

| Estimate Category | Responsible | Accountable | Consulted |
|-------------------|-------------|-------------|-----------|
| Instruments | Procurement | Prop Mgr | AE |
| Panels | Procurement | Prop Mgr | AE |

**Panel Shop Quoting:**
The go-to rule is to quote using E Tech's internal panel shops. If the internal panel shops cannot meet the customer's timeline, E Tech will look to outsource. In very specific cases, E Tech's panel shops need to be approved by the end user, and if E Tech is at capacity and cannot get approval for an external shop, this may require a no-bid on the panel scope. When panel fabrication is OFCI (client-provided), it will be called out in the RFQ or in the standards.

**Expense Estimation (per the RACI — AE is Responsible for all):**
The AE estimates the following expense categories, with the Prop Mgr as Accountable and Procurement Consulted where applicable:

- Bond Requirements (PC is Accountable — must inform ETG Accounting of any potential bond requirements)
- Buggies / Trucks / Fuel
- Forklift
- Warehouse
- Trailer
- Miscellaneous Office / Field Supplies
- Internet
- Per Diem (sourced from GSA per diem rates at www.gsa.gov/travel/plan-book/per-diem-rates, specific to the project's city location)
- OCIP (Owner Controlled Insurance Program — PC is Consulted)
- Permits / Textura

**Output:** Supplier quotes for all procured materials and equipment, panel shop quotes, and a complete expense estimate — all ready to be input into the estimate workbook.

---

## Step 6: Input into Estimate

**RACI:** AE (R for all labor hour categories and expense categories), Prop Mgr (A), various roles Consulted per category
**Reviewers:** Ops Director and Ops PM (peer review after completion)

**Inputs:**
All data gathered from Steps 2–5: the material takeoff with instrument/panel/I/O counts, the LV Sub quote, supplier quotes, panel shop quotes, project timeline from the RFP, expense estimates, and labor/travel assumptions.

**Process:**
The AE inputs all gathered data into the program-specific estimate workbook. The choice of workbook is determined by which MCT program the project falls under — this is always clear-cut:

- **Hyperscale Estimate Workbook** — for Hyperscale program projects. Key tabs include: SimpleSummary (rollup), GC Labor, ENG Labor, PM Labor, Materials, Expenses, Subcontract, Alt Adds, and Rate Schedule. A distinguishing feature is that it has preset rates for multiple years extending into the future and automatically calculates labor costs on a month-to-month basis spanning more than one year.
- **Colo Estimate Workbook** — for Colocation program projects. Key tabs include: Summary (with role-based bill/cost rates), CAD, ENG, IT, MGMT hours worksheets, Materials, ETG Panels, Expenses, Subcontract, Alt Adds, IO Summary, and IO Metrics.
- **OSI Estimate Workbook** — for Operations Systems Integrator program projects. Key tabs include: Financial Summary (with margin settings and contingency/escalation multipliers), 1000_Labor, 2000_Materials, 3000_Subcontracts, 4000_Expenses, Rate Sheet, and WBS.

All three workbooks share common core tabs (Summary, Materials, Expenses, Subcontractor, Risk Registry, Rate Sheet), but each has unique features. There is currently an effort underway to consolidate all three into one common estimate tool.

Each workbook contains fixed labor rates, but these vary by project, region, and client and may be modified by the Ops Director during peer reviews.

**Estimating Labor Hours (per the RACI — AE is Responsible for all, Prop Mgr is Accountable):**

| Hour Category | Consulted By | Notes |
|---------------|-------------|-------|
| Management Hours | Procurement, Ops PM | PM duration across project lifecycle |
| Engineering Hours | Ops Eng | Team size for commissioning phases |
| Commissioning | Procurement, Ops PM, Ops Eng | On-site CX team sizing and rotation planning |
| Admin / Procurement | PC | Buyer, Project Controller and PA hours |
| Construction | Procurement, Ops PM, Ops Eng | Field supervision and LV Sub oversight |
| Estimate COW (Cost of Work) | Procurement, Ops PM, Ops Eng | The COW breakout is not always required — depends on client |
| Estimate GC (General Conditions) | Procurement | The GC breakout is not always required — depends on client |

**Project Schedule Creation:**
Per the RACI, the AE is Responsible for creating the project schedule. The Prop Mgr is Accountable. The Ops PM is Consulted, along with Procurement and Ops Eng. This schedule feeds into the labor hour estimates and the proposal's schedule section.

**Travel and Per Diem:**
Travel estimation considers the number of people required at site and the project duration. Accommodation rates are sourced from the GSA per diem rates website, specific to the project city. The AE also factors in flights, meals, local transportation, and trip rotation structure (1-week vs. 2-week rotations).

**Review Gate:**
After the AE has completed the estimate workbook, it is reviewed by more than one team member, including Ops Director, Ops PM, and PC.

**Output:** A completed estimate workbook with all costs populated — labor by role, materials, LV Sub costs, supplier costs, panel costs, expenses/travel, and any alternate adds. This workbook feeds directly into the proposal pricing section (Step 7) via manual transfer.

---

## Step 7: Create Proposal

**RACI:** Multiple owners by section (see detail below), Prop Mgr (A for overall proposal)
**Template:** MCT Proposal Template (one template for all programs). Some repeat clients have client-specific templates (e.g., AWS and others). Client-specific templates are used when available; otherwise, the standard MCT template is used.

**Inputs:**
The completed estimate workbook from Step 6, the original RFP/RFQ package, the LV Sub SOW and selected sub details from Steps 3–4, the defined win theme from Step 1, and the standard MCT Proposal Template (or client-specific template if applicable).

**Process:**
The proposal is assembled by multiple team members according to the RACI. The Proposal Manager is Accountable for the overall document and Responsible for several key sections. The AE drafts the technical sections. The BDM writes the cover letter and corporate overview.

### Proposal Sections — Ownership and Content Source

**Cover Letter** — BDM is Responsible. Follows a standard, intentionally consistent format across all MCT proposals. Customized with the client/GC name, project name/code, system type (BMS/EPMS/both), and the names of E Tech signers. References E Tech's Zer0Defects™ Methodology and decade-plus data center experience. The AI agent can draft this from the standard template, flagging client name, project code, and signer names as "Needs Input."

**Section 1: Proposal Baseline** — Mostly templated, customized with project details.

- 1.1 Revision History — Project-specific (version, date, initials, comments).
- 1.2 Documents Provided by Client — Project-specific (list of RFP documents received, with the platform/source noted).
- 1.3 Client Contacts — Project-specific (names, titles, phone, email from the RFP).
- 1.4 E Tech Group Contacts — Project-specific (assigned E Tech team members — typically BD lead, Program Director, and Engineering Manager). Needs Input from BD/PD.

**Section 2: Executive Summary** — Standard boilerplate, sometimes lightly adapted. Per the RACI, the BDM defines the win theme (Quality, Experience, or Value Engineering) which should inform the executive summary's emphasis. E Tech has a standard executive summary that is rarely customized significantly. Includes the Strategy for Project Completion narrative and the project schedule reference. The AI agent should use the standard boilerplate and adapt emphasis based on the win theme.

**Section 3: E Tech Company Overview & Qualifications** — Boilerplate — does not change. Per the RACI, the BDM is Responsible for the Corporate Overview, with the Ops Director and Executive Consulted. In practice, this section is pulled from the standard template verbatim. It includes the company overview pages, customer testimonial, and Mission Critical Business Unit Overview (Colo Program, Hyperscale Program, OSI Program descriptions, QA/QC audit results chart, and certifications). The AI agent should pull this section directly from the template.

**Section 4: Project Scope** — Heavily customized per project. Multiple owners.

Per the RACI:
- Scope / Schedule / Quality narrative — Prop Mgr is Responsible. Covers the overall approach narrative, schedule, and quality commitments.
- Instrumentation — all exceptions — AE is Responsible. Covers the detailed instrument scope, what is included, what is excluded, and any exceptions to the standard approach.

Subsections include:
- 4.1 Scope Summary — Derived from the takeoff; lists the major deliverables.
- 4.2 Design, Documentation, and BIM Services — Lists as-built deliverables. Adjusted based on turnkey vs. labor-only.
- 4.3 Materials - Scope of Supply — Directly from the material takeoff. Delineates E Tech-procured vs. OFCI vs. LV Sub-procured.
- 4.4 PLC System Software, Execution — Derived from the specifications.
- 4.5 System Installation — Describes the LV Sub scope and E Tech's supervision role. Names the selected LV Sub partner.
- 4.6 On-Site Support & Commissioning — Describes E Tech's commissioning scope.
- 4.7 Testing (SAT/IST) — Project-specific based on RFP requirements.
- 4.8 Change Management — Standard WOCN boilerplate.
- 4.9 Safety & Security — Standard boilerplate.
- 4.10 Labor Plan Narrative — Derived from the estimate.
- 4.11 Project Schedule — Derived from the Prop Mgr's schedule (Step 6).
- 4.12 Risks/Mitigation — Standard risk library adapted with project-specific risks.

**Section 5: Clarifications, Assumptions & Exclusions** — Unique for each project. Per the RACI, the AE is Responsible, with both the BDM and Prop Mgr as Accountable and the Ops Director Consulted. This section is built from past customer and project type history and from identifying potential gaps or ambiguities in the RFP/RFQ. There is no single master list. The AI agent should draft this section using common MCT assumptions from reference proposals as a starting point, but must flag it prominently for AE review.

**Section 6: Pricing / Investment Section** — Project-specific, manually transferred from the estimate. Per the RACI, the Prop Mgr is Responsible for the Price Summary. The Ops Director is Consulted. The format depends on the customer's bid form and breakout requirements — lump sum, category subtotals, or detailed line-item breakdown. Includes:
- Payment schedule (standard: 30% at PO, 30% at project start, 30% at ship/commissioning, 10% at completion, NET 30)
- Proposal expiration (typically 60 days)
- Tax exclusion notes
- Alternate adds (e.g., warehouse, LARP, bond — per the RACI, these are the Prop Mgr's responsibility)

There may be additional bid forms provided by the customer requiring more detailed pricing breakouts. Pricing is always "Needs Input" for the AI agent.

**Attachment: Terms and Conditions of Sale** — Standard boilerplate.

**Bid Document Actions (per the RACI):**

| Action | Responsible | Accountable | Notes |
|--------|-------------|-------------|-------|
| Attachment B.1 Sales Tax | PC | BDM | Tax documentation |
| Bidding Workbook (Technical Portion) | Prop Mgr | BDM | Client-facing technical bid document |
| Bid Items — Rate Sheet, Questionnaire | Prop Mgr | BDM | Client-specific bid response forms |

**Supplemental Deliverables (if required by the RFP):**
Depending on the customer, the proposal package may also include: bid response forms, pricing spreadsheets in the client's format, certificates (insurance, safety), personnel resumes, schedule files, and other supplemental documents. The AI agent should identify these requirements when parsing the RFP and flag them as additional deliverables needed.

**Output:** A complete first-draft proposal document in Word format, plus any required bid documents and supplemental deliverables, ready for team review.

---

## Step 8: Review (Peer Review, VP Review, Executive Margin Review)

**RACI:** Multiple reviewers at each stage (see detail below)

The proposal goes through a multi-stage review process. Proposals typically go through multiple iterations, with the number of cycles depending on project size and complexity.

### Review Stage 1 — LV Sub SOW Review (occurs during Step 3)
After the AE finishes building the SOW for the Low Voltage contractor and prior to distribution for subcontractor bids, it is reviewed by an Ops Eng and Procurement Mgr.

### Review Stage 2 — Estimate Review (occurs during Step 6)
After the AE has completed the estimate workbook, it is reviewed by more than one team member, including the Ops PM, Ops Eng, Ops Director, PC. The PC is checking for estimate accuracy and formula integrity.

### Review Stage 3 — Proposal Document Review
After the AE and Prop Mgr have completed the proposal document, all relevant team members are invited to review it. The primary review responsibility falls on the BD. Reviewers look for: scope gaps, pricing errors, inconsistencies between the estimate and proposal text, missing assumptions, and formatting issues.

Review comments are handled via tracked changes and comments in the Word document, email feedback, or in-person meetings — sometimes a combination. The AE makes all revisions based on review feedback.

### Review Stage 4 — Margin Review (Two Stages per the RACI)

Once the proposal has been reviewed and approved at the team level, it is booked for margin review. This is a two-stage process:

**Stage 4A — VP Margin / Proposal Review with MCT VP:**
Per the RACI, the BDM is Responsible for this review. The Prop Mgr and AE are Consulted. The Ops Director is Consulted. The Executive is Consulted. This is the first gate before the full executive review.

**Stage 4B — Executive Margin Review with CEO/CFO/CRO/President:**
Per the RACI, the BDM is Responsible. The same Consulted parties apply. The executive team evaluates:
- Labor margin vs. material margin vs. sub margin (the detailed breakdown, not just overall margin).
- Strategic pricing considerations — whether to take a deeper cut on pricing to win a project with a strategically important client name, or to win a project where E Tech already has staff on a nearby project and the timing allows easy staff transfer.

**Margin Review Deliverable:** Prior to the review, the BDM and Prop Mgr prepare a Margin Review PowerPoint presentation (BDM and Prop Mgr are jointly Responsible, AE is Consulted). The BDM is Responsible for scheduling the executive margin review.

There is usually only one executive margin review, conducted near the end of the process.

**Output:** A reviewed, revised, and approved proposal document with executive-approved pricing, ready for submission.

---

## Step 9: Submit Proposal

**RACI:** BDM (R for submission), all others Informed

**Inputs:**
The final approved proposal document and all required supplemental deliverables.

**Process:**
Per the RACI, the BDM is Responsible for submitting the bid documents and proposal to the client. All other team members are Informed.

**Submission Format:**
The proposal is submitted primarily in PDF format, though some clients may require Word format. There are no known file size restrictions on the submission platforms. The BDM submits via the appropriate channel — online portal (Amazon Concentric, BuildingConnected, or other client-specific platforms) or email, depending on the client's requirements.

**Submission Package:**
Beyond the proposal document, the package may include (depending on customer requirements and E Tech's history with the client): bid response forms, pricing spreadsheets in the client's format, certificates (insurance, safety), personnel resumes, schedule files, and other supplemental documents.

**File Naming Convention:**
The recommended standard naming convention is: "MCT Proposal for [Client] [Project Code] [System Type]" — for example, "MCT Proposal for AWS ATL078 EPMS."

**Post-Submission:**
After the proposal is submitted, the AE remains involved. Depending on the client, the AE may: receive questions requiring clarification, attend bid leveling meetings, participate in pricing or scope negotiations, and support proposal revisions triggered by client feedback, scope changes, or competitive repositioning. Revisions follow the same revision history tracking.

**Output:** A submitted proposal with all required deliverables, delivered to the customer via their specified submission channel.

---

## AI Agent Role Summary

Based on this workflow and the RACI matrix, the AI agent's primary responsibilities and limitations are:

### What the AI Agent Produces

1. A compliance matrix mapping every RFP requirement to a proposal response status.
2. A first-draft proposal document using the correct template (standard MCT or client-specific), with sections assigned to the appropriate owners per the RACI.
3. A draft LV Sub SOW using the standard template, populated with client specification requirements.
4. A gaps/needs-input list flagging everything that requires human judgment, pricing, or project-specific decisions — organized by RACI role so the right person can address each gap.
5. A risk assessment using the standard risk library plus project-specific flags from the RFP.
6. A draft Clarifications, Assumptions & Exclusions section based on reference proposals, flagged for AE review.

### What the AI Agent Flags as "Needs Input" (organized by responsible role)

**BDM Needs Input:**
- Win theme (Quality, Experience, or Value Engineering)
- Cover letter signer names
- Client-specific relationship context
- Strategic pricing direction

**Prop Mgr Needs Input:**
- Project schedule
- Price summary and format
- Alternate adds
- Bidding workbook and bid item responses

**AE Needs Input:**
- Instrument counts and material takeoff quantities (from print review)
- Project-specific Clarifications, Assumptions & Exclusions
- Technical exceptions and instrumentation scope decisions
- Labor hour estimates by category

**Procurement Needs Input:**
- LV Sub selection and quotes
- Supplier quotes and material pricing
- Vendor RFQ responses

**Executive Needs Input:**
- Margin approval
- Strategic pricing decisions

**General — Always Needs Input:**
- All pricing (never fabricated by the AI agent)
- Labor rates (vary by project, region, and client)
- Travel rotation assumptions and per diem rates
- E Tech team member assignments and contact information
- Panel shop quotes and capacity decisions

### What the AI Agent Should Never Do

- Fabricate instrument counts, quantities, or material specifications.
- Invent pricing or margin assumptions.
- Name subcontractors without input from the team.
- Assume BMS vs. EPMS scope without confirming from the RFP.
- Copy client-specific terminology from one client's proposals to another.
- Modify the Zer0Defects™ branded language.
- Disclose specific project values or revenue figures from past projects without approval.

---

## RACI Quick Reference Table

| Deliverable | Responsible | Accountable |
|-------------|-------------|-------------|
| Win Theme | BDM | — |
| Cover Letter | BDM | — |
| Corporate Overview | BDM | — |
| Project Schedule | AE | Prop Mgr |
| Price Summary | Prop Mgr | BDM |
| Scope / Schedule / Quality Narrative | Prop Mgr | BDM |
| Bidding Workbook (Technical) | Prop Mgr | BDM |
| Bid Items — Rate Sheet, Questionnaire | Prop Mgr | BDM |
| Alternate Adds | Prop Mgr | BDM |
| Material Takeoff | AE | Prop Mgr |
| LV Sub SOW | AE + Ops Eng | Prop Mgr |
| Estimate Workbook | AE | Prop Mgr |
| Instrumentation — All Exceptions | AE | Prop Mgr |
| Exceptions / Clarifications | AE | BDM + Prop Mgr |
| RFIs | AE + Ops Eng | Prop Mgr |
| Vendor RFQs | Procurement | Prop Mgr |
| Sub Sourcing & Management | Procurement | Prop Mgr |
| Material Pricing (Instruments, Panels) | Procurement | Prop Mgr |
| Sub Selection | Procurement | Prop Mgr |
| Administrative Setup | PC | BDM |
| Bond Requirements | PC | Prop Mgr |
| Margin Review Scheduling | BDM | — |
| Margin Review PowerPoint | BDM + Prop Mgr | — |
| VP Margin Review | BDM | — |
| Executive Margin Review | BDM | — |
| Final Submission to Client | BDM | — |

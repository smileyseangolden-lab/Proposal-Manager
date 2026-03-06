# Proposal Generation Workflow — Global Fallback

This is the global fallback workflow. The Proposal Manager Agent now uses
**vertical-specific workflows** located in:

- `verticals/data_center/workflow.md` — Data Center / Mission Critical (MCT)
- `verticals/life_science/workflow.md` — Life Science & Pharmaceutical
- `verticals/food_beverage/workflow.md` — Food & Beverage / CPG
- `verticals/general/workflow.md` — General / Other

When a user uploads an RFP/RFQ, they select (or the system auto-detects) the
industry vertical, and the corresponding workflow is loaded automatically.

See `verticals/<vertical>/workflow.md` for the detailed process.

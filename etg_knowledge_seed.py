"""Seed the EtgKnowledgeAsset table with system defaults and migrate rows
from the legacy CompanyStandard table.

Called once on app startup. Idempotent: existing rows with matching
(source, title) are left alone. Adding new entries here is safe and they'll
appear on the next boot.
"""

from __future__ import annotations

from models import CompanyStandard, EtgKnowledgeAsset, db


# System-global taxonomy used by the per-document analyzer. Editing these in
# Settings overrides the seed (the user copy wins because the seed only
# inserts rows that don't already exist).
TRADE_TAXONOMY = [
    "general",
    "electrical",
    "instrumentation",
    "automation_controls",
    "process_mechanical",
    "piping_pid",
    "structural_civil",
    "hvac",
    "building_automation",
    "networking_cybersecurity",
    "validation_qualification",
    "commissioning",
    "safety",
    "project_management",
]

DOCUMENT_TYPE_TAXONOMY = [
    "rfp_narrative",
    "specification",
    "datasheet",
    "drawing",
    "sequence_of_operations",
    "io_list",
    "bill_of_materials",
    "schedule",
    "addendum",
    "contract_legal",
    "submittal",
    "vendor_proposal",
    "rfp_qa_log",
    "checklist",
    "report",
    "uncategorized",
]


# Per-vertical "what we expect to see in a complete bid package" lists.
# These drive the gaps & risks section in Phase 2 but the rows are seeded now
# so they're editable from day one.
EXPECTED_DOCUMENTS_BY_VERTICAL = {
    "life_science": [
        ("RFP Narrative / Statement of Work", True),
        ("Process & Instrumentation Diagrams (P&IDs)", True),
        ("Sequence of Operations (SOO)", True),
        ("IO Point Schedule", True),
        ("Equipment Datasheets", True),
        ("Validation Master Plan / IQ-OQ-PQ Strategy", True),
        ("Commissioning Protocol / Cx Plan", True),
        ("Cybersecurity / 21 CFR Part 11 Requirements", True),
        ("Project Schedule (Gantt or Milestone List)", False),
        ("Contract Terms & Conditions", False),
    ],
    "data_center": [
        ("RFP Narrative / Statement of Work", True),
        ("BMS / EPMS Sequence of Operations", True),
        ("Single-Line Diagrams", True),
        ("IO Point Schedule", True),
        ("Equipment Datasheets (UPS, switchgear, CRAH)", True),
        ("Network / Cybersecurity Requirements", True),
        ("Commissioning Plan / Lvl 1-5 Cx", True),
        ("Acceptance Test Procedures", False),
        ("Project Schedule", False),
        ("Contract Terms & Conditions", False),
    ],
    "food_beverage": [
        ("RFP Narrative / Statement of Work", True),
        ("Process & Instrumentation Diagrams (P&IDs)", True),
        ("Sequence of Operations (SOO)", True),
        ("IO Point Schedule", True),
        ("Equipment Datasheets", True),
        ("Sanitary / Hygienic Design Requirements", True),
        ("HACCP / Food Safety Plan References", False),
        ("Commissioning / FAT-SAT Plan", False),
        ("Project Schedule", False),
        ("Contract Terms & Conditions", False),
    ],
    "general": [
        ("RFP Narrative / Statement of Work", True),
        ("Specifications", True),
        ("Drawings", True),
        ("IO Point Schedule or Equivalent", False),
        ("Sequence of Operations (SOO)", False),
        ("Project Schedule", False),
        ("Contract Terms & Conditions", False),
    ],
}


def seed_system_assets() -> int:
    """Insert system-global rows that don't already exist. Returns inserted count."""
    inserted = 0

    # Taxonomy entries — one row per section so they show up in Settings.
    inserted += _ensure_global(
        section="taxonomy",
        title="Trades",
        content_md="\n".join(f"- {t}" for t in TRADE_TAXONOMY),
        tags="taxonomy,trades",
        source="system_seed",
    )
    inserted += _ensure_global(
        section="taxonomy",
        title="Document Types",
        content_md="\n".join(f"- {t}" for t in DOCUMENT_TYPE_TAXONOMY),
        tags="taxonomy,document_types",
        source="system_seed",
    )

    # Expected-document checklists per vertical.
    for vertical, items in EXPECTED_DOCUMENTS_BY_VERTICAL.items():
        body_lines = []
        for label, required in items:
            marker = "REQUIRED" if required else "optional"
            body_lines.append(f"- [{marker}] {label}")
        inserted += _ensure_global(
            section="expected_documents",
            title=f"Expected Documents — {vertical.replace('_', ' ').title()}",
            content_md="\n".join(body_lines),
            vertical=vertical,
            tags=f"expected_documents,{vertical}",
            source="system_seed",
        )

    if inserted:
        db.session.commit()
    return inserted


def migrate_company_standards() -> int:
    """Copy CompanyStandard rows into EtgKnowledgeAsset. Idempotent.

    Each CompanyStandard becomes a per-user knowledge asset under the
    ``company_profile`` section (the closest semantic match). The original
    rows are left untouched so the existing Settings UI keeps working until
    the next phase removes it.
    """
    inserted = 0
    standards = CompanyStandard.query.filter_by(is_active=True).all()
    for s in standards:
        exists = EtgKnowledgeAsset.query.filter_by(
            user_id=s.user_id,
            source="migrated_company_standard",
            title=s.title,
        ).first()
        if exists:
            continue
        asset = EtgKnowledgeAsset(
            user_id=s.user_id,
            section=_section_for_standard_category(s.category),
            title=s.title,
            content_md=s.content,
            tags=f"migrated,{s.category.lower().replace(' ', '_')}",
            source="migrated_company_standard",
        )
        db.session.add(asset)
        inserted += 1
    if inserted:
        db.session.commit()
    return inserted


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _ensure_global(
    *,
    section: str,
    title: str,
    content_md: str,
    vertical: str = "",
    tags: str = "",
    source: str = "system_seed",
) -> int:
    existing = EtgKnowledgeAsset.query.filter_by(
        user_id=None, section=section, title=title
    ).first()
    if existing:
        return 0
    asset = EtgKnowledgeAsset(
        user_id=None,
        section=section,
        title=title,
        content_md=content_md,
        vertical=vertical,
        tags=tags,
        source=source,
    )
    db.session.add(asset)
    return 1


_CATEGORY_TO_SECTION = {
    "Mission Statement": "company_profile",
    "Company Overview": "company_profile",
    "Certifications": "company_profile",
    "Past Performance": "past_proposal",
    "Safety Record": "company_profile",
    "Quality Standards": "company_profile",
    "Insurance": "company_profile",
    "Terms & Conditions": "sow_boilerplate",
    "Key Personnel": "company_profile",
    "Differentiators": "company_profile",
    "Other": "company_profile",
}


def _section_for_standard_category(category: str) -> str:
    return _CATEGORY_TO_SECTION.get(category, "company_profile")


def upsert_from_company_standard(standard) -> EtgKnowledgeAsset:
    """Mirror a CompanyStandard write into the knowledge base.

    Used by the legacy Settings UI so the proposal generator can read from
    EtgKnowledgeAsset alone. Returns the upserted asset.
    """
    asset = EtgKnowledgeAsset.query.filter_by(
        user_id=standard.user_id,
        source="migrated_company_standard",
        title=standard.title,
    ).first()
    section = _section_for_standard_category(standard.category)
    tags = f"migrated,{standard.category.lower().replace(' ', '_')}"
    if asset is None:
        asset = EtgKnowledgeAsset(
            user_id=standard.user_id,
            section=section,
            title=standard.title,
            content_md=standard.content,
            tags=tags,
            source="migrated_company_standard",
        )
        db.session.add(asset)
    else:
        asset.section = section
        asset.title = standard.title
        asset.content_md = standard.content
        asset.tags = tags
        asset.is_active = standard.is_active
    db.session.commit()
    return asset


def delete_from_company_standard(standard) -> None:
    """Remove the mirrored knowledge asset when its CompanyStandard is deleted."""
    asset = EtgKnowledgeAsset.query.filter_by(
        user_id=standard.user_id,
        source="migrated_company_standard",
        title=standard.title,
    ).first()
    if asset is not None:
        db.session.delete(asset)
        db.session.commit()

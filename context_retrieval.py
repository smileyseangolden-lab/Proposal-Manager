"""ETG knowledge retrieval helper.

Pulls relevant entries from the ``etg_knowledge_assets`` table for injection
into AI prompts. Phase 1 implementation is deterministic (section + vertical
+ tag filtering, deterministic ordering, fixed character budget). A future
phase can add embedding-based retrieval; the function signature is designed
to remain stable.

Sections (the ``section`` column on ``EtgKnowledgeAsset``):

- ``company_profile``: company overview, mission, history.
- ``capabilities``: capability matrix entries by vertical.
- ``tech_stack``: technology platforms with depth ratings.
- ``sow_boilerplate``: standard SOW language, versioned by vertical.
- ``exclusions``: standard exclusions / assumptions library.
- ``pricing_reference``: labor rates, hours-per-IO heuristics, markups.
- ``past_proposal``: indexed past proposals with tags.
- ``brand_asset``: logos, letterheads, brand color palettes.
- ``taxonomy``: triage taxonomy (trade list, document type list).
- ``expected_documents``: per-vertical checklist of expected doc types.
"""

from __future__ import annotations

from typing import Iterable

from models import EtgKnowledgeAsset


# Default per-call character budget. Plenty for Haiku tasks; the proposal
# generator can request a larger budget when it needs the full library.
DEFAULT_BUDGET = 8000


def retrieve_context(
    sections: Iterable[str],
    *,
    user_id: str | None = None,
    vertical: str | None = None,
    tags: Iterable[str] | None = None,
    max_chars: int = DEFAULT_BUDGET,
) -> str:
    """Return a Markdown-formatted context block ready to drop into a prompt.

    Empty string when nothing matches. Caller is responsible for prefixing
    a heading like ``## Company Context``.
    """
    sections = list(sections)
    if not sections:
        return ""

    query = EtgKnowledgeAsset.query.filter(
        EtgKnowledgeAsset.section.in_(sections),
        EtgKnowledgeAsset.is_active.is_(True),
    )

    if user_id is not None:
        # User-scoped assets plus globals (user_id null).
        query = query.filter(
            db_or(EtgKnowledgeAsset.user_id == user_id, EtgKnowledgeAsset.user_id.is_(None))
        )
    else:
        query = query.filter(EtgKnowledgeAsset.user_id.is_(None))

    rows = query.order_by(
        EtgKnowledgeAsset.section.asc(),
        EtgKnowledgeAsset.sort_order.asc(),
        EtgKnowledgeAsset.created_at.asc(),
    ).all()

    rows = _filter_by_vertical(rows, vertical)
    if tags:
        rows = _filter_by_tags(rows, list(tags))

    if not rows:
        return ""

    blocks: list[str] = []
    used = 0
    for row in rows:
        block = _format_asset(row)
        if not block:
            continue
        if used + len(block) > max_chars:
            blocks.append("\n[...additional context truncated for length...]\n")
            break
        blocks.append(block)
        used += len(block)

    return "\n".join(blocks).strip()


def list_assets(
    *,
    user_id: str | None = None,
    section: str | None = None,
) -> list[EtgKnowledgeAsset]:
    """List assets visible to a user (their own + globals), optionally one section."""
    query = EtgKnowledgeAsset.query
    if user_id is not None:
        query = query.filter(
            db_or(EtgKnowledgeAsset.user_id == user_id, EtgKnowledgeAsset.user_id.is_(None))
        )
    else:
        query = query.filter(EtgKnowledgeAsset.user_id.is_(None))
    if section:
        query = query.filter(EtgKnowledgeAsset.section == section)
    return query.order_by(
        EtgKnowledgeAsset.section.asc(),
        EtgKnowledgeAsset.sort_order.asc(),
        EtgKnowledgeAsset.title.asc(),
    ).all()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def db_or(*clauses):
    """Tiny wrapper so callers don't have to import sqlalchemy.or_."""
    from sqlalchemy import or_ as _or
    return _or(*clauses)


def _filter_by_vertical(rows, vertical):
    if not vertical:
        # Caller didn't constrain. Return everything (vertical-specific entries
        # are still useful as general context).
        return rows
    return [r for r in rows if not r.vertical or r.vertical == vertical]


def _filter_by_tags(rows, tags):
    wanted = {t.strip().lower() for t in tags if t.strip()}
    if not wanted:
        return rows
    out = []
    for r in rows:
        row_tags = {t.strip().lower() for t in (r.tags or "").split(",") if t.strip()}
        if not row_tags or row_tags & wanted:
            out.append(r)
    return out


def _format_asset(row: EtgKnowledgeAsset) -> str:
    """Render a single asset as a Markdown block."""
    section_label = (row.section or "").replace("_", " ").title() or "Knowledge"
    title = row.title or "Untitled"
    body = (row.content_md or "").strip()
    if not body:
        return ""
    header = f"### {section_label}: {title}"
    if row.vertical:
        header += f"  _(vertical: {row.vertical})_"
    return f"\n{header}\n{body}\n"

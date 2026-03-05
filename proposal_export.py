"""Export generated proposals to DOCX format."""

import re
from pathlib import Path

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt


def markdown_to_docx(markdown_text: str, output_path: str) -> str:
    """Convert a Markdown proposal to a formatted DOCX file.

    Args:
        markdown_text: The proposal in Markdown format.
        output_path: File path for the output DOCX.

    Returns:
        The output file path.
    """
    doc = docx.Document()

    # Page setup
    section = doc.sections[0]
    section.top_margin = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin = Inches(1.25)
    section.right_margin = Inches(1.25)

    # Default font
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Calibri"
    font.size = Pt(11)

    lines = markdown_text.split("\n")
    i = 0
    in_table = False
    table_rows: list[list[str]] = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Flush table if we leave a table block
        if in_table and not stripped.startswith("|"):
            _add_table(doc, table_rows)
            table_rows = []
            in_table = False

        # Headings
        if stripped.startswith("# ") and not stripped.startswith("## "):
            heading = doc.add_heading(stripped[2:], level=1)
            heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif stripped.startswith("## "):
            doc.add_heading(stripped[3:], level=2)
        elif stripped.startswith("### "):
            doc.add_heading(stripped[4:], level=3)
        elif stripped.startswith("#### "):
            doc.add_heading(stripped[5:], level=4)

        # Horizontal rule
        elif stripped in ("---", "***", "___"):
            doc.add_paragraph("").add_run().add_break()

        # Table rows
        elif stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            # Skip separator rows like |---|---|
            if all(re.match(r"^[-:]+$", c) for c in cells):
                i += 1
                continue
            table_rows.append(cells)
            in_table = True

        # Bullet points
        elif stripped.startswith("- ") or stripped.startswith("* "):
            doc.add_paragraph(stripped[2:], style="List Bullet")

        # Numbered lists
        elif re.match(r"^\d+\.\s", stripped):
            text = re.sub(r"^\d+\.\s", "", stripped)
            doc.add_paragraph(text, style="List Number")

        # Bold text as standalone line
        elif stripped.startswith("**") and stripped.endswith("**"):
            p = doc.add_paragraph()
            run = p.add_run(stripped.strip("*"))
            run.bold = True

        # Empty line
        elif not stripped:
            pass  # Skip empty lines (spacing handled by styles)

        # Regular paragraph
        else:
            p = doc.add_paragraph()
            _add_formatted_text(p, stripped)

        i += 1

    # Flush any remaining table
    if table_rows:
        _add_table(doc, table_rows)

    doc.save(output_path)
    return output_path


def _add_table(doc, rows: list[list[str]]):
    """Add a table to the document."""
    if not rows:
        return
    num_cols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=num_cols)
    table.style = "Light Grid Accent 1"

    for r_idx, row in enumerate(rows):
        for c_idx, cell_text in enumerate(row):
            if c_idx < num_cols:
                table.rows[r_idx].cells[c_idx].text = cell_text

    doc.add_paragraph()  # spacing after table


def _add_formatted_text(paragraph, text: str):
    """Add text with basic inline formatting (bold, italic) to a paragraph."""
    # Split on bold markers
    parts = re.split(r"(\*\*.*?\*\*)", text)
    for part in parts:
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("*") and part.endswith("*"):
            run = paragraph.add_run(part[1:-1])
            run.italic = True
        else:
            paragraph.add_run(part)

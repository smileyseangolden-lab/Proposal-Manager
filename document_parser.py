"""Parse uploaded RFP/RFQ documents into plain text for the agent."""

from pathlib import Path

import docx
import PyPDF2


def parse_document(file_path: str) -> str:
    """Parse a document file and return its text content.

    Supports PDF, DOCX, and plain text formats.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        return _parse_pdf(path)
    elif suffix in (".docx", ".doc"):
        return _parse_docx(path)
    elif suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8")
    else:
        raise ValueError(f"Unsupported file format: {suffix}")


def _parse_pdf(path: Path) -> str:
    """Extract text from a PDF file."""
    text_parts = []
    with open(path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n\n".join(text_parts)


def _parse_docx(path: Path) -> str:
    """Extract text from a DOCX file."""
    doc = docx.Document(str(path))
    paragraphs = []
    for para in doc.paragraphs:
        if para.text.strip():
            paragraphs.append(para.text)

    # Also extract text from tables
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text.strip() for cell in row.cells)
            if row_text.strip(" |"):
                paragraphs.append(row_text)

    return "\n\n".join(paragraphs)


def load_reference_documents(reference_dir: Path) -> dict[str, list[str]]:
    """Load all reference documents organized by category.

    Returns a dict with keys: 'sample_rfps', 'sample_rfqs', 'past_proposals'
    each mapping to a list of document text contents.
    """
    categories = {
        "sample_rfps": reference_dir / "sample_rfps",
        "sample_rfqs": reference_dir / "sample_rfqs",
        "past_proposals": reference_dir / "past_proposals",
    }
    result: dict[str, list[str]] = {}
    for category, cat_dir in categories.items():
        docs = []
        if cat_dir.exists():
            for file_path in sorted(cat_dir.iterdir()):
                if file_path.suffix.lower() in (".md", ".txt", ".pdf", ".docx"):
                    try:
                        docs.append(parse_document(str(file_path)))
                    except Exception:
                        continue
        result[category] = docs
    return result


def load_templates(templates_dir: Path) -> dict[str, str]:
    """Load all proposal templates/boilerplate.

    Returns a dict mapping template filename to its text content.
    """
    templates: dict[str, str] = {}
    if templates_dir.exists():
        for file_path in sorted(templates_dir.iterdir()):
            if file_path.suffix.lower() in (".md", ".txt"):
                templates[file_path.name] = file_path.read_text(encoding="utf-8")
    return templates

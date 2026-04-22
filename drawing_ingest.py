"""Load engineering drawings as base64-encoded images for vision-capable models.

Images (PNG/JPG/WEBP/GIF/BMP) are loaded as-is. PDFs are rasterized page-by-page
via PyMuPDF (`pymupdf`/`fitz`). The total number of images is capped so SOW
generation doesn't silently balloon token cost on a 200-page drawing pack.
"""

from __future__ import annotations

import base64
from pathlib import Path

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_PDF_SUFFIX = ".pdf"

# Cap total images sent to Claude per SOW run. Each page of a PDF drawing
# counts as one image. Override via the `limit` kwarg in callers.
DEFAULT_MAX_IMAGES = 10


def _guess_media_type(suffix: str) -> str:
    s = suffix.lower().lstrip(".")
    if s in ("jpg", "jpeg"):
        return "image/jpeg"
    if s == "webp":
        return "image/webp"
    if s == "gif":
        return "image/gif"
    if s == "bmp":
        return "image/bmp"
    return "image/png"


def _encode_file(path: Path) -> dict:
    data = path.read_bytes()
    return {
        "media_type": _guess_media_type(path.suffix),
        "data": base64.standard_b64encode(data).decode("ascii"),
        "source_name": path.name,
    }


def _rasterize_pdf(path: Path, remaining: int) -> list[dict]:
    """Render PDF pages to PNGs using PyMuPDF. Returns at most `remaining` images."""
    if remaining <= 0:
        return []
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return []

    out: list[dict] = []
    try:
        doc = fitz.open(str(path))
    except Exception:
        return []

    try:
        # 144 DPI — enough for Claude to read most drawing annotations without
        # making individual images massive.
        matrix = fitz.Matrix(144 / 72, 144 / 72)
        for i, page in enumerate(doc):
            if len(out) >= remaining:
                break
            try:
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                png_bytes = pix.tobytes("png")
                out.append({
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(png_bytes).decode("ascii"),
                    "source_name": f"{path.name} (page {i + 1})",
                })
            except Exception:
                continue
    finally:
        doc.close()
    return out


def load_drawing_images(file_paths: list[str],
                        limit: int = DEFAULT_MAX_IMAGES) -> tuple[list[dict], bool]:
    """Load a batch of drawing files as Claude-compatible image blocks.

    Returns (images, truncated) where `images` is a list of dicts shaped like:
        {"media_type": "image/png", "data": "<base64>", "source_name": "foo.pdf (page 1)"}
    and `truncated` is True when the cap was hit and more content existed.
    """
    images: list[dict] = []
    truncated = False

    for raw_path in file_paths:
        if len(images) >= limit:
            truncated = True
            break
        path = Path(raw_path)
        if not path.exists():
            continue
        suffix = path.suffix.lower()
        remaining = limit - len(images)

        if suffix in _IMAGE_SUFFIXES:
            try:
                images.append(_encode_file(path))
            except Exception:
                continue
        elif suffix == _PDF_SUFFIX:
            before = len(images)
            rendered = _rasterize_pdf(path, remaining)
            images.extend(rendered)
            # If we stopped early because of the cap but the PDF had more pages,
            # flag truncation. We don't know page count post-close without
            # reopening, so a simple proxy: if we filled to the cap, mark it.
            if len(images) >= limit and (len(images) - before) == remaining:
                truncated = True
        # Unsupported extensions are silently ignored.

    return images, truncated

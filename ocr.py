"""Optical character recognition for scanned (image-only) PDFs.

Many RFPs arrive as scans — a PDF whose "pages" are just images, so the normal
text extractor returns almost nothing. When OCR is available we render each
page to an image (via pypdfium2, a self-contained wheel — no poppler/system
renderer needed) and run Tesseract over it.

Everything here is BEST-EFFORT and degrades gracefully: if the Tesseract binary
or the Python wrappers aren't installed, `available()` is False and callers
fall back to plain text extraction. OCR only runs inside background jobs
(generation / scope / estimate), never in the request path, because it is slow
(~1-2s per page).

Results are cached to a sidecar file next to the source (``<path>.ocr.txt``)
and mirrored to object storage, so a scanned document is OCR'd once, not once
per scope-draft + generate + estimate.
"""

import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Kill switch + safety caps (env-overridable). A very long scanned PDF could
# otherwise tie a worker up for minutes; we cap pages and log truncation.
_ENABLED = os.getenv("OCR_ENABLED", "true").lower() == "true"
_MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "50"))
_DPI = int(os.getenv("OCR_DPI", "200"))

# Below this many characters, a PDF's embedded text is treated as "scanned"
# and worth an OCR pass.
SPARSE_TEXT_THRESHOLD = 100

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}


@lru_cache(maxsize=1)
def available() -> bool:
    """True only if OCR can actually run (wrappers importable + Tesseract binary
    present). Cached — the answer doesn't change during a process's life."""
    if not _ENABLED:
        return False
    try:
        import pypdfium2  # noqa: F401
        import pytesseract
    except Exception:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        logger.info("OCR wrappers present but the Tesseract binary is not runnable.")
        return False


def _sidecar(path) -> Path:
    return Path(str(path) + ".ocr.txt")


def _read_cache(path) -> str | None:
    sc = _sidecar(path)
    try:
        import storage
        storage.ensure_local(sc)
    except Exception:
        pass
    if sc.exists():
        try:
            return sc.read_text(encoding="utf-8")
        except Exception:
            return None
    return None


def _write_cache(path, text: str):
    sc = _sidecar(path)
    try:
        sc.write_text(text, encoding="utf-8")
        import storage
        storage.sync_up(sc)
    except Exception:
        logger.warning("Could not cache OCR output for %s", path, exc_info=True)


def _ocr_pil(image) -> str:
    import pytesseract
    try:
        return pytesseract.image_to_string(image) or ""
    except Exception:
        logger.warning("Tesseract failed on a page", exc_info=True)
        return ""


def image_text(path) -> str:
    """OCR a single image file. Returns '' on any failure."""
    if not available():
        return ""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return _ocr_pil(img).strip()
    except Exception:
        logger.warning("OCR of image %s failed", path, exc_info=True)
        return ""


def pdf_text(path, max_pages: int = None) -> str:
    """OCR a scanned PDF page by page. Cached to a sidecar. Returns '' if OCR is
    unavailable or nothing could be recognized."""
    if not available():
        return ""
    cached = _read_cache(path)
    if cached is not None:
        return cached

    max_pages = max_pages or _MAX_PAGES
    text = ""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        try:
            n = len(pdf)
            scale = _DPI / 72.0
            parts = []
            for i in range(min(n, max_pages)):
                page = pdf[i]
                try:
                    bitmap = page.render(scale=scale)
                    pil = bitmap.to_pil()
                    parts.append(_ocr_pil(pil))
                finally:
                    page.close()
            if n > max_pages:
                logger.warning("OCR truncated %s at %d of %d pages", path, max_pages, n)
                parts.append(f"\n[OCR truncated after {max_pages} of {n} pages]")
            text = "\n\n".join(p for p in parts if p).strip()
        finally:
            pdf.close()
    except Exception:
        logger.warning("OCR of PDF %s failed", path, exc_info=True)
        return ""

    # Cache even an empty result would re-trigger OCR forever; only cache hits.
    if text:
        _write_cache(path, text)
    return text


def is_image(path) -> bool:
    return Path(str(path)).suffix.lower() in _IMAGE_SUFFIXES

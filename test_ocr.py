"""Tests for scanned-PDF OCR.

Covers: availability probe, real end-to-end recognition (when Tesseract is
installed), the sidecar cache, graceful degradation when OCR is unavailable,
parse_document routing (ocr flag, image files), and the sparse-text threshold
that decides when a PDF is treated as a scan.

Standalone runner: python test_ocr.py
"""
import os
import sys
import tempfile
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

import document_parser
import ocr

passed = failed = skipped = 0


def test(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS: {name}")
    else:
        failed += 1; print(f"  FAIL: {name} - {detail}")


def skip(name, why):
    global skipped
    skipped += 1
    print(f"  SKIP: {name} - {why}")


def make_scanned_pdf(text, path):
    """A PDF whose single page is an IMAGE of text — i.e. a scan with no
    embedded text layer."""
    img = Image.new("RGB", (1600, 500), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 60)
    except Exception:
        font = ImageFont.load_default()
    draw.text((60, 200), text, fill="black", font=font)
    img.save(path, "PDF", resolution=200.0)


tmp = tempfile.mkdtemp()

print("\n=== Availability ===")
test("available() returns a bool without raising", isinstance(ocr.available(), bool))
print(f"  (OCR available in this environment: {ocr.available()})")

print("\n=== Sparse-text detection routing ===")
scan_pdf = os.path.join(tmp, "scan.pdf")
make_scanned_pdf("OCRABLE SCOPE TOKEN 12345", scan_pdf)

# Fast pass (no OCR) sees essentially nothing — it's an image.
fast = document_parser.parse_document(scan_pdf)
test("scanned PDF yields little/no text without OCR", len(fast.strip()) < 100,
     f"got {len(fast.strip())} chars")

print("\n=== Graceful degradation (OCR unavailable) ===")
with patch.object(ocr, "available", return_value=False):
    degraded = document_parser.parse_document(scan_pdf, ocr=True)
    test("parse_document(ocr=True) never crashes when OCR is off",
         isinstance(degraded, str))
    test("image file returns '' when OCR is off",
         ocr.image_text(scan_pdf) == "")

print("\n=== Real end-to-end OCR ===")
if ocr.available():
    # Clear the sidecar cache for a clean run
    sc = ocr.__dict__["_sidecar"](scan_pdf)
    if sc.exists():
        sc.unlink()

    recovered = document_parser.parse_document(scan_pdf, ocr=True)
    test("OCR recovers text from a scanned PDF",
         "SCOPE" in recovered.upper() and "TOKEN" in recovered.upper(),
         f"got: {recovered!r}")
    test("OCR output is much richer than the fast pass",
         len(recovered.strip()) > len(fast.strip()))

    test("sidecar cache written", sc.exists())
    # Second call must hit the cache, not re-render/re-OCR.
    with patch("pypdfium2.PdfDocument", side_effect=AssertionError("should not re-render")):
        cached = ocr.pdf_text(scan_pdf)
        test("second OCR call served from cache (no re-render)",
             "SCOPE" in cached.upper())

    # Image-file OCR path
    png = os.path.join(tmp, "note.png")
    img = Image.new("RGB", (900, 240), "white")
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 56)
    except Exception:
        f = ImageFont.load_default()
    d.text((40, 90), "IMAGE OCR WORKS", fill="black", font=f)
    img.save(png)
    img_text = document_parser.parse_document(png, ocr=True)
    test("image files OCR through parse_document",
         "IMAGE" in img_text.upper() and "OCR" in img_text.upper(),
         f"got: {img_text!r}")
else:
    skip("real OCR recognition", "Tesseract binary not installed")
    skip("sidecar cache", "Tesseract binary not installed")
    skip("image OCR", "Tesseract binary not installed")

print("\n=== Non-OCR formats unaffected ===")
txt = os.path.join(tmp, "plain.txt")
with open(txt, "w") as fh:
    fh.write("plain text stays plain")
test("txt files ignore the ocr flag",
     document_parser.parse_document(txt, ocr=True) == "plain text stays plain")

print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed, {skipped} skipped "
      f"out of {passed + failed + skipped} checks")
sys.exit(0 if failed == 0 else 1)

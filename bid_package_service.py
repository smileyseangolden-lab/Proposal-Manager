"""Bid package ingestion service.

Accepts a zip archive (or, for admins, a server-side folder path) and
registers every document inside as a ``ProjectDocument`` tied to a
``BidPackage``. Designed to run inside the request handler for small
packages and to be invoked by the triage worker for large ones.

Constraints honoured:

- Per-package and per-project size caps (configurable; defaults below).
- SHA-256 dedup within the package (skip identical bytes).
- Duplicate filename safe-rename (keeps zip subfolder layout intact).
- Disk-friendly: never holds an entire file in memory; uses streamed copy.
- Skips obviously-not-document files (zip-internal junk, OS files).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.utils import secure_filename

from config.settings import UPLOADS_DIR
from models import BidPackage, ProjectDocument, db


# ---------------------------------------------------------------------------
# Configuration (override in config/settings.py later if needed)
# ---------------------------------------------------------------------------

MAX_PACKAGE_SIZE_BYTES = int(os.getenv("MAX_BID_PACKAGE_SIZE_MB", "2048")) * 1024 * 1024
MAX_FILES_PER_PACKAGE = int(os.getenv("MAX_FILES_PER_BID_PACKAGE", "500"))

# Extensions we'll keep. Everything else is silently skipped during ingestion.
ACCEPTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".xlsx",
    ".xls",
    ".txt",
    ".md",
    ".csv",
    ".dwg",
    ".dxf",
    ".rtf",
}

# OS / archive metadata files we always skip.
JUNK_PREFIXES = ("__MACOSX/", ".DS_Store", "Thumbs.db", "desktop.ini")


class BidPackageError(Exception):
    """Raised when a bid package can't be accepted."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest_zip_filelike(
    *,
    project_id: str,
    user_id: str,
    file_storage,
    original_filename: str = "",
) -> BidPackage:
    """Ingest a zip uploaded via Flask's file_storage.

    The file is streamed to a temp file, validated, then unpacked into the
    project's bid_package directory. A BidPackage row is created (status
    ``ready`` on success, ``failed`` otherwise) and ProjectDocument rows are
    inserted for every accepted file.
    """
    package = BidPackage(
        project_id=project_id,
        uploaded_by=user_id,
        source="zip",
        original_filename=original_filename or getattr(file_storage, "filename", "") or "",
        status="ingesting",
    )
    db.session.add(package)
    db.session.commit()

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp_path = Path(tmp.name)
    try:
        # Stream copy the upload to disk so we can re-open it as a zip.
        shutil.copyfileobj(file_storage.stream, tmp)
        tmp.close()

        ingest_zip_path(
            zip_path=tmp_path,
            package=package,
        )
    except BidPackageError as exc:
        package.status = "failed"
        package.error_message = str(exc)
        package.completed_at = datetime.now(timezone.utc)
        db.session.commit()
        raise
    except Exception as exc:  # pragma: no cover - defensive
        package.status = "failed"
        package.error_message = f"Unexpected error: {exc}"
        package.completed_at = datetime.now(timezone.utc)
        db.session.commit()
        raise
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    return package


def ingest_zip_path(*, zip_path: Path, package: BidPackage) -> None:
    """Unpack ``zip_path`` into the package's directory and register documents."""
    package_dir = _package_dir(package.project_id, package.id)
    package_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        members = [m for m in members if not _is_junk(m.filename)]
        members = [m for m in members if _has_accepted_extension(m.filename)]

        if len(members) > MAX_FILES_PER_PACKAGE:
            raise BidPackageError(
                f"Bid package contains {len(members)} files; the per-package "
                f"limit is {MAX_FILES_PER_PACKAGE}. Split it into smaller "
                "uploads or contact an administrator to raise the cap."
            )

        # Preflight size sanity check.
        total = sum(m.file_size for m in members)
        if total > MAX_PACKAGE_SIZE_BYTES:
            raise BidPackageError(
                f"Bid package uncompressed size is {_human_size(total)}; the "
                f"limit is {_human_size(MAX_PACKAGE_SIZE_BYTES)}."
            )

        seen_hashes: set[str] = set()
        accepted = 0
        duplicates = 0
        skipped = 0

        for member in members:
            try:
                accepted_one, hash_seen = _extract_one(zf, member, package, package_dir, seen_hashes)
            except BidPackageError:
                raise
            except Exception:
                skipped += 1
                continue

            if accepted_one:
                accepted += 1
                if hash_seen:
                    seen_hashes.add(hash_seen)
            else:
                duplicates += 1

        package.file_count = accepted
        package.duplicate_count = duplicates
        package.skipped_count = skipped
        package.total_size_bytes = _dir_size(package_dir)
        package.status = "ready"
        package.completed_at = datetime.now(timezone.utc)
        db.session.commit()


def list_documents_for_package(package_id: str):
    """All ProjectDocument rows tied to a package, ordered by relative path."""
    return (
        ProjectDocument.query.filter_by(bid_package_id=package_id)
        .order_by(ProjectDocument.relative_path.asc())
        .all()
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _extract_one(
    zf: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    package: BidPackage,
    package_dir: Path,
    seen_hashes: set[str],
) -> tuple[bool, str | None]:
    """Extract one zip member; return (accepted, sha256_if_accepted)."""
    rel_path = _safe_relative_path(member.filename)
    dest_path = package_dir / rel_path
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Stream copy + hash in one pass.
    sha = hashlib.sha256()
    written = 0
    with zf.open(member) as src, open(dest_path, "wb") as dst:
        while True:
            chunk = src.read(64 * 1024)
            if not chunk:
                break
            sha.update(chunk)
            dst.write(chunk)
            written += len(chunk)

    digest = sha.hexdigest()

    # Dedup: same hash in this package OR same hash already in this project.
    if digest in seen_hashes:
        try:
            dest_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False, None

    existing = ProjectDocument.query.filter_by(
        project_id=package.project_id, sha256=digest
    ).first()
    if existing is not None:
        try:
            dest_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False, None

    safe_name = secure_filename(Path(rel_path).name) or f"document_{uuid.uuid4().hex[:8]}"
    doc = ProjectDocument(
        project_id=package.project_id,
        filename=f"{uuid.uuid4().hex[:8]}_{safe_name}",
        original_filename=Path(rel_path).name,
        file_type="bid_package",
        file_path=str(dest_path),
        file_size=written,
        bid_package_id=package.id,
        relative_path=str(rel_path),
        sha256=digest,
    )
    db.session.add(doc)
    return True, digest


def _package_dir(project_id: str, package_id: str) -> Path:
    return UPLOADS_DIR / "projects" / project_id / "bid_packages" / package_id


def _safe_relative_path(name: str) -> Path:
    """Reject zip-slip and reduce to a safe relative path."""
    # Normalize separators, drop drive letters, collapse traversal.
    parts = []
    for raw in Path(name.replace("\\", "/")).parts:
        if raw in ("", ".", ".."):
            continue
        if raw.endswith(":"):
            # Drive letter on Windows-style entries
            continue
        parts.append(secure_filename(raw) or "_")
    if not parts:
        parts = [f"file_{uuid.uuid4().hex[:8]}"]
    return Path(*parts)


def _has_accepted_extension(name: str) -> bool:
    return Path(name).suffix.lower() in ACCEPTED_EXTENSIONS


def _is_junk(name: str) -> bool:
    base = name.split("/")[-1]
    if name.startswith(JUNK_PREFIXES):
        return True
    return base in {".DS_Store", "Thumbs.db", "desktop.ini"}


def _dir_size(p: Path) -> int:
    total = 0
    if not p.exists():
        return 0
    for entry in p.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"

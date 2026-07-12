"""Object storage abstraction.

The app works against local paths (as before). When S3-compatible storage is
configured (S3_BUCKET set), files are mirrored to the bucket after writes and
re-fetched on reads if the local copy is missing — so uploads and generated
proposals survive ephemeral disks and multi-instance deployments.

Usage:
    storage.sync_up(path)      # after writing a local file
    storage.ensure_local(path) # before reading a local file
    storage.delete(path)       # when deleting a file
All three are no-ops when S3 is not configured.
"""

import logging
from pathlib import Path

from config.settings import (
    BASE_DIR,
    S3_ACCESS_KEY_ID,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_REGION,
    S3_SECRET_ACCESS_KEY,
)

logger = logging.getLogger(__name__)

_client = None


def enabled() -> bool:
    return bool(S3_BUCKET)


def _get_client():
    global _client
    if _client is None:
        import boto3  # lazy import — only needed when S3 is configured

        kwargs = {}
        if S3_REGION:
            kwargs["region_name"] = S3_REGION
        if S3_ENDPOINT_URL:
            kwargs["endpoint_url"] = S3_ENDPOINT_URL
        if S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY:
            kwargs["aws_access_key_id"] = S3_ACCESS_KEY_ID
            kwargs["aws_secret_access_key"] = S3_SECRET_ACCESS_KEY
        _client = boto3.client("s3", **kwargs)
    return _client


def _key_for(path) -> str:
    """Bucket key = path relative to the app root (stable across containers)."""
    p = Path(path).resolve()
    try:
        return p.relative_to(BASE_DIR).as_posix()
    except ValueError:
        return p.name


def sync_up(path) -> None:
    """Mirror a local file to the bucket. No-op when S3 is unconfigured."""
    if not enabled():
        return
    p = Path(path)
    if not p.exists():
        return
    try:
        _get_client().upload_file(str(p), S3_BUCKET, _key_for(p))
    except Exception:
        logger.exception("S3 upload failed for %s", path)


def ensure_local(path) -> bool:
    """Make sure a file exists locally, downloading from the bucket if needed.
    Returns True if the file exists locally afterwards."""
    p = Path(path)
    if p.exists():
        return True
    if not enabled():
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _get_client().download_file(S3_BUCKET, _key_for(p), str(p))
        return p.exists()
    except Exception:
        logger.exception("S3 download failed for %s", path)
        return False


def delete(path) -> None:
    """Delete a file locally and from the bucket."""
    p = Path(path)
    try:
        if p.exists() and p.is_file():
            p.unlink()
    except Exception:
        pass
    if not enabled():
        return
    try:
        _get_client().delete_object(Bucket=S3_BUCKET, Key=_key_for(p))
    except Exception:
        logger.exception("S3 delete failed for %s", path)

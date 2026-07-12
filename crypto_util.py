"""Symmetric encryption for secrets at rest (user API keys).

Uses Fernet (AES-128-CBC + HMAC). The key is derived from APP_ENCRYPTION_KEY
or, as a fallback, FLASK_SECRET_KEY so the app still runs in dev without extra
config. Ciphertext is stored with an "enc:v1:" prefix so we can distinguish it
from legacy plaintext values and migrate transparently on read.

Rotation: set APP_ENCRYPTION_KEY to the new key and put the previous key(s) in
APP_ENCRYPTION_KEY_OLD (comma-separated). Decryption tries the current key first
then each old key, so rotating the key no longer silently destroys stored
secrets — values re-encrypt under the current key the next time they are saved.
"""

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"


def _fernet_for(secret: str) -> Fernet:
    # Derive a stable 32-byte urlsafe key from whatever secret we have.
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _primary_secret() -> str:
    return os.getenv("APP_ENCRYPTION_KEY") or os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")


def _fernet() -> Fernet:
    return _fernet_for(_primary_secret())


def _decrypt_fernets() -> list[Fernet]:
    """Current key first, then any rotated-out keys from APP_ENCRYPTION_KEY_OLD."""
    secrets_list = [_primary_secret()]
    old = os.getenv("APP_ENCRYPTION_KEY_OLD", "")
    secrets_list += [s.strip() for s in old.split(",") if s.strip()]
    return [_fernet_for(s) for s in secrets_list]


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def decrypt(stored: str) -> str:
    """Decrypt a stored value. Legacy plaintext (no prefix) is returned as-is
    so pre-encryption keys keep working until the user re-saves them.

    Tries the current key then any rotated-out keys. If none work, logs a
    warning (rather than silently returning "") so a misconfigured/rotated key
    is diagnosable instead of quietly wiping every stored secret."""
    if not stored:
        return ""
    if not stored.startswith(_PREFIX):
        return stored  # legacy plaintext
    token = stored[len(_PREFIX):].encode("utf-8")
    for f in _decrypt_fernets():
        try:
            return f.decrypt(token).decode("utf-8")
        except (InvalidToken, ValueError):
            continue
    logger.warning(
        "Failed to decrypt a stored secret with any configured key. If you "
        "rotated APP_ENCRYPTION_KEY, set the previous key in APP_ENCRYPTION_KEY_OLD."
    )
    return ""


def is_encrypted(stored: str) -> bool:
    return bool(stored) and stored.startswith(_PREFIX)

"""Symmetric encryption for secrets at rest (user API keys).

Uses Fernet (AES-128-CBC + HMAC). The key is derived from APP_ENCRYPTION_KEY
or, as a fallback, FLASK_SECRET_KEY so the app still runs in dev without extra
config. Ciphertext is stored with an "enc:v1:" prefix so we can distinguish it
from legacy plaintext values and migrate transparently on read.
"""

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    secret = os.getenv("APP_ENCRYPTION_KEY") or os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
    # Derive a stable 32-byte urlsafe key from whatever secret we have
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def decrypt(stored: str) -> str:
    """Decrypt a stored value. Legacy plaintext (no prefix) is returned as-is
    so pre-encryption keys keep working until the user re-saves them."""
    if not stored:
        return ""
    if not stored.startswith(_PREFIX):
        return stored  # legacy plaintext
    try:
        return _fernet().decrypt(stored[len(_PREFIX):].encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def is_encrypted(stored: str) -> bool:
    return bool(stored) and stored.startswith(_PREFIX)

"""Time-based one-time-password (TOTP) two-factor authentication.

Standard RFC-6238 TOTP compatible with Google Authenticator, Authy, 1Password,
etc. The per-user secret is stored ENCRYPTED at rest (crypto_util); backup
codes are stored HASHED (like password-reset tokens) and are single-use.

Pure-Python (pyotp + qrcode) — no system dependency.
"""

import base64
import hashlib
import io
import json
import secrets

import pyotp

# How many 30s steps on either side of "now" are accepted — tolerates a little
# clock skew between the phone and the server without meaningfully widening the
# brute-force window.
_VALID_WINDOW = 1
_BACKUP_CODE_COUNT = 10


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account: str, issuer: str = "Proposal Manager") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


def qr_data_uri(otpauth_uri: str) -> str:
    """A PNG data: URI of the otpauth QR code, safe to inline in an <img>."""
    import qrcode
    img = qrcode.make(otpauth_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def verify_code(secret: str, code: str) -> bool:
    """True if `code` is a currently-valid TOTP for `secret`."""
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit():
        return False
    try:
        return pyotp.TOTP(secret).verify(code, valid_window=_VALID_WINDOW)
    except Exception:
        return False


# --- Backup codes ----------------------------------------------------------

def _normalize(code: str) -> str:
    return (code or "").strip().lower().replace("-", "").replace(" ", "")


def hash_backup_code(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode("utf-8")).hexdigest()


def generate_backup_codes(n: int = _BACKUP_CODE_COUNT) -> tuple[list[str], str]:
    """Return (plaintext_codes, json_of_hashes). The plaintext is shown to the
    user ONCE; only the hashes are persisted."""
    plaintext = []
    for _ in range(n):
        raw = secrets.token_hex(4)  # 8 hex chars
        plaintext.append(f"{raw[:4]}-{raw[4:]}")
    hashes = [hash_backup_code(c) for c in plaintext]
    return plaintext, json.dumps(hashes)


def consume_backup_code(stored_json: str, code: str) -> tuple[bool, str]:
    """If `code` matches an unused backup hash, return (True, new_json) with
    that hash removed (single-use). Otherwise (False, unchanged_json)."""
    try:
        hashes = json.loads(stored_json or "[]")
    except (ValueError, TypeError):
        hashes = []
    h = hash_backup_code(code)
    if h in hashes:
        hashes.remove(h)
        return True, json.dumps(hashes)
    return False, stored_json or "[]"


def backup_codes_remaining(stored_json: str) -> int:
    try:
        return len(json.loads(stored_json or "[]"))
    except (ValueError, TypeError):
        return 0

"""Platform-wide runtime configuration.

Settings the platform owner edits from /platform-admin (LLM model + API key,
payment/Stripe keys, email/SMTP) are stored in the PlatformSetting table and
layered OVER the corresponding environment variable: a DB value wins, otherwise
the env default applies. Secret values (is_secret) are encrypted at rest via
crypto_util, so keys are never stored or displayed in plaintext.
"""

import os

import crypto_util

# key -> (env-var fallback, is_secret, label, category)
SETTINGS = {
    # LLM
    "llm_model":              ("CLAUDE_MODEL", False, "Default model", "llm"),
    "anthropic_api_key":      ("ANTHROPIC_API_KEY", True, "Anthropic API key", "llm"),
    # Payments (Stripe)
    "stripe_secret_key":      ("STRIPE_SECRET_KEY", True, "Stripe secret key", "payment"),
    "stripe_publishable_key": ("STRIPE_PUBLISHABLE_KEY", False, "Stripe publishable key", "payment"),
    "stripe_webhook_secret":  ("STRIPE_WEBHOOK_SECRET", True, "Stripe webhook signing secret", "payment"),
    "stripe_price_pro":       ("STRIPE_PRICE_PRO", False, "Stripe price id — Pro", "payment"),
    "stripe_price_business":  ("STRIPE_PRICE_BUSINESS", False, "Stripe price id — Business", "payment"),
    # Email (SMTP)
    "smtp_host":              ("SMTP_HOST", False, "SMTP host", "email"),
    "smtp_port":              ("SMTP_PORT", False, "SMTP port", "email"),
    "smtp_user":              ("SMTP_USER", False, "SMTP username", "email"),
    "smtp_password":          ("SMTP_PASSWORD", True, "SMTP password", "email"),
    "smtp_use_tls":           ("SMTP_USE_TLS", False, "Use TLS (true/false)", "email"),
    "mail_from":              ("MAIL_FROM", False, "From address", "email"),
    "mail_from_name":         ("MAIL_FROM_NAME", False, "From name", "email"),
    # SSO (OIDC)
    "oidc_client_id":         ("OIDC_CLIENT_ID", False, "OIDC client ID", "sso"),
    "oidc_client_secret":     ("OIDC_CLIENT_SECRET", True, "OIDC client secret", "sso"),
    "oidc_auth_url":          ("OIDC_AUTH_URL", False, "OIDC authorization URL", "sso"),
    "oidc_token_url":         ("OIDC_TOKEN_URL", False, "OIDC token URL", "sso"),
    "oidc_userinfo_url":      ("OIDC_USERINFO_URL", False, "OIDC userinfo URL", "sso"),
    "oidc_scopes":            ("OIDC_SCOPES", False, "OIDC scopes", "sso"),
    "oidc_button_label":      ("OIDC_BUTTON_LABEL", False, "SSO button label", "sso"),
}


def is_secret(key: str) -> bool:
    spec = SETTINGS.get(key)
    return bool(spec and spec[1])


def get(key: str, default: str = "") -> str:
    """DB override if set, else the env-var fallback, else `default`."""
    spec = SETTINGS.get(key)
    env_var, secret = (spec[0], spec[1]) if spec else (None, False)
    try:
        from models import PlatformSetting
        row = PlatformSetting.query.filter_by(key=key).first()
        if row and (row.value or "") != "":
            return crypto_util.decrypt(row.value) if secret else row.value
    except Exception:
        pass
    if env_var:
        v = os.getenv(env_var, "")
        if v != "":
            return v
    return default


def set_value(key: str, value: str, updated_by: str = None) -> None:
    """Persist a setting. Secrets are encrypted; empty value clears the override
    (falling back to the env default)."""
    from models import PlatformSetting, db
    secret = is_secret(key)
    row = PlatformSetting.query.filter_by(key=key).first()
    if row is None:
        row = PlatformSetting(key=key, is_secret=secret)
        db.session.add(row)
    value = value or ""
    row.value = crypto_util.encrypt(value) if (secret and value) else value
    row.is_secret = secret
    row.updated_by = updated_by
    db.session.commit()


def masked(key: str) -> str:
    """A display-safe representation: '••••set' if a secret is configured, the
    plain value otherwise, or '' if unset."""
    spec = SETTINGS.get(key)
    secret = spec[1] if spec else False
    val = get(key)
    if not val:
        return ""
    return "•••• set" if secret else val

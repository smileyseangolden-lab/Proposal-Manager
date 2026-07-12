"""Transactional email delivery.

Supports SMTP (set SMTP_HOST etc.) and a console fallback that logs the message
(used in dev/test). Every call returns True only when the message was actually
handed off to a transport, so callers can surface a shareable link when email
isn't configured.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
MAIL_FROM = os.getenv("MAIL_FROM", os.getenv("SMTP_USER", "no-reply@proposalmanager.app"))
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Proposal Manager")


def _cfg(key: str, fallback):
    """Effective SMTP setting: platform-admin override (DB) else env fallback."""
    try:
        import platform_config
        v = platform_config.get(key, "")
        return v if v != "" else fallback
    except Exception:
        return fallback


def configured() -> bool:
    return bool(_cfg("smtp_host", SMTP_HOST))


def send_email(to: str, subject: str, body: str, html: str = "", attachments: list = None) -> bool:
    """Send an email. Returns True if it was dispatched via SMTP.
    When SMTP is not configured, logs to the console and returns False so the
    caller can fall back to showing a link in the UI.

    attachments: optional list of local file paths to attach."""
    if not to:
        return False

    if not configured():
        # SMTP is not configured. Do NOT log the body — it may contain a
        # password-reset token or other secrets/PII. Log only a redacted notice.
        logger.info("[mailer:console] email suppressed (SMTP not configured) | subject: %s", subject)
        return False

    host = _cfg("smtp_host", SMTP_HOST)
    port = int(_cfg("smtp_port", SMTP_PORT) or SMTP_PORT)
    user = _cfg("smtp_user", SMTP_USER)
    password = _cfg("smtp_password", SMTP_PASSWORD)
    use_tls = str(_cfg("smtp_use_tls", SMTP_USE_TLS)).lower() in ("true", "1", "yes")
    mail_from = _cfg("mail_from", MAIL_FROM) or user or MAIL_FROM
    mail_from_name = _cfg("mail_from_name", MAIL_FROM_NAME)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{mail_from_name} <{mail_from}>"
    msg["To"] = to
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    for path in (attachments or []):
        try:
            import os
            with open(path, "rb") as fh:
                data = fh.read()
            fname = os.path.basename(path)
            maintype, subtype = ("application", "octet-stream")
            if fname.lower().endswith(".pdf"):
                maintype, subtype = ("application", "pdf")
            elif fname.lower().endswith(".docx"):
                maintype, subtype = ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)
        except Exception:
            logger.exception("Failed to attach %s", path)

    try:
        if use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.starttls(context=context)
                if user:
                    server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                if user:
                    server.login(user, password)
                server.send_message(msg)
        return True
    except Exception:
        logger.exception("SMTP send failed to %s", to)
        return False

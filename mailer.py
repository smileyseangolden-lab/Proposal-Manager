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


def configured() -> bool:
    return bool(SMTP_HOST)


def send_email(to: str, subject: str, body: str, html: str = "", attachments: list = None) -> bool:
    """Send an email. Returns True if it was dispatched via SMTP.
    When SMTP is not configured, logs to the console and returns False so the
    caller can fall back to showing a link in the UI.

    attachments: optional list of local file paths to attach."""
    if not to:
        return False

    if not configured():
        logger.info("[mailer:console] To: %s | Subject: %s\n%s", to, subject, body)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{MAIL_FROM_NAME} <{MAIL_FROM}>"
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
        if SMTP_USE_TLS:
            context = ssl.create_default_context()
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.starttls(context=context)
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
        return True
    except Exception:
        logger.exception("SMTP send failed to %s", to)
        return False

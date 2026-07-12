"""Outbound integrations: Slack incoming webhooks and a generic JSON webhook.

Best-effort and non-blocking-by-design: failures are swallowed so a broken
integration never breaks a user action. Uses urllib to avoid adding a
dependency. Outbound HTTPS honors the environment's proxy settings.
"""

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

_TIMEOUT = 6


def _post_json(url: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=_TIMEOUT).read()


def notify_slack(webhook_url: str, text: str):
    if not webhook_url:
        return
    try:
        _post_json(webhook_url, {"text": text})
    except Exception:
        logger.warning("Slack webhook failed", exc_info=True)


def notify_webhook(webhook_url: str, event: str, payload: dict):
    if not webhook_url:
        return
    try:
        _post_json(webhook_url, {"event": event, "data": payload})
    except Exception:
        logger.warning("Outbound webhook failed", exc_info=True)

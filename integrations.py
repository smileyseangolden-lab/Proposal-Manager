"""Outbound integrations: Slack incoming webhooks and a generic JSON webhook.

Best-effort and non-blocking-by-design: failures are swallowed so a broken
integration never breaks a user action. Uses urllib to avoid adding a
dependency. Outbound HTTPS honors the environment's proxy settings.

Security: the webhook URL is tenant-controlled, so every request is validated
to point at a PUBLIC host (no private/loopback/link-local/metadata addresses)
and redirects are disabled — otherwise a tenant could SSRF the server into its
own internal network or the cloud metadata endpoint.
"""

import ipaddress
import json
import logging
import socket
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_TIMEOUT = 6
_ALLOWED_SCHEMES = {"http", "https"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects (a redirect could point at an internal host,
    bypassing the up-front URL validation)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _host_is_public(host: str) -> bool:
    """True only if every resolved address for host is a routable public IP."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip.split("%")[0])  # strip IPv6 zone id
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


def is_safe_webhook_url(url: str, require_https: bool = False,
                        host_suffix: str | None = None) -> bool:
    """Validate a tenant-supplied webhook URL before the server calls it."""
    if not url:
        return False
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    if p.scheme not in _ALLOWED_SCHEMES:
        return False
    if require_https and p.scheme != "https":
        return False
    host = p.hostname
    if not host:
        return False
    if host_suffix and not (host == host_suffix or host.endswith("." + host_suffix)):
        return False
    return _host_is_public(host)


def _post_json(url: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    _opener.open(req, timeout=_TIMEOUT).read()


def notify_slack(webhook_url: str, text: str):
    if not webhook_url:
        return
    # Slack incoming webhooks are always https://hooks.slack.com/...
    if not is_safe_webhook_url(webhook_url, require_https=True, host_suffix="slack.com"):
        logger.warning("Refusing Slack webhook to non-Slack/unsafe URL")
        return
    try:
        _post_json(webhook_url, {"text": text})
    except Exception:
        logger.warning("Slack webhook failed", exc_info=True)


def notify_webhook(webhook_url: str, event: str, payload: dict):
    if not webhook_url:
        return
    if not is_safe_webhook_url(webhook_url):
        logger.warning("Refusing outbound webhook to unsafe/internal URL")
        return
    try:
        _post_json(webhook_url, {"event": event, "data": payload})
    except Exception:
        logger.warning("Outbound webhook failed", exc_info=True)

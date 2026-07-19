"""OIDC single sign-on (authorization-code flow).

Provider-agnostic OpenID Connect: works with Google, Microsoft/Entra, Okta,
Auth0, etc. The operator configures the endpoints + client credentials in
Platform-Admin (or env). A workspace admin then "claims" an email domain
(Organization.sso_domain) so users at that domain sign in via the IdP; with
JIT provisioning on, first-time users are auto-added to that workspace.

Security model:
  - `state` (random, session-stored) defends the callback against CSRF.
  - We require `email_verified == true` from the IdP before trusting an email.
  - Users are matched to accounts by verified email; a deactivated account is
    refused; an unknown email only creates an account when its domain is
    claimed by an org with JIT enabled — SSO never silently mints a brand-new
    workspace.
  - All IdP URLs must be https.

We use the access token against the userinfo endpoint rather than validating an
id_token JWT ourselves: the userinfo response is trusted because it's fetched
directly from the IdP over TLS with a freshly-issued access token.
"""

import json
import logging
import urllib.parse
import urllib.request
from collections import namedtuple

logger = logging.getLogger(__name__)

_TIMEOUT = 10

Resolution = namedtuple("Resolution", ["status", "user", "org"])
# status ∈ {"ok", "jit", "unverified", "deactivated", "no_account", "error"}


def _cfg(key: str, default: str = "") -> str:
    try:
        import platform_config
        return platform_config.get(key, default)
    except Exception:
        return default


def client_id() -> str: return _cfg("oidc_client_id")
def client_secret() -> str: return _cfg("oidc_client_secret")
def auth_url() -> str: return _cfg("oidc_auth_url")
def token_url() -> str: return _cfg("oidc_token_url")
def userinfo_url() -> str: return _cfg("oidc_userinfo_url")
def scopes() -> str: return _cfg("oidc_scopes", "openid email profile")
def button_label() -> str: return _cfg("oidc_button_label", "Single sign-on (SSO)")


def _all_https(*urls) -> bool:
    return all((u or "").lower().startswith("https://") for u in urls)


def configured() -> bool:
    """True only if every required OIDC endpoint + credential is set and all
    endpoints are https."""
    parts = [client_id(), client_secret(), auth_url(), token_url(), userinfo_url()]
    if not all(parts):
        return False
    return _all_https(auth_url(), token_url(), userinfo_url())


def authorize_url(redirect_uri: str, state: str) -> str:
    q = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id(),
        "redirect_uri": redirect_uri,
        "scope": scopes(),
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    })
    sep = "&" if "?" in auth_url() else "?"
    return f"{auth_url()}{sep}{q}"


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for tokens at the IdP token endpoint."""
    return _post_form(token_url(), {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id(),
        "client_secret": client_secret(),
    })


def fetch_userinfo(access_token: str) -> dict:
    return _get_json(userinfo_url(), access_token)


def domain_of(email: str) -> str:
    return (email or "").strip().lower().rsplit("@", 1)[-1] if "@" in (email or "") else ""


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


def resolve(email: str, email_verified) -> Resolution:
    """Map a verified IdP identity onto a local account. PURE decision logic
    (no HTTP) so every branch is unit-testable.

    Returns a Resolution whose status tells the caller what to do:
      ok          -> log `user` in
      jit         -> create a member in `org`, then log in
      unverified  -> refuse (email not verified by the IdP)
      deactivated -> refuse (account offboarded)
      no_account  -> refuse (no workspace claims this email's domain)
    """
    from models import Organization, User, db

    email = (email or "").strip().lower()
    if not email or not _truthy(email_verified):
        return Resolution("unverified", None, None)

    user = User.query.filter(db.func.lower(User.email) == email).first()
    if user is not None:
        if user.is_active is False:
            return Resolution("deactivated", None, None)
        return Resolution("ok", user, None)

    domain = domain_of(email)
    org = Organization.query.filter(
        db.func.lower(Organization.sso_domain) == domain
    ).first() if domain else None
    if org and _truthy(org.sso_jit):
        return Resolution("jit", None, org)
    return Resolution("no_account", None, None)

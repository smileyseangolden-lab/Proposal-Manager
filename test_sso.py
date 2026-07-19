"""Tests for OIDC single sign-on.

The security-critical account-resolution logic is pure and exhaustively tested.
The full authorization-code callback is exercised with the two IdP HTTP calls
(token exchange + userinfo) mocked, so state/CSRF handling, JIT provisioning,
linking, and refusal paths are all covered without a live IdP. (Live-IdP
verification is the one thing that still needs a real provider.)

Standalone runner: python test_sso.py
"""
import os
import sys
from unittest.mock import patch

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'
os.environ.pop('APP_ENV', None)

import app as appmod
import sso
from app import app, db
from models import Organization, User

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = failed = 0


def test(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS: {name}")
    else:
        failed += 1; print(f"  FAIL: {name} - {detail}")


# OIDC config used throughout (all https, all endpoints present)
OIDC = {
    "oidc_client_id": "cid", "oidc_client_secret": "csecret",
    "oidc_auth_url": "https://idp.example.com/auth",
    "oidc_token_url": "https://idp.example.com/token",
    "oidc_userinfo_url": "https://idp.example.com/userinfo",
    "oidc_scopes": "openid email profile", "oidc_button_label": "Sign in with Acme SSO",
}


def fake_cfg(key, default=""):
    return OIDC.get(key, default)


with app.app_context():
    db.drop_all(); db.create_all()

    print("\n=== configured() gating ===")
    with patch.object(sso, "_cfg", side_effect=fake_cfg):
        test("configured() true when all https endpoints set", sso.configured())
        test("button label read from config", sso.button_label() == "Sign in with Acme SSO")
        u = sso.authorize_url("https://app/cb", "xyz")
        test("authorize_url carries client_id/state/redirect",
             "client_id=cid" in u and "state=xyz" in u and "redirect_uri=https%3A%2F%2Fapp%2Fcb" in u)

    def cfg_missing(key, default=""):
        d = dict(OIDC); d["oidc_client_secret"] = ""
        return d.get(key, default)
    with patch.object(sso, "_cfg", side_effect=cfg_missing):
        test("configured() false when a secret is missing", not sso.configured())

    def cfg_http(key, default=""):
        d = dict(OIDC); d["oidc_token_url"] = "http://idp.example.com/token"
        return d.get(key, default)
    with patch.object(sso, "_cfg", side_effect=cfg_http):
        test("configured() false when an endpoint isn't https", not sso.configured())

    print("\n=== resolve(): the security-critical branches ===")
    # Seed: an org that claims acme.com with JIT, an existing user, a
    # deactivated user, and an org that claims beta.com WITHOUT jit.
    acme = Organization(name="Acme", sso_domain="acme.com", sso_jit=True)
    beta = Organization(name="Beta", sso_domain="beta.com", sso_jit=False)
    db.session.add_all([acme, beta]); db.session.flush()
    existing = User(username="ann", email="ann@acme.com", org_id=acme.id, role="proposal")
    existing.set_password("x"); db.session.add(existing)
    dead = User(username="dan", email="dan@acme.com", org_id=acme.id, role="proposal", is_active=False)
    dead.set_password("x"); db.session.add(dead)
    db.session.commit()

    test("unverified email refused",
         sso.resolve("ann@acme.com", False).status == "unverified")
    test("empty email refused", sso.resolve("", True).status == "unverified")
    test("existing verified user -> ok",
         sso.resolve("ANN@acme.com", True).status == "ok")
    test("existing match is the right user",
         sso.resolve("ann@acme.com", "true").user.id == existing.id)
    test("deactivated user refused",
         sso.resolve("dan@acme.com", True).status == "deactivated")
    r = sso.resolve("new@acme.com", True)
    test("unknown user at a JIT domain -> jit", r.status == "jit" and r.org.id == acme.id)
    test("unknown user at a claimed-but-no-JIT domain -> no_account",
         sso.resolve("new@beta.com", True).status == "no_account")
    test("unknown user at an unclaimed domain -> no_account",
         sso.resolve("stranger@nowhere.com", True).status == "no_account")

    print("\n=== Full callback (mocked IdP HTTP) ===")
    c = app.test_client()

    def drive_callback(userinfo, tamper_state=False):
        """Simulate: /sso/login (get state) then the IdP redirect to /sso/callback.
        Always starts anonymous so each scenario is independent."""
        c.post("/logout")
        with patch.object(sso, "_cfg", side_effect=fake_cfg):
            with c.session_transaction() as s:
                s.pop("sso_state", None)
            c.get("/sso/login")  # sets session['sso_state'] and 302s to IdP
            with c.session_transaction() as s:
                state = s.get("sso_state")
            use_state = "WRONG" if tamper_state else state
            with patch.object(sso, "exchange_code", return_value={"access_token": "at"}), \
                 patch.object(sso, "fetch_userinfo", return_value=userinfo):
                return c.get(f"/sso/callback?code=abc&state={use_state}",
                             follow_redirects=False)

    # CSRF: mismatched state is rejected and never logs in
    r = drive_callback({"email": "ann@acme.com", "email_verified": True}, tamper_state=True)
    test("state mismatch rejected", c.get("/").status_code == 302)

    # Existing user links and logs in
    r = drive_callback({"email": "ann@acme.com", "email_verified": True})
    test("existing user signs in via SSO", c.get("/").status_code == 200)

    # Unverified email from IdP is refused
    r = drive_callback({"email": "ann@acme.com", "email_verified": False})
    test("IdP-unverified email refused at callback", c.get("/").status_code == 302)

    # JIT provisioning creates a member in the claimed org, then logs in
    before = User.query.filter_by(org_id=acme.id).count()
    r = drive_callback({"email": "jit@acme.com", "email_verified": True, "name": "Jit User"})
    after = User.query.filter_by(org_id=acme.id).count()
    test("JIT provisions a new member into the claimed org", after == before + 1)
    newu = User.query.filter(appmod._email_matches("jit@acme.com")).first()
    test("JIT user is active, verified, correct org/role",
         newu and newu.org_id == acme.id and newu.role == "proposal"
         and newu.email_verified is True and newu.is_active is True)
    test("JIT user signed in", c.get("/").status_code == 200)
    c.post("/logout")

    # No workspace claims this domain -> refused, no user created
    n_before = User.query.count()
    r = drive_callback({"email": "someone@unclaimed.com", "email_verified": True})
    test("unclaimed domain refused, no account created",
         c.get("/").status_code == 302 and User.query.count() == n_before)

    # Deactivated user refused even with a valid IdP identity
    r = drive_callback({"email": "dan@acme.com", "email_verified": True})
    test("deactivated user refused at callback", c.get("/").status_code == 302)

    print("\n=== Login page button visibility ===")
    with patch.object(sso, "_cfg", side_effect=fake_cfg):
        r = c.get("/login")
        test("SSO button shown when configured",
             b'Sign in with Acme SSO' in r.data and b'/sso/login' in r.data)
    r = c.get("/login")  # unconfigured
    test("SSO button hidden when not configured", b'/sso/login' not in r.data)

    print("\n=== /sso/login refuses when unconfigured ===")
    r = c.get("/sso/login", follow_redirects=False)
    test("sso/login redirects to password login when unconfigured",
         r.status_code == 302 and "/login" in r.headers.get("Location", ""))

    print("\n=== Admin domain claim ===")
    c.post("/signup", data={"username": "boss", "email": "boss@corp.com",
                            "password": "password123", "company_name": "CorpCo"})
    boss = User.query.filter_by(username="boss").first()
    org = db.session.get(Organization, boss.org_id)
    c.post("/admin/sso", data={"sso_domain": "corp.com", "sso_jit": "1"})
    db.session.refresh(org)
    test("admin claims a domain with JIT", org.sso_domain == "corp.com" and org.sso_jit is True)
    # Can't claim a domain another org already owns
    r = c.post("/admin/sso", data={"sso_domain": "acme.com"})
    db.session.refresh(org)
    test("duplicate domain claim rejected", org.sso_domain == "corp.com")
    # Bad domain format rejected
    c.post("/admin/sso", data={"sso_domain": "not a domain"})
    db.session.refresh(org)
    test("malformed domain rejected", org.sso_domain == "corp.com")

print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
sys.exit(0 if failed == 0 else 1)

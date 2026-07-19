"""Tests for TOTP two-factor authentication: the helper module, enrollment,
the login second-step gate, backup codes, and disable.

Standalone runner: python test_2fa.py
"""
import os
import sys

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'
os.environ.pop('APP_ENV', None)

import pyotp

import crypto_util
import twofa
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


def code_for(user):
    secret = crypto_util.decrypt(user.totp_secret_encrypted)
    return pyotp.TOTP(secret).now()


with app.app_context():
    db.drop_all(); db.create_all()

    print("\n=== twofa helper module ===")
    s = twofa.new_secret()
    test("new_secret is base32-ish", isinstance(s, str) and len(s) >= 16)
    test("verify_code accepts a live code", twofa.verify_code(s, pyotp.TOTP(s).now()))
    test("verify_code rejects a bad code", not twofa.verify_code(s, "000000"))
    test("verify_code rejects non-digits", not twofa.verify_code(s, "abcdef"))

    plain, hashes = twofa.generate_backup_codes()
    test("10 backup codes generated", len(plain) == 10)
    test("backup codes stored hashed (not plaintext)",
         all(p not in hashes for p in plain))
    ok, new_json = twofa.consume_backup_code(hashes, plain[0])
    test("valid backup code consumes", ok and twofa.backup_codes_remaining(new_json) == 9)
    ok2, _ = twofa.consume_backup_code(new_json, plain[0])
    test("consumed backup code can't be reused", not ok2)
    test("backup code matching is case/dash-insensitive",
         twofa.hash_backup_code(plain[1].upper().replace('-', '')) ==
         twofa.hash_backup_code(plain[1]))

    # --- Enrollment via the app -------------------------------------------
    c = app.test_client()
    c.post('/signup', data={'username': 'tia', 'email': 'tia@corp.com',
                            'password': 'password123', 'company_name': 'TiaCorp'})
    tia = User.query.filter_by(username='tia').first()
    tia.email_verified = True
    db.session.commit()

    print("\n=== Enrollment ===")
    r = c.post('/settings/2fa/start')
    db.session.refresh(tia)
    test("start generates an (unconfirmed) secret",
         bool(tia.totp_secret_encrypted) and tia.totp_enabled is False)
    test("setup page shows a QR code", b'data:image/png;base64,' in r.data)

    r = c.post('/settings/2fa/enable', data={'code': '000000'})
    db.session.refresh(tia)
    test("wrong confirmation code does not enable", tia.totp_enabled is False)

    r = c.post('/settings/2fa/enable', data={'code': code_for(tia)})
    db.session.refresh(tia)
    test("correct code enables 2FA", tia.totp_enabled is True)
    test("backup codes shown once on enable", tia.totp_backup_codes
         and twofa.backup_codes_remaining(tia.totp_backup_codes) == 10)

    print("\n=== Login second-step gate ===")
    c.post('/logout')
    r = c.post('/login', data={'username': 'tia', 'password': 'password123'})
    test("password step redirects to 2FA (not dashboard)",
         r.status_code == 302 and '/login/2fa' in r.headers.get('Location', ''))
    test("pending 2FA does NOT authenticate",
         c.get('/').status_code == 302)  # dashboard still gated

    r = c.post('/login/2fa', data={'code': '000000'})
    test("wrong 2FA code keeps you out", c.get('/').status_code == 302)

    r = c.post('/login/2fa', data={'code': code_for(tia)}, follow_redirects=False)
    test("correct 2FA code completes login",
         c.get('/').status_code == 200)

    print("\n=== Backup-code login ===")
    c.post('/logout')
    c.post('/login', data={'username': 'tia', 'password': 'password123'})
    # Grab a fresh backup code set by regenerating (we only have hashes stored)
    c2 = app.test_client()  # fresh client to drive settings while first is mid-login
    # Actually regenerate through the logged-out flow isn't possible; instead
    # enable produced codes we didn't capture. Re-enroll to capture plaintext:
    # (simplest correct path) complete this login first via TOTP:
    c.post('/login/2fa', data={'code': code_for(tia)})
    r = c.post('/settings/2fa/backup-codes', data={'code': code_for(tia)})
    # Parse the shown plaintext codes out of the page
    import re as _re
    shown = _re.findall(rb'<li[^>]*>([0-9a-f]{4}-[0-9a-f]{4})</li>', r.data)
    test("regenerate shows fresh plaintext codes", len(shown) == 10, f"got {len(shown)}")
    backup = shown[0].decode()

    c.post('/logout')
    c.post('/login', data={'username': 'tia', 'password': 'password123'})
    c.post('/login/2fa', data={'code': backup})
    test("a backup code completes login", c.get('/').status_code == 200)
    db.session.refresh(tia)
    test("used backup code was consumed",
         twofa.backup_codes_remaining(tia.totp_backup_codes) == 9)
    # And it can't be reused
    c.post('/logout')
    c.post('/login', data={'username': 'tia', 'password': 'password123'})
    c.post('/login/2fa', data={'code': backup})
    test("consumed backup code can't be reused for login",
         c.get('/').status_code == 302)
    # Recover the session with a real code for the disable test
    c.post('/login/2fa', data={'code': code_for(tia)})

    print("\n=== Disable requires re-proving identity ===")
    r = c.post('/settings/2fa/disable', data={'password': 'wrong-password'})
    db.session.refresh(tia)
    test("wrong password does not disable", tia.totp_enabled is True)
    r = c.post('/settings/2fa/disable', data={'password': 'password123'})
    db.session.refresh(tia)
    test("correct password disables and clears the secret",
         tia.totp_enabled is False and not tia.totp_secret_encrypted
         and not tia.totp_backup_codes)

    print("\n=== Non-2FA accounts unaffected ===")
    c.post('/logout')
    c.post('/signup', data={'username': 'notfa', 'email': 'no@corp.com',
                            'password': 'password123', 'company_name': 'NoCo'})
    c.post('/logout')
    r = c.post('/login', data={'username': 'notfa', 'password': 'password123'})
    test("account without 2FA logs straight in",
         r.status_code == 302 and '/login/2fa' not in r.headers.get('Location', ''))

print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
sys.exit(0 if failed == 0 else 1)

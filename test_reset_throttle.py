"""Regression tests for the /forgot-password throttle.

The endpoint is unauthenticated and sends email, so before the throttle an
attacker could bomb a victim's inbox with reset mail (or drain the SMTP
budget) with a trivial loop. Requests are now limited per requesting IP and
per target email on a sliding window; throttled requests return the exact
same generic response so a prober learns nothing — the observable effect is
that no reset token is issued and no email is sent.

Standalone runner: python test_reset_throttle.py
"""
import os
import re
import sys
import time

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'
os.environ.pop('APP_ENV', None)

import app as appmod
from app import app, db
from models import User, UserToken

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = failed = 0


def test(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS: {name}")
    else:
        failed += 1; print(f"  FAIL: {name} - {detail}")


print("\n=== Sliding-window helper ===")
appmod._RESET_REQUESTS.clear()
for _ in range(appmod._RESET_MAX_PER_WINDOW):
    test_limited_before = appmod._reset_rate_limited("k")
    appmod._record_reset_request("k")
test("under the limit is allowed", test_limited_before is False)
test("at the limit is blocked", appmod._reset_rate_limited("k") is True)
appmod._RESET_REQUESTS["k"] = [time.time() - appmod._RESET_WINDOW_SECONDS - 1] * 10
test("old attempts age out of the window", appmod._reset_rate_limited("k") is False)
appmod._RESET_REQUESTS.clear()

with app.app_context():
    db.drop_all(); db.create_all()
    c = app.test_client()
    c.post('/signup', data={'username': 'tara', 'email': 'tara@corp.com',
                            'password': 'password123', 'company_name': 'TaraCo'})
    c.post('/signup', data={'username': 'omar', 'email': 'omar@corp.com',
                            'password': 'password123', 'company_name': 'OmarCo'})
    tara = User.query.filter_by(username='tara').first()
    omar = User.query.filter_by(username='omar').first()

    def reset_tokens(user):
        return UserToken.query.filter_by(user_id=user.id, purpose='reset').count()

    print("\n=== Throttle enforced on the live route (TESTING off) ===")
    # The gate is skipped under TESTING (like the signup throttle), so flip it
    # off and drive the real production path, CSRF included.
    app.config['TESTING'] = False
    appmod._RESET_REQUESTS.clear()
    try:
        anon = app.test_client()
        html = anon.get('/forgot-password').data.decode()
        tok = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        test("forgot-password form carries a CSRF token", bool(tok))

        for i in range(7):
            r = anon.post('/forgot-password',
                          data={'email': 'tara@corp.com', 'csrf_token': tok})
        test("responses stay identical after the limit (no probe signal)",
             r.status_code == 302)
        test("only the first 5 requests issued tokens",
             reset_tokens(tara) == appmod._RESET_MAX_PER_WINDOW,
             f"tokens={reset_tokens(tara)}")

        # The same IP is now exhausted — a different target email is blocked
        # too, so one client can't rotate through victims.
        anon.post('/forgot-password',
                  data={'email': 'omar@corp.com', 'csrf_token': tok})
        test("exhausted IP can't switch to another victim",
             reset_tokens(omar) == 0, f"tokens={reset_tokens(omar)}")

        # A different IP hammering the SAME inbox trips the per-email key.
        appmod._RESET_REQUESTS.pop("ip:127.0.0.1", None)
        anon.post('/forgot-password',
                  data={'email': 'tara@corp.com', 'csrf_token': tok})
        test("fresh IP still can't bomb an exhausted inbox",
             reset_tokens(tara) == appmod._RESET_MAX_PER_WINDOW,
             f"tokens={reset_tokens(tara)}")

        # Window expiry frees the flow again.
        for key in list(appmod._RESET_REQUESTS):
            appmod._RESET_REQUESTS[key] = [
                t - appmod._RESET_WINDOW_SECONDS - 1 for t in appmod._RESET_REQUESTS[key]
            ]
        anon.post('/forgot-password',
                  data={'email': 'tara@corp.com', 'csrf_token': tok})
        test("throttle releases after the window",
             reset_tokens(tara) == appmod._RESET_MAX_PER_WINDOW + 1,
             f"tokens={reset_tokens(tara)}")
    finally:
        app.config['TESTING'] = True
        appmod._RESET_REQUESTS.clear()

    print("\n=== Reset flow still works end to end ===")
    raw = appmod._issue_token(tara, 'reset', hours=2)
    r = c.post(f'/reset-password/{raw}',
               data={'password': 'newpassword99', 'confirm_password': 'newpassword99'})
    db.session.refresh(tara)
    test("valid token still resets the password", tara.check_password('newpassword99'))

print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
sys.exit(0 if failed == 0 else 1)

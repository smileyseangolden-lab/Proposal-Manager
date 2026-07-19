"""Security regression tests: tenant isolation, billing gates, auth, SSRF/XSS,
webhook integrity, and encryption. Standalone runner (python test_security.py).

Covers the blocker + High/Medium/Low fixes from the release-readiness audit so
they can't silently regress.
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'
os.environ.pop('APP_ENV', None)

import app as appmod
from app import app, db
import billing, integrations, crypto_util, htmlsafe, jobs
from models import (User, Organization, Project, Proposal, ProposalComment,
                    OrgInvitation, BackgroundJob, LlmUsage, ProcessedWebhookEvent)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = failed = 0
def test(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS: {name}")
    else:
        failed += 1; print(f"  FAIL: {name} - {detail}")


def login(c, u):
    c.post('/logout')
    c.post('/login', data={'username': u, 'password': 'password123'})


with app.app_context():
    db.drop_all(); db.create_all()
    import migrations; migrations._add_indexes()
    c = app.test_client()
    c.post('/signup', data={'username':'alice','email':'alice@a.com','password':'password123','display_name':'Alice','company_name':'OrgA'})
    c.post('/logout')
    c.post('/signup', data={'username':'bob','email':'bob@b.com','password':'password123','display_name':'Bob','company_name':'OrgB'})
    c.post('/logout')
    alice = User.query.filter_by(username='alice').first()
    bob = User.query.filter_by(username='bob').first()

    proj = Project(user_id=alice.id, org_id=alice.org_id, name='Bid'); db.session.add(proj); db.session.flush()
    prop = Proposal(project_id=proj.id, job_id='j1', document_type='RFP', md_file='sec_dummy.md'); db.session.add(prop); db.session.flush()
    cmt = ProposalComment(proposal_id=prop.id, author_id=alice.id, body='hi'); db.session.add(cmt); db.session.commit()
    from config.settings import GENERATED_DIR
    (GENERATED_DIR / 'sec_dummy.md').write_text('# Bid', encoding='utf-8')

    print("\n=== Tenant isolation ===")
    login(c, 'bob')  # foreign-org admin
    test("foreign admin can't view proposal", c.get(f'/proposal/{prop.id}').status_code == 404)
    test("foreign admin can't share proposal", c.post(f'/proposal/{prop.id}/share', data={}).status_code == 404)
    test("foreign admin can't toggle role", c.post(f'/admin/toggle-admin/{alice.id}').status_code == 404)
    test("foreign admin can't update role", c.post(f'/admin/update-role/{alice.id}', data={'role':'proposal'}).status_code == 404)
    test("foreign admin can't delete comment", c.post(f'/proposal/{prop.id}/comments/{cmt.id}/delete').status_code == 404)
    db.session.refresh(alice)
    test("victim role unchanged", alice.is_admin is True and db.session.get(ProposalComment, cmt.id) is not None)
    login(c, 'alice')  # owner
    test("owner can view own proposal", c.get(f'/proposal/{prop.id}').status_code == 200)
    (GENERATED_DIR / 'sec_dummy.md').unlink()

    print("\n=== Billing gates ===")
    # Fresh workspaces carry a 14-day Pro trial (P2); these assertions test the
    # FREE-plan limits, so end the trials first.
    Organization.query.update({'trial_ends_at': None})
    db.session.commit()
    billing.STRIPE_SECRET_KEY = ''
    login(c, 'bob')
    appmod.SELF_HOSTED = False
    c.post('/billing/checkout/business')
    test("no-Stripe paid upgrade refused", (db.session.get(Organization, bob.org_id).plan or 'free') == 'free')
    appmod.SELF_HOSTED = True
    c.post('/billing/checkout/business')
    test("self-hosted upgrade allowed", db.session.get(Organization, bob.org_id).plan == 'business')
    appmod.SELF_HOSTED = False
    # delinquent billing_status -> free limits
    orgB = db.session.get(Organization, bob.org_id); orgB.billing_status = 'past_due'; db.session.commit()
    test("past_due org soft-locked to free limits", billing.limits_for(orgB)['generations_per_month'] == billing.PLANS['free']['limits']['generations_per_month'])
    orgB.plan = 'free'; orgB.billing_status = ''; db.session.commit()
    # generation limit counts queued
    for _ in range(5):
        db.session.add(BackgroundJob(kind='generate_proposal', org_id=alice.org_id, user_id=alice.id,
                                     status='queued', created_at=datetime.now(timezone.utc)))
    db.session.commit()
    test("generation limit counts in-flight jobs", billing.check_generation(alice.org_id)[0] is False)
    # AI token budget
    db.session.add(LlmUsage(org_id=bob.org_id, input_tokens=600_000, output_tokens=0, created_at=datetime.now(timezone.utc)))
    db.session.commit()
    test("AI token budget enforced", billing.check_ai_budget(bob.org_id)[0] is False)

    print("\n=== Auth ===")
    # per-account lockout (must be logged out — /login early-returns if authed)
    c.post('/logout')
    for _ in range(10):
        appmod._LOGIN_ATTEMPTS.clear()
        c.post('/login', data={'username':'alice','password':'nope'})
    db.session.refresh(alice)
    test("account locks after repeated failures", alice.lockout_until is not None)
    alice.lockout_until = None; alice.failed_login_count = 0; db.session.commit()
    # invite bound to email
    inv = OrgInvitation(org_id=alice.org_id, email='invitee@corp.com', role='proposal',
                        expires_at=datetime.utcnow() + timedelta(days=7))
    db.session.add(inv); db.session.commit()
    c.post('/logout')
    c.post('/signup', data={'username':'invitee','email':'attacker@evil.com','password':'password123','invite_token':inv.token})
    iu = User.query.filter_by(username='invitee').first()
    test("invite bound to invited email", iu is not None and iu.email == 'invitee@corp.com')

    print("\n=== Webhook integrity ===")
    billing.STRIPE_SECRET_KEY, billing.STRIPE_WEBHOOK_SECRET = 'sk_x', ''
    test("webhook refuses unsigned (503)", c.post('/billing/webhook', data='{}', content_type='application/json').status_code == 503)
    billing.STRIPE_WEBHOOK_SECRET = 'whsec_x'
    evt = {"id": "evt_test_1", "type": "customer.subscription.updated",
           "data": {"object": {"id": "sub_none", "status": "active"}}}
    with patch('stripe.Webhook.construct_event', return_value=evt):
        r1 = c.post('/billing/webhook', data='{}', content_type='application/json',
                    headers={'Stripe-Signature': 'x'})
        r2 = c.post('/billing/webhook', data='{}', content_type='application/json',
                    headers={'Stripe-Signature': 'x'})
    test("first webhook processed", r1.status_code == 200 and r1.get_json().get('duplicate') is not True)
    test("replayed webhook deduped", r2.get_json().get('duplicate') is True)
    billing.STRIPE_SECRET_KEY = ''; billing.STRIPE_WEBHOOK_SECRET = ''

    print("\n=== SSRF / XSS ===")
    test("blocks metadata IP webhook", integrations.is_safe_webhook_url('http://169.254.169.254/x') is False)
    test("blocks private webhook", integrations.is_safe_webhook_url('http://10.0.0.1/x') is False)
    test("allows public webhook", integrations.is_safe_webhook_url('https://1.1.1.1/x') is True)
    test("enforces slack host", integrations.is_safe_webhook_url('https://evil.com/x', require_https=True, host_suffix='slack.com') is False)
    clean = htmlsafe.sanitize('<h1>Hi</h1><script>x()</script><img src=x onerror=y>')
    test("sanitizer strips script/handlers", '<script' not in clean.lower() and 'onerror' not in clean.lower() and '<h1>Hi</h1>' in clean)

    print("\n=== Ops / crypto ===")
    stale = BackgroundJob(kind='generate_proposal', org_id=alice.org_id, user_id=alice.id, status='running',
                          started_at=datetime.now(timezone.utc) - timedelta(hours=1))
    db.session.add(stale); db.session.commit()
    jobs.reap_stale_jobs(); db.session.refresh(stale)
    test("orphaned job reaped", stale.status == 'failed')
    test("/healthz ok", c.get('/healthz').status_code == 200)
    os.environ['APP_ENCRYPTION_KEY'] = 'kA'; os.environ.pop('APP_ENCRYPTION_KEY_OLD', None)
    tok = crypto_util.encrypt('secret')
    os.environ['APP_ENCRYPTION_KEY'] = 'kB'; os.environ['APP_ENCRYPTION_KEY_OLD'] = 'kA'
    test("decrypts via rotated-out key", crypto_util.decrypt(tok) == 'secret')
    os.environ.pop('APP_ENCRYPTION_KEY', None); os.environ.pop('APP_ENCRYPTION_KEY_OLD', None)

print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
if failed:
    print("SOME TESTS FAILED"); sys.exit(1)
print("ALL TESTS PASSED")

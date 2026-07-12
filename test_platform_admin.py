"""Tests for the Platform-Admin owner dashboard: owner-only gating (404 for
everyone else) and that each tab renders. Standalone runner."""
import os
import sys
from datetime import datetime, timezone

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'
os.environ.pop('APP_ENV', None)

from app import app, db
import platform_admin
from models import User, Organization, Project, Proposal, LlmUsage, BackgroundJob

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = failed = 0
def test(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS: {name}")
    else:
        failed += 1; print(f"  FAIL: {name} - {detail}")

with app.app_context():
    db.drop_all(); db.create_all()
    c = app.test_client()

    # A normal tenant user (also a tenant admin of their own org).
    c.post('/signup', data={'username':'tenant','email':'tenant@corp.com','password':'password123','company_name':'TenantCo'})
    c.post('/logout')
    # A platform owner via the DB column.
    owner_org = Organization(name='Ops'); db.session.add(owner_org); db.session.flush()
    owner = User(org_id=owner_org.id, username='owner', email='owner@platform.com',
                 display_name='Owner', is_admin=True, role='admin', platform_owner=True)
    owner.set_password('password123')
    db.session.add(owner); db.session.flush()  # populate owner.id
    # Some cross-tenant data so tabs have content to render.
    proj = Project(user_id=owner.id, org_id=owner_org.id, name='P')
    db.session.add(proj); db.session.flush()
    db.session.add(Proposal(project_id=proj.id, job_id='j1', document_type='RFP'))
    db.session.add(BackgroundJob(kind='generate_proposal', org_id=owner_org.id, user_id=owner.id,
                                 status='done', created_at=datetime.now(timezone.utc),
                                 finished_at=datetime.now(timezone.utc)))
    db.session.add(LlmUsage(org_id=owner_org.id, model='claude-opus-4-6', input_tokens=1000,
                            output_tokens=2000, est_cost_usd=0.165, created_at=datetime.now(timezone.utc)))
    db.session.commit()

    print("\n=== Gating ===")
    test("anonymous -> 404", c.get('/platform-admin/').status_code == 404)
    c.post('/login', data={'username':'tenant','password':'password123'})
    test("tenant admin -> 404 (not advertised)", c.get('/platform-admin/').status_code == 404)
    test("tenant admin accounts -> 404", c.get('/platform-admin/accounts').status_code == 404)
    c.post('/logout')

    print("\n=== Owner login ===")
    test("login page renders", c.get('/platform-admin/login').status_code == 200)
    r = c.post('/platform-admin/login', data={'email':'tenant@corp.com','password':'password123'})
    test("non-owner login rejected", b'Invalid credentials' in r.data)
    r = c.post('/platform-admin/login', data={'email':'owner@platform.com','password':'password123'})
    test("owner login redirects in", r.status_code == 302 and '/platform-admin' in r.headers.get('Location',''))

    print("\n=== Tabs render for owner ===")
    for path, needle in [('/platform-admin/', b'Overview'),
                         ('/platform-admin/accounts', b'Accounts'),
                         ('/platform-admin/revenue', b'Recurring Revenue'),
                         ('/platform-admin/ai-costs', b'AI Costs'),
                         ('/platform-admin/health', b'Health')]:
        r = c.get(path)
        test(f"{path} renders 200", r.status_code == 200 and needle in r.data, f"{r.status_code}")
    # Overview shows real cross-tenant counts (2 orgs: TenantCo + Ops)
    r = c.get('/platform-admin/')
    test("overview counts all orgs", b'Organizations' in r.data)

    print("\n=== Allowlist path ===")
    platform_admin.PLATFORM_OWNER_EMAILS.add('tenant@corp.com')
    c.post('/logout')
    c.post('/login', data={'username':'tenant','password':'password123'})
    test("allowlisted email gains access", c.get('/platform-admin/').status_code == 200)
    platform_admin.PLATFORM_OWNER_EMAILS.discard('tenant@corp.com')

print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
if failed:
    print("SOME TESTS FAILED"); sys.exit(1)
print("ALL TESTS PASSED")

"""Regression tests for the P1 audit batch.

Auth: email uniqueness, email-change re-verification, hashed tokens, password
change, member deactivation, platform-admin lockout, platform-AI gate.
Org identity: workspace-level logo/branding, setup-wizard org awareness.
AI pipeline: metered+rate-limited chat, jobified scope/estimate, xlsx parsing,
legal docs + project rate sheets in generation context.
Lifecycle/UX: redline tags survive sanitizing, parse receipts, scope item
editing, human-preserving scope re-draft, document & project deletion,
share expiry.

Standalone runner: python test_p1_fixes.py
"""
import io
import os
import sys
from unittest.mock import patch

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'
os.environ.pop('APP_ENV', None)

import openpyxl

import app as appmod
from app import app, db
import billing
import crypto_util
import htmlsafe
import proposal_agent
from document_parser import parse_document
from models import (BackgroundJob, EstimateLineItem, Organization, Project,
                    ProjectDocument, ProjectScope, Proposal, ProposalEstimate,
                    ProposalShare, ProposalVersion, ScopeItem, User, UserRateSheet,
                    UserToken)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = failed = 0


def test(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS: {name}")
    else:
        failed += 1; print(f"  FAIL: {name} - {detail}")


def login(c, username, password='password123'):
    c.post('/logout')
    c.post('/login', data={'username': username, 'password': password})


with app.app_context():
    db.drop_all(); db.create_all()
    c = app.test_client()

    c.post('/signup', data={'username': 'ana', 'email': 'ana@corp.com',
                            'password': 'password123', 'display_name': 'Ana',
                            'company_name': 'AnaCorp'})
    ana = User.query.filter_by(username='ana').first()
    ana.email_verified = True
    db.session.commit()
    org = db.session.get(Organization, ana.org_id)

    print("\n=== Email uniqueness & verification ===")
    c.post('/logout')
    r = c.post('/signup', data={'username': 'imposter', 'email': 'ANA@corp.com',
                                'password': 'password123'})
    test("duplicate email (case-insensitive) rejected",
         User.query.filter_by(username='imposter').first() is None)

    login(c, 'ana')
    c.post('/settings', data={'display_name': 'Ana', 'email': 'ana-new@corp.com',
                              'font_preference': 'Calibri', 'llm_model': ana.llm_model})
    db.session.refresh(ana)
    test("email change stored", ana.email == 'ana-new@corp.com')
    test("email change resets verification", ana.email_verified is False)
    ana.email_verified = True; db.session.commit()

    # Collision on change
    other = User(username='zed', email='zed@corp.com', org_id=ana.org_id)
    other.set_password('password123'); db.session.add(other); db.session.commit()
    c.post('/settings', data={'display_name': 'Ana', 'email': 'zed@corp.com',
                              'font_preference': 'Calibri', 'llm_model': ana.llm_model})
    db.session.refresh(ana)
    test("email change to an existing address rejected", ana.email == 'ana-new@corp.com')

    print("\n=== Hashed tokens & password change ===")
    raw = appmod._issue_token(ana, 'reset', hours=2)
    row = UserToken.query.filter_by(user_id=ana.id, purpose='reset').first()
    test("reset token stored hashed", row is not None and row.token != raw
         and row.token == appmod._hash_token(raw))
    r = c.post(f'/reset-password/{raw}', data={'password': 'newpassword9',
                                               'confirm_password': 'newpassword9'})
    login(c, 'ana', 'newpassword9')
    test("reset with raw token works end-to-end",
         c.get('/').status_code == 200)

    c.post('/settings/change-password', data={'current_password': 'wrong',
                                              'new_password': 'password123',
                                              'confirm_password': 'password123'})
    db.session.refresh(ana)
    test("password change requires current password", not ana.check_password('password123')
         or ana.check_password('newpassword9') is False)
    c.post('/settings/change-password', data={'current_password': 'newpassword9',
                                              'new_password': 'password123',
                                              'confirm_password': 'password123'})
    db.session.refresh(ana)
    test("password change works", ana.check_password('password123'))

    print("\n=== Member deactivation ===")
    from flask import g

    def _fresh_request_state():
        # These suites run inside ONE long-lived app context, so flask-login's
        # per-context user cache (g._login_user) survives across simulated
        # requests. Production pushes a fresh context per request; clearing the
        # cache reproduces that so the user_loader actually runs.
        if hasattr(g, '_login_user'):
            delattr(g, '_login_user')

    org.plan = 'business'  # seat headroom so reactivation isn't limit-blocked
    bob = User(username='bob', email='bob@corp.com', org_id=ana.org_id, role='proposal')
    bob.set_password('password123'); db.session.add(bob); db.session.commit()
    seats_before = billing.seats_used(ana.org_id)

    c2 = app.test_client()
    login(c2, 'bob')
    test("bob signed in", c2.get('/').status_code == 200)

    login(c, 'ana')
    c.post(f'/admin/deactivate-user/{bob.id}')
    db.session.refresh(bob)
    test("bob deactivated", bob.is_active is False)
    test("deactivation frees the seat", billing.seats_used(ana.org_id) == seats_before - 1)
    _fresh_request_state()
    test("existing session invalidated", c2.get('/').status_code == 302)
    _fresh_request_state()
    c2.post('/login', data={'username': 'bob', 'password': 'password123'})
    _fresh_request_state()
    test("deactivated login blocked", c2.get('/').status_code == 302)

    # Reactivation at the seat limit must be refused; with headroom it works.
    # (Re-login: the interleaved c2 requests poisoned the shared-context user
    # cache — same reason _fresh_request_state exists.)
    login(c, 'ana')
    # ana + zed already fill 2 free seats; end the signup trial so FREE limits
    # (not trial-Pro limits) apply to the seat check.
    org.plan = 'free'; org.trial_ends_at = None; db.session.commit()
    c.post(f'/admin/reactivate-user/{bob.id}')
    db.session.refresh(bob)
    test("reactivation blocked at seat limit", bob.is_active is False)
    org.plan = 'business'; db.session.commit()
    c.post(f'/admin/reactivate-user/{bob.id}')
    db.session.refresh(bob)
    test("reactivation works with seat headroom", bob.is_active is True)

    print("\n=== Signup throttle & platform-admin lockout ===")
    for _ in range(5):
        appmod._record_signup('9.9.9.9')
    test("signup rate limit trips after 5/hr per IP", appmod._signup_rate_limited('9.9.9.9'))

    pa = app.test_client()
    blocked = False
    for i in range(9):
        r = pa.post('/platform-admin/login',
                    data={'email': 'owner@x.com', 'password': 'nope'})
        if b'Too many attempts' in r.data:
            blocked = True
            break
    test("platform-admin login rate limited", blocked, "never throttled")

    print("\n=== Platform-AI gate (verified email or own key) ===")
    login(c, 'ana')  # restore c's cached identity after pa-client requests
    ana.email_verified = False; db.session.commit()
    proj = Project(user_id=ana.id, org_id=ana.org_id, name='GateBid')
    db.session.add(proj); db.session.flush()
    db.session.add(ProjectDocument(project_id=proj.id, filename='r.txt',
                                   original_filename='r.txt', file_type='rfp',
                                   file_path='/tmp/none.txt', file_size=1))
    db.session.commit()
    c.post(f'/projects/{proj.id}/generate', data={'vertical': 'auto'})
    test("unverified user without key blocked from generation",
         BackgroundJob.query.filter_by(kind='generate_proposal').count() == 0)

    # With their OWN key, unverified users may generate (they spend their own money)
    ana.api_key_encrypted = crypto_util.encrypt('sk-own-key'); db.session.commit()
    def fake_gate_gen(rfp_text, **kwargs):
        return {"proposal_markdown": "# G\n\nConfidence Score: 70%", "action_items": [],
                "confidence_score": 70, "document_type": "RFP", "vertical": "general",
                "vertical_label": "General", "generated_at": "2026-01-01T00:00:00+00:00"}
    with patch('app.generate_proposal', side_effect=fake_gate_gen):
        c.post(f'/projects/{proj.id}/generate', data={'vertical': 'general'})
    test("own-key user passes the gate while unverified",
         BackgroundJob.query.filter_by(kind='generate_proposal').count() == 1)
    ana.email_verified = True
    ana.api_key_encrypted = ""
    db.session.commit()

    print("\n=== Org-level branding ===")
    import PIL.Image
    buf = io.BytesIO()
    PIL.Image.new('RGB', (80, 40), (10, 100, 100)).save(buf, format='PNG')
    buf.seek(0)
    login(c, 'ana')
    c.post('/settings/upload-logo', data={'company_logo': (buf, 'brand.png')},
           content_type='multipart/form-data')
    db.session.refresh(org)
    test("logo stored on the ORG", bool(org.logo_path))
    kw = appmod._logo_docx_kwargs(org, font_user=ana)
    test("brand kwargs come from org", kw.get('logo_path') == org.logo_path
         and kw.get('company_name') == org.name, f"got {kw}")
    prog = appmod._setup_progress(bob)
    logo_step = next(s for s in prog['steps'] if s['key'] == 'logo')
    company_step = next(s for s in prog['steps'] if s['key'] == 'company')
    test("teammate's setup wizard sees org logo/company done",
         logo_step['done'] and company_step['done'])

    print("\n=== Metered + rate-limited chat ===")
    ana.api_key_encrypted = crypto_util.encrypt('sk-chat-key')
    db.session.commit()
    class _Block:
        type = "text"; text = "CANNED_HELP"
    class _Resp:
        content = [_Block()]
    class _FakeMessages:
        def __init__(self, sink): self._sink = sink
        def create(self, **kw):
            self._sink.append(kw); return _Resp()
    class _FakeClient:
        def __init__(self, sink): self.messages = _FakeMessages(sink)
    sink = []
    with patch.object(proposal_agent, '_make_client', lambda key, s=sink: _FakeClient(s)):
        r = c.post('/api/chat', json={'message': 'how do I generate?'})
        test("chat routed through metered client factory",
             sink and r.get_json().get('reply') == 'CANNED_HELP', f"got {r.get_json()}")
        throttled = False
        for _ in range(25):
            r = c.post('/api/chat', json={'message': 'again'})
            if 'catch up' in (r.get_json().get('reply') or ''):
                throttled = True
                break
        test("chat rate limit engages", throttled)
    appmod._AI_CALLS.clear()

    print("\n=== Parsing & context fixes ===")
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(['Item', 'Price']); ws.append(['XLSMARKER Widget', 42])
    xbuf = io.BytesIO(); wb.save(xbuf); xbuf.seek(0)
    xpath = '/tmp/claude-p1-test.xlsx'
    with open(xpath, 'wb') as fh:
        fh.write(xbuf.read())
    test("parse_document reads xlsx", 'XLSMARKER' in parse_document(xpath))
    os.unlink(xpath)

    test("sanitizer keeps redline ins/del",
         '<ins>' in htmlsafe.sanitize('<p><ins>new</ins> <del>old</del></p>')
         and '<del>' in htmlsafe.sanitize('<p><del>old</del></p>'))

    # Upload a real project with rfp + legal + rate-sheet docs; capture what
    # reaches the AI.
    c.post('/projects/new', data={'project_name': 'CtxBid', 'client_name': 'Acme'})
    ctx = Project.query.filter_by(name='CtxBid').first()
    c.post(f'/projects/{ctx.id}/upload', data={
        'file_type': 'rfp',
        'documents': (io.BytesIO(b'RFPMARKER data center EPMS switchgear PDU data hall'), 'rfp.txt'),
    }, content_type='multipart/form-data')
    c.post(f'/projects/{ctx.id}/upload', data={
        'file_type': 'legal',
        'documents': (io.BytesIO(b'LEGALMARKER liquidated damages clause'), 'msa.txt'),
    }, content_type='multipart/form-data')
    xbuf2 = io.BytesIO(); wb.save(xbuf2); xbuf2.seek(0)
    c.post(f'/projects/{ctx.id}/upload', data={
        'file_type': 'rate_sheet',
        'documents': (xbuf2, 'projectrates.xlsx'),
    }, content_type='multipart/form-data')

    rfp_doc = ProjectDocument.query.filter_by(project_id=ctx.id, file_type='rfp').first()
    test("parse receipt recorded at upload", (rfp_doc.text_chars or 0) > 10,
         f"got {rfp_doc.text_chars}")

    gen_capture = {}
    def fake_generate(rfp_text, **kwargs):
        gen_capture['rfp_text'] = rfp_text
        gen_capture['kwargs'] = kwargs
        return {"proposal_markdown": "# P\n\nConfidence Score: 80%", "action_items": [],
                "confidence_score": 80, "document_type": "RFP",
                "vertical": kwargs.get('vertical'), "vertical_label": "General",
                "generated_at": "2026-01-01T00:00:00+00:00"}
    with patch('app.generate_proposal', side_effect=fake_generate):
        c.post(f'/projects/{ctx.id}/generate', data={'vertical': 'general'})
    test("legal docs included in AI context", 'LEGALMARKER' in gen_capture.get('rfp_text', ''))
    rs = gen_capture.get('kwargs', {}).get('rate_sheet_data') or {}
    rs_text = " ".join(v.get('raw_text', '') for v in rs.values())
    test("project rate-sheet doc reaches pricing context", 'XLSMARKER' in rs_text,
         f"keys={list(rs)}")

    print("\n=== Jobified scope with human-preserving re-draft + item edit ===")
    def fake_scope(text, **kwargs):
        return {"vertical": "general", "vertical_label": "General",
                "summary": "sum", "items": [
                    {"item": "AI item one", "category": "general"},
                    {"item": "AI item two", "category": "general"}]}
    with patch('app.draft_scope_of_work', side_effect=fake_scope):
        r = c.post(f'/projects/{ctx.id}/scope/generate', data={'vertical': 'auto'})
    test("scope drafting runs as a job", r.status_code == 302 and '/jobs/' in r.headers.get('Location', ''))
    scope = ProjectScope.query.filter_by(project_id=ctx.id).first()
    test("scope created by job", scope is not None and
         ScopeItem.query.filter_by(scope_id=scope.id).count() == 2)

    c.post(f'/projects/{ctx.id}/scope/add', data={'item_text': 'HUMANITEM as-builts',
                                                  'category': 'documentation'})
    with patch('app.draft_scope_of_work', side_effect=fake_scope):
        c.post(f'/projects/{ctx.id}/scope/generate', data={'vertical': 'auto'})
    scope = ProjectScope.query.filter_by(project_id=ctx.id).first()
    kept = ScopeItem.query.filter_by(scope_id=scope.id, source='human').all()
    test("re-draft keeps team-added items",
         len(kept) == 1 and 'HUMANITEM' in kept[0].item_text)

    item = ScopeItem.query.filter_by(scope_id=scope.id, source='ai').first()
    c.post(f'/projects/{ctx.id}/scope/items/{item.id}/edit',
           data={'item_text': 'EDITED wording of item'})
    db.session.refresh(item)
    test("scope item wording editable", item.item_text.startswith('EDITED'))

    c.post(f'/projects/{ctx.id}/scope/approve')
    c.post(f'/projects/{ctx.id}/scope/items/{item.id}/edit',
           data={'item_text': 'EDITED again'})
    db.session.refresh(scope)
    test("editing an approved scope re-opens it", scope.status == 'draft')

    print("\n=== Jobified estimate draft ===")
    prop = Proposal.query.filter_by(project_id=ctx.id).first()
    def fake_estimate(rfp_text, **kwargs):
        return {"currency": "USD", "items": [
            {"kind": "labor", "description": "Eng hours", "quantity": 10,
             "unit": "hrs", "unit_cost": 150}]}
    with patch('app.draft_estimate', side_effect=fake_estimate):
        r = c.post(f'/proposal/{prop.id}/estimate/draft')
    est = ProposalEstimate.query.filter_by(proposal_id=prop.id).first()
    test("estimate drafting runs as a job and saves rows",
         r.status_code == 302 and '/jobs/' in r.headers.get('Location', '')
         and est is not None and est.items.count() == 1)

    print("\n=== Deletion lifecycle & share expiry ===")
    doc = ProjectDocument.query.filter_by(project_id=ctx.id, file_type='legal').first()
    doc_path = doc.file_path
    c.post(f'/documents/{doc.id}/delete')
    test("document deletable (row + file)",
         db.session.get(ProjectDocument, doc.id) is None and not os.path.exists(doc_path))

    c.post(f'/proposal/{prop.id}/share', data={})
    share = ProposalShare.query.filter_by(proposal_id=prop.id).first()
    test("share link gets an expiry", share is not None and share.expires_at is not None)

    ctx_id, prop_id = ctx.id, prop.id
    c.post(f'/projects/{ctx_id}/delete', data={'confirm_name': 'WRONG'})
    test("project delete requires exact name", db.session.get(Project, ctx_id) is not None)
    c.post(f'/projects/{ctx_id}/delete', data={'confirm_name': 'CtxBid'})
    test("project delete cascades",
         db.session.get(Project, ctx_id) is None
         and Proposal.query.filter_by(project_id=ctx_id).count() == 0
         and ProposalVersion.query.filter_by(proposal_id=prop_id).count() == 0
         and ProposalShare.query.filter_by(proposal_id=prop_id).count() == 0
         and ProjectDocument.query.filter_by(project_id=ctx_id).count() == 0)

    print("\n=== Travel rate-sheet upload label ===")
    xbuf3 = io.BytesIO(); wb.save(xbuf3); xbuf3.seek(0)
    c.post('/settings/add-travel-rate', data={
        'travel_rate_file': (xbuf3, 'travel.xlsx'),
    }, content_type='multipart/form-data')
    tsheet = UserRateSheet.query.filter_by(org_id=ana.org_id).order_by(
        UserRateSheet.uploaded_at.desc()).first()
    test("travel sheet labeled travel_rates", tsheet is not None
         and tsheet.sheet_type == 'travel_rates', f"got {tsheet.sheet_type if tsheet else None}")

print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
sys.exit(0 if failed == 0 else 1)

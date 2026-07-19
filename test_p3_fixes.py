"""Regression tests for the P3 journey-polish round.

Jobs UX (cancel + discard guard), login-by-email + preserved form values,
exact-match email lookups, generation quota hint + self-notification +
honored PDF output format, batched scope updates, the action-items send gate,
portal PDF / typed-name acceptance / first-view notification, money_compact,
clarification int safety, posture inline edit routes, and the admin-only
"Invite your team" wizard step.

Standalone runner: python test_p3_fixes.py
"""
import io
import os
import sys
from unittest.mock import patch

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'
os.environ.pop('APP_ENV', None)

import app as appmod
from app import app, db
import jobs
from models import (BackgroundJob, ClarificationItem, EquipmentItem,
                    Notification, Organization, Project, ProjectScope, Proposal,
                    ProposalShare, ProposalVersion, ScopeItem,
                    TravelExpenseRate, User)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = failed = 0


def test(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS: {name}")
    else:
        failed += 1; print(f"  FAIL: {name} - {detail}")


def fake_generate(md_marker="V1MARKER"):
    def _fake(rfp_text, **kwargs):
        return {"proposal_markdown": f"# Proposal {md_marker}\n\nConfidence Score: 80%",
                "action_items": [], "confidence_score": 80, "document_type": "RFP",
                "vertical": kwargs.get("vertical") or "general",
                "vertical_label": "General",
                "generated_at": "2026-01-01T00:00:00+00:00"}
    return _fake


with app.app_context():
    db.drop_all(); db.create_all()
    c = app.test_client()
    c.post('/signup', data={'username': 'zoe', 'email': 'zoe@corp.com',
                            'password': 'password123', 'display_name': 'Zoe',
                            'company_name': 'ZoeCorp'})
    zoe = User.query.filter_by(username='zoe').first()
    zoe.email_verified = True
    db.session.commit()
    org = db.session.get(Organization, zoe.org_id)

    print("\n=== Login by email + preserved form values ===")
    c.post('/logout')
    r = c.post('/login', data={'username': 'ZOE@corp.com', 'password': 'password123'})
    test("login with email (case-insensitive)", c.get('/').status_code == 200)
    c.post('/logout')
    r = c.post('/login', data={'username': 'zoe@corp.com', 'password': 'wrong'})
    test("failed login preserves the typed identifier",
         r.status_code == 200 and b'value="zoe@corp.com"' in r.data)
    test("wildcard email can't match",
         User.query.filter(appmod._email_matches('%')).first() is None)

    r = c.post('/signup', data={'username': 'zoe', 'email': 'new@x.com',
                                'password': 'password123', 'display_name': 'Newbie',
                                'company_name': 'NewCo'})
    test("signup error preserves typed values",
         r.status_code == 200 and b'value="Newbie"' in r.data
         and b'value="new@x.com"' in r.data)

    c.post('/login', data={'username': 'zoe', 'password': 'password123'})

    print("\n=== Job cancel + discard guard ===")
    job = BackgroundJob(kind='draft_scope', user_id=zoe.id, org_id=zoe.org_id,
                        status='queued', payload='{}')
    db.session.add(job); db.session.commit()
    c.post(f'/jobs/{job.id}/cancel')
    db.session.refresh(job)
    test("queued job cancels", job.status == 'canceled')
    test("canceled job is never claimed", jobs._claim() is None)
    r = c.get(f'/jobs/{job.id}/status.json')
    test("status.json reports canceled", r.get_json().get('status') == 'canceled')

    @jobs.register("p3_cancel_midrun")
    def _h(payload, j):
        # Simulates the user canceling while the handler is still running
        j.status = 'canceled'
        db.session.commit()
        return {"redirect": "/should-be-discarded"}

    job2 = BackgroundJob(kind='p3_cancel_midrun', user_id=zoe.id, org_id=zoe.org_id,
                         status='queued', payload='{}')
    db.session.add(job2); db.session.commit()
    jobs._run_job(job2.id)
    db.session.refresh(job2)
    test("mid-run cancel discards the result",
         job2.status == 'canceled' and 'discarded' not in (job2.result or ''))

    print("\n=== Generation: quota hint, PDF format, self-notification ===")
    c.post('/projects/new', data={'project_name': 'PolishBid', 'client_name': 'Acme'})
    proj = Project.query.filter_by(name='PolishBid').first()
    c.post(f'/projects/{proj.id}/upload', data={
        'file_type': 'rfp',
        'documents': (io.BytesIO(b'RFP for a data center EPMS project'), 'rfp.txt'),
    }, content_type='multipart/form-data')

    r = c.get(f'/projects/{proj.id}/upload')
    test("generate card shows quota usage", b'used this month' in r.data)

    with patch('app.generate_proposal', side_effect=fake_generate()):
        c.post(f'/projects/{proj.id}/generate',
               data={'vertical': 'general', 'output_format': 'pdf'})
    prop = Proposal.query.filter_by(project_id=proj.id).first()
    test("proposal generated", prop is not None)
    from config.settings import GENERATED_DIR
    test("output_format=pdf pre-renders the PDF",
         bool(prop.pdf_file) and (GENERATED_DIR / prop.pdf_file).exists(),
         f"pdf_file={prop.pdf_file!r}")
    test("generator gets a completion notification",
         Notification.query.filter_by(user_id=zoe.id, category='proposal_generated')
         .filter(Notification.title.like('Your proposal is ready%')).count() == 1)

    print("\n=== Batched scope updates ===")
    def fake_scope(text, **kwargs):
        return {"vertical": "general", "vertical_label": "General", "summary": "s",
                "items": [{"item": f"Item {i}", "category": "general"} for i in range(4)]}
    with patch('app.draft_scope_of_work', side_effect=fake_scope):
        c.post(f'/projects/{proj.id}/scope/generate', data={'vertical': 'auto'})
    scope = ProjectScope.query.filter_by(project_id=proj.id).first()
    items = ScopeItem.query.filter_by(scope_id=scope.id).order_by(ScopeItem.sort_order).all()
    test("scope drafted with 4 items", len(items) == 4)

    c.post(f'/projects/{proj.id}/scope/approve')
    db.session.refresh(scope)
    test("scope approved", scope.status == 'approved')

    # Keep only the first two items, in ONE post
    c.post(f'/projects/{proj.id}/scope/batch-update',
           data={'included': [items[0].id, items[1].id]})
    statuses = [db.session.get(ScopeItem, i.id).status for i in items]
    test("batch update applied all checkbox states",
         statuses == ['included', 'included', 'removed', 'removed'], statuses)
    db.session.refresh(scope)
    test("batch change reopens an approved scope", scope.status == 'draft')
    r = c.get(f'/projects/{proj.id}/scope')
    test("scope page shows the review summary", b'AI kept' in r.data and b'struck' in r.data)

    print("\n=== Action-items send gate ===")
    db.session.add(ProposalVersion(proposal_id=prop.id, version_number=2,
                                   markdown_content='# v2'))
    prop.review_status = 'internally_approved'
    prop.action_items_count = 3
    db.session.commit()

    c.post(f'/proposal/{prop.id}/submit-to-customer', data={})
    db.session.refresh(prop)
    test("submit blocked while action items open", prop.review_status == 'internally_approved')
    c.post(f'/proposal/{prop.id}/share', data={})
    test("share blocked while action items open",
         ProposalShare.query.filter_by(proposal_id=prop.id).count() == 0)
    c.post(f'/proposal/{prop.id}/share', data={'ack_action_items': '1'})
    share = ProposalShare.query.filter_by(proposal_id=prop.id).first()
    test("acknowledged share goes through", share is not None)
    c.post(f'/proposal/{prop.id}/submit-to-customer', data={'ack_action_items': '1'})
    db.session.refresh(prop)
    test("acknowledged submit goes through",
         prop.review_status in ('submitted_to_customer',))

    print("\n=== Customer portal: PDF, first-view notify, typed acceptance ===")
    r = c.get(f'/p/{share.token}')
    test("portal renders with download link", b'Download PDF' in r.data)
    db.session.refresh(share)
    test("first view notifies the owner",
         share.view_count == 1 and Notification.query.filter_by(
             user_id=zoe.id, category='share_viewed').count() == 1)
    c.get(f'/p/{share.token}')  # rapid re-view dedupes
    test("rapid re-views don't re-notify",
         Notification.query.filter_by(user_id=zoe.id, category='share_viewed').count() == 1)

    r = c.get(f'/p/{share.token}/download.pdf')
    test("portal PDF downloads", r.status_code == 200
         and r.data[:4] == b'%PDF', f"status={r.status_code}")

    c.post(f'/p/{share.token}/decision', data={'decision': 'accepted'})
    db.session.refresh(share)
    test("acceptance requires a typed name", share.decision == '')
    c.post(f'/p/{share.token}/decision', data={
        'decision': 'accepted', 'decider_name': 'Pat Buyer',
        'decider_title': 'VP Procurement', 'note': 'Looks great'})
    db.session.refresh(share)
    test("typed-name acceptance recorded",
         share.decision == 'accepted' and share.decided_by_name == 'Pat Buyer'
         and share.decided_by_title == 'VP Procurement')
    n = Notification.query.filter_by(user_id=zoe.id, category='customer_decision').first()
    test("owner notified with the signer's name",
         n is not None and 'Pat Buyer' in (n.message or ''))

    print("\n=== Small fixes ===")
    mc = appmod.money_compact_filter
    test("money_compact humanizes values",
         mc(850) == '$850' and mc(45000) == '$45k' and mc(1200000) == '$1.2M'
         and mc(2500) == '$2.5k', f"{mc(850)}/{mc(45000)}/{mc(1200000)}/{mc(2500)}")

    ci = ClarificationItem(project_id=proj.id, question='Q?', status='open')
    db.session.add(ci); db.session.commit()
    r = c.post(f'/clarifications/{ci.id}/update', data={'confidence_impact': 'not-a-number'})
    test("bad confidence_impact no longer 500s", r.status_code in (200, 302))

    eq = EquipmentItem(user_id=zoe.id, org_id=zoe.org_id, item_name='PLC', unit_cost=100.0)
    tr = TravelExpenseRate(user_id=zoe.id, org_id=zoe.org_id, expense_type='Hotel', rate=150.0)
    db.session.add_all([eq, tr]); db.session.commit()
    c.post(f'/settings/edit-equipment-item/{eq.id}',
           data={'item_name': 'PLC M580', 'unit_cost': '4,500.00', 'unit': 'each'})
    db.session.refresh(eq)
    test("equipment inline edit works", eq.item_name == 'PLC M580' and eq.unit_cost == 4500.0)
    c.post(f'/settings/edit-travel-rate/{tr.id}',
           data={'expense_type': 'Hotel (GSA)', 'travel_rate': '189'})
    db.session.refresh(tr)
    test("travel inline edit works", tr.expense_type == 'Hotel (GSA)' and tr.rate == 189.0)

    # Cross-org isolation on the new edit routes
    c.post('/logout')
    c.post('/signup', data={'username': 'rival', 'email': 'rival@other.com',
                            'password': 'password123', 'company_name': 'OtherCo'})
    r = c.post(f'/settings/edit-equipment-item/{eq.id}', data={'item_name': 'HACKED'})
    db.session.refresh(eq)
    test("foreign org can't edit equipment", r.status_code == 404 and eq.item_name == 'PLC M580')

    print("\n=== Wizard 'Invite your team' step ===")
    prog_admin = appmod._setup_progress(zoe)
    keys = [s['key'] for s in prog_admin['steps']]
    test("admin wizard includes the team step", 'team' in keys)
    team_step = next(s for s in prog_admin['steps'] if s['key'] == 'team')
    test("team step pending while solo", team_step['done'] is False)

    member = User(username='mem', email='mem@corp.com', org_id=zoe.org_id, role='proposal')
    member.set_password('password123'); db.session.add(member); db.session.commit()
    prog_admin = appmod._setup_progress(zoe)
    team_step = next(s for s in prog_admin['steps'] if s['key'] == 'team')
    test("team step done once a teammate exists", team_step['done'] is True)
    prog_member = appmod._setup_progress(member)
    test("non-admin wizard hides the team step",
         'team' not in [s['key'] for s in prog_member['steps']])

print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
sys.exit(0 if failed == 0 else 1)

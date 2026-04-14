"""Tests for Part 3: Multi-Stakeholder Review & Revision Workflow.

These tests exercise the lifecycle state machine, approval rollup, revision
request filing, batch apply with AI (mocked), customer feedback flow, and
cross-user access protection.
"""
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'

from app import app, db
from models import (
    User, Project, ProjectDocument, Proposal, ProposalVersion,
    ProposalReviewer, ProposalApproval, RevisionRequest,
    ProposalRevisionBatch, ProposalStatusHistory, RevisionTemplate,
    Notification,
)
from proposal_export import markdown_to_docx
from proposal_lifecycle import (
    LifecycleError,
    STATES,
    approval_state,
    auto_advance_after_decision,
    can_transition,
    latest_version,
    pending_requests,
    transition as lifecycle_transition,
)
from proposal_agent import _parse_revision_response

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = 0
failed = 0


def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name} - {detail}")


def make_user(username, email=None, admin=False):
    """Create a user directly without going through signup (signup is routed)."""
    u = User(
        username=username,
        email=email or f"{username}@test.com",
        display_name=username.title(),
        company_name="Test Corp",
        is_admin=admin,
        role="admin" if admin else "proposal",
    )
    u.set_password("testpass123")
    db.session.add(u)
    db.session.commit()
    return u


def login_as(client, username):
    client.post('/login', data={'username': username, 'password': 'testpass123'})


def make_project_with_proposal(owner, name="Test Proj", md="# Test Proposal\n\n## Scope\n\nContent."):
    """Bypass the AI: create a project, a proposal, and a v1 directly."""
    gen_dir = Path(__file__).resolve().parent / 'generated_proposals'
    gen_dir.mkdir(exist_ok=True)

    proj = Project(user_id=owner.id, name=name, client_name="ACME")
    db.session.add(proj)
    db.session.flush()

    md_file = f"proposal_{uuid.uuid4().hex[:8]}.md"
    (gen_dir / md_file).write_text(md)
    docx_file = md_file.replace('.md', '.docx')
    markdown_to_docx(md, str(gen_dir / docx_file))

    prop = Proposal(
        project_id=proj.id,
        job_id=f"test_{uuid.uuid4().hex[:8]}",
        document_type='RFP',
        vertical='general',
        vertical_label='General',
        confidence_score=85,
        md_file=md_file,
        docx_file=docx_file,
        review_status="draft",
    )
    db.session.add(prop)
    db.session.flush()

    db.session.add(ProposalStatusHistory(
        proposal_id=prop.id,
        from_status="",
        to_status="draft",
        actor_id=owner.id,
        note="AI-generated v1.",
    ))
    v1 = ProposalVersion(
        proposal_id=prop.id,
        version_number=1,
        markdown_content=md,
        edit_source='ai',
        change_summary='AI-generated original',
    )
    db.session.add(v1)
    db.session.commit()
    return proj, prop, v1


def fake_revise_proposal(current_markdown, revision_requests, **kwargs):
    """Mock the AI revision — append a marker line per request, no network."""
    lines = [current_markdown, "", "## Applied Revisions"]
    for i, r in enumerate(revision_requests, 1):
        lines.append(f"- [{r['category']}] {r['directive']}")
    return {
        "revised_markdown": "\n".join(lines),
        "change_log": [
            {"request_index": i + 1, "applied": True, "action": f"Applied: {r['directive'][:40]}"}
            for i, r in enumerate(revision_requests)
        ],
        "ai_summary": f"Applied {len(revision_requests)} revision request(s).",
    }


def fake_parse_customer_email(email_text, **kwargs):
    """Mock the email parser — returns hard-coded draft requests."""
    return [
        {"directive": "Lower T&M rate by 10%", "category": "pricing", "target_section": "Pricing"},
        {"directive": "Add 6-month warranty", "category": "terms", "target_section": ""},
    ]


def fake_preflight(markdown_content, **kwargs):
    return {"action_items": [], "warnings": [], "ready": True}


with app.app_context():
    db.drop_all()
    db.create_all()

    # =========================================================================
    # 1. Lifecycle state machine
    # =========================================================================
    print("\n=== Lifecycle State Machine ===")

    test("can_transition draft->in_review", can_transition("draft", "in_review"))
    test("can_transition in_review->revision_requested", can_transition("in_review", "revision_requested"))
    test("can_transition revision_requested->in_review", can_transition("revision_requested", "in_review"))
    test("can_transition in_review->internally_approved", can_transition("in_review", "internally_approved"))
    test("can_transition internally_approved->submitted_to_customer", can_transition("internally_approved", "submitted_to_customer"))
    test("can_transition submitted_to_customer->customer_approved", can_transition("submitted_to_customer", "customer_approved"))
    test("can_transition customer_approved->won", can_transition("customer_approved", "won"))
    test("can_transition customer_declined->lost", can_transition("customer_declined", "lost"))
    test("can_transition same-state no-op", can_transition("draft", "draft"))

    # Illegal transitions
    test("cannot draft->won", not can_transition("draft", "won"))
    test("cannot won->anything", not can_transition("won", "draft"))
    test("cannot lost->anything", not can_transition("lost", "won"))
    test("cannot draft->customer_approved", not can_transition("draft", "customer_approved"))
    test("cannot in_review->won", not can_transition("in_review", "won"))

    # =========================================================================
    # 2. DB setup — users
    # =========================================================================
    print("\n=== Users & Setup ===")
    owner = make_user("owner")
    engineer = make_user("engineer")
    accountant = make_user("accountant")
    sales_lead = make_user("saleslead")
    bystander = make_user("bystander")
    test("Users created", User.query.count() == 5)

    client = app.test_client()
    login_as(client, "owner")

    proj, prop, v1 = make_project_with_proposal(owner)
    test("Proposal created in draft", prop.review_status == "draft")
    test("v1 exists", v1.version_number == 1)
    test("Initial status history entry", ProposalStatusHistory.query.filter_by(proposal_id=prop.id).count() == 1)

    # =========================================================================
    # 3. transition() + status history
    # =========================================================================
    print("\n=== transition() + history ===")
    try:
        lifecycle_transition(prop, "won", owner.id)
        db.session.rollback()
        test("Illegal transition raises", False)
    except LifecycleError:
        db.session.rollback()
        test("Illegal transition raises", True)

    # Valid: draft -> in_review
    lifecycle_transition(prop, "in_review", owner.id, note="Manual test")
    db.session.commit()
    test("draft->in_review applied", prop.review_status == "in_review")
    test("History entry written", ProposalStatusHistory.query.filter_by(
        proposal_id=prop.id, to_status="in_review"
    ).count() == 1)

    # Reset back for route-level tests
    prop.review_status = "draft"
    ProposalStatusHistory.query.filter_by(proposal_id=prop.id).delete()
    db.session.add(ProposalStatusHistory(
        proposal_id=prop.id, from_status="", to_status="draft", actor_id=owner.id,
    ))
    db.session.commit()

    # =========================================================================
    # 4. Send for Review route
    # =========================================================================
    print("\n=== Send for Review route ===")
    resp = client.get(f'/proposal/{prop.id}/send-for-review')
    test("Send-for-review page loads", resp.status_code == 200)
    test("Shows reviewer picker", b'reviewer_user_id' in resp.data)

    # Try to send with only the owner as a reviewer -> must fail
    resp = client.post(
        f'/proposal/{prop.id}/send-for-review',
        data={
            'reviewer_user_id': [owner.id],
            'reviewer_role': ['engineering'],
            'required_0': ['1'],
        },
        follow_redirects=True,
    )
    test("Owner-only reviewer rejected", prop.review_status == "draft")

    # Now assign two other users
    resp = client.post(
        f'/proposal/{prop.id}/send-for-review',
        data={
            'reviewer_user_id': [engineer.id, accountant.id],
            'reviewer_role': ['engineering', 'accounting'],
            'required_0': '1',
            'required_1': '1',
            'review_note': 'Focus on pricing',
        },
        follow_redirects=True,
    )
    db.session.refresh(prop)
    test("Send-for-review response 200", resp.status_code == 200)
    test("Status moved to in_review", prop.review_status == "in_review")
    test("2 reviewers created", ProposalReviewer.query.filter_by(proposal_id=prop.id).count() == 2)
    test("Engineer notified", Notification.query.filter_by(
        user_id=engineer.id, category="review_assigned"
    ).count() == 1)
    test("Accountant notified", Notification.query.filter_by(
        user_id=accountant.id, category="review_assigned"
    ).count() == 1)

    # =========================================================================
    # 5. Reviewer files a revision request
    # =========================================================================
    print("\n=== Revision Request filing ===")
    client.get('/logout')
    login_as(client, "engineer")

    # Engineer can view the proposal page now
    resp = client.get(f'/proposal/{prop.id}')
    test("Reviewer can view proposal", resp.status_code == 200)

    # Bystander (non-reviewer, non-owner) cannot
    client.get('/logout')
    login_as(client, "bystander")
    resp = client.get(f'/proposal/{prop.id}')
    test("Bystander blocked from viewing", resp.status_code == 404)

    client.get('/logout')
    login_as(client, "engineer")

    # Engineer files a revision request
    resp = client.post(
        f'/proposal/{prop.id}/revision-request',
        data={
            'category': 'resources',
            'directive': 'Add two Controls Engineers at $175/hr.',
            'target_section': 'Staffing Plan',
        },
        follow_redirects=True,
    )
    test("Filing request returns 200", resp.status_code == 200)
    req = RevisionRequest.query.filter_by(proposal_id=prop.id).first()
    test("Request saved", req is not None)
    test("Request source is internal_engineering", req.source == "internal_engineering")
    test("Request status pending", req.status == "pending")
    test("Proposal auto-transitioned to revision_requested",
         db.session.get(Proposal, prop.id).review_status == "revision_requested")
    test("Owner got notification", Notification.query.filter_by(
        user_id=owner.id, category="revision_requested"
    ).count() >= 1)

    # Engineer files a second request
    client.post(
        f'/proposal/{prop.id}/revision-request',
        data={'category': 'scope', 'directive': 'Clarify deliverable format.'},
        follow_redirects=True,
    )
    test("Second request saved", RevisionRequest.query.filter_by(proposal_id=prop.id).count() == 2)

    # Withdraw one
    pending = RevisionRequest.query.filter_by(proposal_id=prop.id, status="pending").first()
    resp = client.post(
        f'/proposal/{prop.id}/revision-request/{pending.id}/withdraw',
        follow_redirects=True,
    )
    test("Withdraw returns 200", resp.status_code == 200)
    db.session.refresh(pending)
    test("Withdrawn request marked", pending.status == "withdrawn")

    # =========================================================================
    # 6. Approval decisions
    # =========================================================================
    print("\n=== Approval rollup ===")
    state = approval_state(prop)
    test("2 required reviewers", state["required_count"] == 2)
    test("0 approved initially", state["approved_count"] == 0)
    test("2 pending initially", state["pending_count"] == 2)

    # Engineer tries to approve their own request (valid — they're not the owner)
    resp = client.post(
        f'/proposal/{prop.id}/approve',
        data={'decision': 'approved'},
        follow_redirects=True,
    )
    test("Engineer approval returns 200", resp.status_code == 200)
    state = approval_state(db.session.get(Proposal, prop.id))
    test("1 approved, 1 pending", state["approved_count"] == 1 and state["pending_count"] == 1)
    test("Not yet fully approved", not state["all_approved"])

    # Owner cannot approve their own proposal
    client.get('/logout')
    login_as(client, "owner")
    # Add the owner as a reviewer first (for this test)
    db.session.add(ProposalReviewer(
        proposal_id=prop.id, user_id=owner.id, review_role="sales", is_required=False,
    ))
    db.session.commit()
    resp = client.post(
        f'/proposal/{prop.id}/approve',
        data={'decision': 'approved'},
        follow_redirects=True,
    )
    test("Owner cannot self-approve", ProposalApproval.query.filter_by(
        proposal_id=prop.id, user_id=owner.id
    ).count() == 0)

    # Remove owner reviewer row for the rest of the test
    ProposalReviewer.query.filter_by(proposal_id=prop.id, user_id=owner.id).delete()
    db.session.commit()

    # Accountant approves — all required should now be approved
    client.get('/logout')
    login_as(client, "accountant")
    resp = client.post(
        f'/proposal/{prop.id}/approve',
        data={'decision': 'approved'},
        follow_redirects=True,
    )
    db.session.refresh(prop)
    test("After 2nd approval, internally_approved", prop.review_status == "internally_approved")

    state = approval_state(prop)
    test("all_approved True", state["all_approved"])

    # =========================================================================
    # 7. Re-file a change request from accountant against v1 — state should
    #    roll back (not supported currently — approvals don't unwind).
    #    Instead, test that a NEW version invalidates all prior approvals.
    # =========================================================================
    print("\n=== Apply feedback & AI mock ===")
    # Add a pending revision request from accountant
    client.get('/logout')
    login_as(client, "accountant")
    client.post(
        f'/proposal/{prop.id}/revision-request',
        data={'category': 'pricing', 'directive': 'Bump buyout margin 1%.'},
        follow_redirects=True,
    )

    pending_ids = [r.id for r in pending_requests(prop.id)]
    # Engineer has 1 remaining pending (1 was withdrawn) + 1 from accountant = 2
    test("2 pending requests for batch", len(pending_ids) == 2)

    # Owner-edited directive must target a real request id we're sending
    edit_target = pending_ids[0]

    # Switch back to owner and apply feedback (mock AI)
    client.get('/logout')
    login_as(client, "owner")

    with patch('app.revise_proposal', side_effect=fake_revise_proposal):
        resp = client.post(
            f'/proposal/{prop.id}/apply-feedback',
            data={
                'apply_request_id': pending_ids,
                f'directive_{edit_target}': 'Bump buyout margin 1% (edited by owner)',
            },
            follow_redirects=True,
        )
    test("Apply feedback returns 200", resp.status_code == 200)

    versions = ProposalVersion.query.filter_by(proposal_id=prop.id).order_by(
        ProposalVersion.version_number
    ).all()
    test("v2 was created", len(versions) == 2 and versions[-1].version_number == 2)
    test("v2 is AI source", versions[-1].edit_source == "ai")
    test("v2 contains applied directive",
         "edited by owner" in versions[-1].markdown_content)

    # Batch log
    batch = ProposalRevisionBatch.query.filter_by(proposal_id=prop.id).first()
    test("Revision batch logged", batch is not None)
    test("Batch request_count == 2", batch and batch.request_count == 2)

    # Both requests marked applied
    applied_count = RevisionRequest.query.filter_by(
        proposal_id=prop.id, status="applied"
    ).count()
    test("Both requests marked applied", applied_count == 2)
    applied_one = db.session.get(RevisionRequest, edit_target)
    test("Request linked to new version",
         applied_one.applied_in_version_id == versions[-1].id)

    # Approvals on v1 should NOT carry over to v2
    state = approval_state(db.session.get(Proposal, prop.id))
    test("v2 has 0 approvals", state["approved_count"] == 0)
    test("Back to pending (2 reviewers)", state["pending_count"] == 2)

    # Status moved back to in_review
    db.session.refresh(prop)
    test("Status returned to in_review", prop.review_status == "in_review")

    # Reviewers notified
    test("Engineer notified of new version", Notification.query.filter_by(
        user_id=engineer.id, category="review_assigned"
    ).count() >= 2)

    # =========================================================================
    # 8. Second round of approvals on v2
    # =========================================================================
    print("\n=== v2 approvals ===")
    client.get('/logout')
    login_as(client, "engineer")
    client.post(f'/proposal/{prop.id}/approve', data={'decision': 'approved'})
    client.get('/logout')
    login_as(client, "accountant")
    client.post(f'/proposal/{prop.id}/approve', data={'decision': 'approved'})
    db.session.refresh(prop)
    test("v2 fully approved -> internally_approved", prop.review_status == "internally_approved")

    # =========================================================================
    # 9. Submit to customer
    # =========================================================================
    print("\n=== Submit to customer ===")
    client.get('/logout')
    login_as(client, "owner")
    resp = client.post(f'/proposal/{prop.id}/submit-to-customer', follow_redirects=True)
    db.session.refresh(prop)
    test("Submit to customer OK", prop.review_status == "submitted_to_customer")
    # Project status should sync to submitted
    db.session.refresh(proj)
    test("Project status synced to submitted", proj.status == "submitted")

    # =========================================================================
    # 10. Customer feedback (AI email parse mocked)
    # =========================================================================
    print("\n=== Customer feedback flow ===")
    with patch('app.parse_customer_email', side_effect=fake_parse_customer_email):
        resp = client.post(
            f'/proposal/{prop.id}/customer-feedback',
            data={
                'mode': 'parse_email',
                'email_text': "Hi, please lower the T&M rate by 10% and add a 6-month warranty. Thanks!",
            },
            follow_redirects=True,
        )
    test("Parse email returns 200", resp.status_code == 200)
    customer_reqs = RevisionRequest.query.filter_by(
        proposal_id=prop.id, source="customer"
    ).all()
    test("2 customer requests created", len(customer_reqs) == 2)
    test("Customer request pricing present", any(r.category == "pricing" for r in customer_reqs))
    test("Customer request terms present", any(r.category == "terms" for r in customer_reqs))

    db.session.refresh(prop)
    test("Status customer_feedback", prop.review_status == "customer_feedback")

    # Manual-entry customer feedback
    client.post(
        f'/proposal/{prop.id}/customer-feedback',
        data={
            'mode': 'manual',
            'category': ['scope', 'schedule'],
            'target_section': ['Scope', ''],
            'directive': ['Remove phase 2', 'Extend timeline by 2 weeks'],
        },
        follow_redirects=True,
    )
    test("Manual customer entries saved", RevisionRequest.query.filter_by(
        proposal_id=prop.id, source="customer"
    ).count() == 4)

    # Apply customer feedback (mock AI again)
    customer_pending = [r.id for r in pending_requests(prop.id)]
    test("4 pending after customer round", len(customer_pending) == 4)

    with patch('app.revise_proposal', side_effect=fake_revise_proposal):
        resp = client.post(
            f'/proposal/{prop.id}/apply-feedback',
            data={'apply_request_id': customer_pending},
            follow_redirects=True,
        )
    test("Apply customer feedback OK", resp.status_code == 200)
    versions = ProposalVersion.query.filter_by(proposal_id=prop.id).count()
    test("v3 created", versions == 3)

    # =========================================================================
    # 11. Customer accepted / declined -> won / lost
    # =========================================================================
    print("\n=== Customer decision ===")
    # Move back to submitted
    # (current status is in_review after apply_feedback)
    db.session.refresh(prop)
    # Re-approve v3 quickly and submit again
    client.get('/logout')
    login_as(client, "engineer")
    client.post(f'/proposal/{prop.id}/approve', data={'decision': 'approved'})
    client.get('/logout')
    login_as(client, "accountant")
    client.post(f'/proposal/{prop.id}/approve', data={'decision': 'approved'})
    db.session.refresh(prop)
    client.get('/logout')
    login_as(client, "owner")
    client.post(f'/proposal/{prop.id}/submit-to-customer')
    db.session.refresh(prop)
    test("Resubmitted to customer", prop.review_status == "submitted_to_customer")

    # Accept
    resp = client.post(
        f'/proposal/{prop.id}/customer-decision',
        data={'decision': 'accepted', 'note': 'Customer loves it.'},
        follow_redirects=True,
    )
    db.session.refresh(prop)
    db.session.refresh(proj)
    test("Proposal won", prop.review_status == "won")
    test("Project status = won", proj.status == "won")

    # =========================================================================
    # 12. Revision templates
    # =========================================================================
    print("\n=== Revision templates (settings) ===")
    client.get('/logout')
    login_as(client, "owner")
    resp = client.post(
        '/settings/add-revision-template',
        data={
            'template_name': 'Bump margin 1%',
            'template_category': 'pricing',
            'template_directive': 'Increase all buyout margins by 1 percentage point.',
            'template_description': 'Quick margin bump',
        },
        follow_redirects=True,
    )
    test("Add template returns 200", resp.status_code == 200)
    test("Template created", RevisionTemplate.query.filter_by(user_id=owner.id).count() == 1)
    tmpl = RevisionTemplate.query.first()
    test("Template directive saved", 'percentage point' in tmpl.directive_template)

    # Shows up in settings page
    resp = client.get('/settings')
    test("Settings shows revision template", b'Bump margin 1%' in resp.data)

    # Delete
    resp = client.post(f'/settings/delete-revision-template/{tmpl.id}', follow_redirects=True)
    test("Delete template 200", resp.status_code == 200)
    test("Template gone", RevisionTemplate.query.count() == 0)

    # Cross-user: user2 can't delete user1's template
    t2 = RevisionTemplate(
        user_id=owner.id, name='Preset', category='scope',
        directive_template='Reduce scope.',
    )
    db.session.add(t2)
    db.session.commit()
    client.get('/logout')
    login_as(client, "engineer")
    resp = client.post(f'/settings/delete-revision-template/{t2.id}')
    test("Cross-user template delete blocked", resp.status_code == 404)

    # =========================================================================
    # 13. Dashboard widgets
    # =========================================================================
    print("\n=== Dashboard widgets ===")

    # Set up a new proposal for the engineer to review (fresh state)
    client.get('/logout')
    login_as(client, "owner")
    proj2, prop2, v1b = make_project_with_proposal(owner, name="Proj 2")
    client.post(
        f'/proposal/{prop2.id}/send-for-review',
        data={
            'reviewer_user_id': [engineer.id],
            'reviewer_role': ['engineering'],
            'required_0': '1',
        },
    )

    # Owner dashboard shows "Out for Review"
    resp = client.get('/')
    test("Owner dashboard shows Out for Review", b'Out for Review' in resp.data)
    test("Owner dashboard lists Proj 2", b'Proj 2' in resp.data)

    # Engineer dashboard shows "Pending My Review"
    client.get('/logout')
    login_as(client, "engineer")
    resp = client.get('/')
    test("Engineer dashboard shows Pending My Review", b'Pending My Review' in resp.data)
    test("Engineer dashboard lists Proj 2", b'Proj 2' in resp.data)

    # =========================================================================
    # 14. Cross-user protection on review routes
    # =========================================================================
    print("\n=== Cross-user protection ===")
    client.get('/logout')
    login_as(client, "bystander")

    resp = client.get(f'/proposal/{prop2.id}/send-for-review')
    test("Bystander blocked from send-for-review", resp.status_code == 404)

    resp = client.get(f'/proposal/{prop2.id}/apply-feedback')
    test("Bystander blocked from apply-feedback", resp.status_code == 404)

    resp = client.post(f'/proposal/{prop2.id}/submit-to-customer')
    test("Bystander blocked from submit-to-customer", resp.status_code == 404)

    resp = client.get(f'/proposal/{prop2.id}/customer-feedback')
    test("Bystander blocked from customer-feedback", resp.status_code == 404)

    resp = client.post(
        f'/proposal/{prop2.id}/revision-request',
        data={'category': 'scope', 'directive': 'Hack.'},
    )
    test("Bystander can't file revision request", resp.status_code in (403, 404))

    # =========================================================================
    # 15. Parse revision response parser (unit-level)
    # =========================================================================
    print("\n=== _parse_revision_response parser ===")

    text = """# Revised Proposal

## Pricing
New content here.

=====CHANGE_LOG=====
[
  {"request_index": 1, "applied": true, "action": "Bumped margin"},
  {"request_index": 2, "applied": false, "reason": "Conflict"}
]
Applied 1 of 2 revision requests."""
    result = _parse_revision_response(text, [{"directive": "x"}, {"directive": "y"}])
    test("Parser extracts revised markdown", "Revised Proposal" in result["revised_markdown"])
    test("Parser strips change log marker", "=====" not in result["revised_markdown"])
    test("Parser extracts change log", len(result["change_log"]) == 2)
    test("Parser reads applied flag", result["change_log"][0]["applied"] is True)
    test("Parser reads ai_summary", "Applied 1 of 2" in result["ai_summary"])

    # Fallback when no marker is present
    result2 = _parse_revision_response("just markdown", [{"directive": "z"}])
    test("Parser fallback still returns markdown", "just markdown" in result2["revised_markdown"])
    test("Parser fallback generates change log", len(result2["change_log"]) == 1)

    # =========================================================================
    # 16. approval_state edge cases
    # =========================================================================
    print("\n=== approval_state edge cases ===")
    # Proposal with no reviewers
    empty_proj, empty_prop, _ = make_project_with_proposal(owner, name="Empty Proj")
    state = approval_state(empty_prop)
    test("Empty: 0 required", state["required_count"] == 0)
    test("Empty: not all_approved", not state["all_approved"])

    # =========================================================================
    # 17. Project status one-directional sync safety
    # =========================================================================
    print("\n=== Project status sync ===")
    # A won project should NOT revert to submitted when its proposal is resent
    db.session.refresh(proj)
    assert proj.status == "won"
    # Force the proposal lifecycle via direct state (not a valid route transition,
    # but tests the guard in _sync_project_status).
    from proposal_lifecycle import _sync_project_status
    _sync_project_status(proj, "submitted_to_customer")
    test("Won project stays won", proj.status == "won")

    # =========================================================================
    # SUMMARY
    # =========================================================================
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
    if failed:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")

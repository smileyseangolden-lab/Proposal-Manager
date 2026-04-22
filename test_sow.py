"""Tests for the Scope of Work (SOW) feature.

Exercises: model shape, generate flow (with AI mocked), edit+save versioning,
approval lock, reopen, delete, stamping the Proposal with sow_id/sow_version_id,
the draft-blocks-proposal-generation rule, the explicit ignore-SOW escape
hatch, and the include-SOW-in-deliverable download bundling.
"""

import os
import uuid
from pathlib import Path
from unittest.mock import patch

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'

from app import app, db
from models import (
    User, Project, ProjectDocument, Proposal, ProposalVersion,
    ScopeOfWork, ScopeOfWorkVersion,
)
from proposal_agent import (
    _parse_sow_response,
    assemble_sow_markdown,
    sow_items_to_markdown,
    sow_markdown_to_items,
)

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


def make_project_with_rfp(owner, rfp_text="This is a sample RFP for controls work."):
    proj = Project(user_id=owner.id, name="SOW Test Proj", client_name="ACME")
    db.session.add(proj)
    db.session.flush()

    upload_dir = Path(__file__).resolve().parent / 'uploads' / 'test_sow'
    upload_dir.mkdir(parents=True, exist_ok=True)
    rfp_path = upload_dir / f"rfp_{uuid.uuid4().hex[:6]}.txt"
    rfp_path.write_text(rfp_text)

    doc = ProjectDocument(
        project_id=proj.id,
        filename=rfp_path.name,
        original_filename=rfp_path.name,
        file_type="rfp",
        file_path=str(rfp_path),
        file_size=rfp_path.stat().st_size,
    )
    db.session.add(doc)
    db.session.commit()
    return proj, doc


def fake_generate_sow(rfp_text, **kwargs):
    return {
        "in_scope": [
            {"text": "Install PLC control panel", "rfp_reference": "Section 3.1"},
            {"text": "Commission BMS integration", "rfp_reference": "Section 4.2"},
        ],
        "out_of_scope": [
            {"text": "Structural steel erection", "rfp_reference": "Section 2.5"},
            {"text": "Process mechanical piping (by others)", "rfp_reference": ""},
        ],
        "assumptions": [
            {"text": "Site power available before commissioning"},
            {"text": "Owner-furnished instrumentation per Exhibit B"},
        ],
        "raw": '{"in_scope":[...]}',
        "vertical": "general",
        "vertical_label": "General",
        "model": "claude-opus-4-6",
        "generated_at": "2026-04-22T10:00:00+00:00",
        "drawings_count": 0,
    }


def fake_generate_proposal_capturing(capture):
    def _fake(rfp_text, **kwargs):
        capture["called"] = True
        capture["approved_sow"] = kwargs.get("approved_sow", "")
        return {
            "proposal_markdown": "# Proposal\n\n## Pricing\n\n[ACTION REQUIRED: fill in]",
            "action_items": ["fill in"],
            "confidence_score": 80,
            "document_type": "RFP",
            "vertical": "general",
            "vertical_label": "General",
            "generated_at": "2026-04-22T10:05:00+00:00",
        }
    return _fake


with app.app_context():
    db.drop_all()
    db.create_all()

    # =========================================================================
    # 1. Pure function layer: parse/serialize helpers
    # =========================================================================
    print("\n=== Parse / Serialize Helpers ===")

    raw_json = (
        '{"in_scope": [{"text": "A", "rfp_reference": "1.1"}], '
        '"out_of_scope": [{"text": "B"}], '
        '"assumptions": [{"text": "C"}]}'
    )
    parsed = _parse_sow_response(raw_json)
    test("Parses in_scope", len(parsed["in_scope"]) == 1 and parsed["in_scope"][0]["text"] == "A")
    test("In-scope keeps ref", parsed["in_scope"][0]["rfp_reference"] == "1.1")
    test("Parses out_of_scope", len(parsed["out_of_scope"]) == 1)
    test("Parses assumptions", parsed["assumptions"][0]["text"] == "C")

    fenced = f"Here you go:\n```json\n{raw_json}\n```"
    parsed2 = _parse_sow_response(fenced)
    test("Parses JSON inside code fence", len(parsed2["in_scope"]) == 1)

    garbage = _parse_sow_response("this is not JSON")
    test("Graceful on non-JSON", garbage == {"in_scope": [], "out_of_scope": [], "assumptions": []})

    md = sow_items_to_markdown([{"text": "Foo", "rfp_reference": "1.2"}, {"text": "Bar"}])
    test("Markdown render has bullet", md.startswith("- Foo"))
    test("Markdown render includes ref", "(ref: 1.2)" in md)
    items = sow_markdown_to_items(md)
    test("Roundtrip preserves count", len(items) == 2)
    test("Roundtrip keeps ref", items[0]["rfp_reference"] == "1.2")
    test("Roundtrip drops ref when absent", items[1]["rfp_reference"] == "")

    assembled = assemble_sow_markdown("- a", "- b", "- c")
    test("Assembled has In Scope heading", "## In Scope" in assembled)
    test("Assembled has Out of Scope heading", "## Out of Scope" in assembled)
    test("Assembled has Assumptions heading", "## Assumptions" in assembled)

    # =========================================================================
    # 2. Route + model integration
    # =========================================================================
    print("\n=== Users & Setup ===")
    owner = make_user("sowowner")
    admin = make_user("sowadmin", admin=True)
    other = make_user("sowother")

    client = app.test_client()
    login_as(client, "sowowner")

    proj, rfp_doc = make_project_with_rfp(owner)
    test("Project + RFP created", proj is not None and rfp_doc.file_type == "rfp")
    test("No SOW initially", proj.sow is None)

    # =========================================================================
    # 3. Generate SOW (AI mocked)
    # =========================================================================
    print("\n=== SOW generate route ===")
    with patch('app.generate_sow', side_effect=fake_generate_sow):
        resp = client.post(f'/projects/{proj.id}/sow/generate', data={
            'vertical': 'auto',
        }, follow_redirects=False)
    test("Generate redirects to editor", resp.status_code == 302 and '/sow' in resp.headers.get('Location', ''))

    db.session.expire_all()
    proj = db.session.get(Project, proj.id)
    sow = proj.sow
    test("SOW created", sow is not None)
    test("SOW status=draft", sow.status == "draft")
    test("SOW not locked", sow.locked is False)
    test("SOW has in-scope content", "PLC control panel" in (sow.in_scope_md or ""))
    test("SOW has out-of-scope content", "Structural steel" in (sow.out_of_scope_md or ""))
    test("SOW has assumptions content", "Site power" in (sow.assumptions_md or ""))
    test("v1 created", ScopeOfWorkVersion.query.filter_by(sow_id=sow.id, version_number=1).count() == 1)

    # =========================================================================
    # 4. Edit+save creates v2
    # =========================================================================
    print("\n=== SOW edit + save ===")
    resp = client.get(f'/projects/{proj.id}/sow')
    test("Editor page loads", resp.status_code == 200)
    test("Editor shows in-scope", b"PLC control panel" in resp.data)

    resp = client.post(f'/projects/{proj.id}/sow/save', data={
        'in_scope_md': "- Install PLC control panel\n- Added by user",
        'out_of_scope_md': sow.out_of_scope_md,
        'assumptions_md': sow.assumptions_md,
    }, follow_redirects=False)
    test("Save redirects", resp.status_code == 302)
    db.session.expire_all()
    sow = db.session.get(ScopeOfWork, sow.id)
    test("Edit persisted", "Added by user" in sow.in_scope_md)
    test("v2 snapshot exists", ScopeOfWorkVersion.query.filter_by(sow_id=sow.id, version_number=2).count() == 1)
    v2 = ScopeOfWorkVersion.query.filter_by(sow_id=sow.id, version_number=2).first()
    test("v2 edit_source=human", v2.edit_source == "human")
    test("v2 editor_id set", v2.editor_id == owner.id)

    # =========================================================================
    # 5. Approval: owner can; non-owner non-admin cannot
    # =========================================================================
    print("\n=== SOW approval ===")
    # Other user cannot even access the project (not assigned, not admin)
    client.get('/logout')
    login_as(client, "sowother")
    resp = client.post(f'/projects/{proj.id}/sow/approve', follow_redirects=False)
    test("Other user blocked from approve (404)", resp.status_code == 404)

    # Admin can approve even though not the owner
    client.get('/logout')
    login_as(client, "sowadmin")
    resp = client.post(f'/projects/{proj.id}/sow/approve', follow_redirects=False)
    db.session.expire_all()
    sow = db.session.get(ScopeOfWork, sow.id)
    test("Admin-on-behalf approval succeeds", sow.status == "approved")
    test("SOW is locked after approval", sow.locked is True)
    test("approved_by records admin", sow.approved_by_user_id == admin.id)

    # Owner tries to save while locked
    client.get('/logout')
    login_as(client, "sowowner")
    before_in_scope = sow.in_scope_md
    resp = client.post(f'/projects/{proj.id}/sow/save', data={
        'in_scope_md': "- Tampering attempt",
        'out_of_scope_md': "",
        'assumptions_md': "",
    }, follow_redirects=True)
    db.session.expire_all()
    sow = db.session.get(ScopeOfWork, sow.id)
    test("Save blocked while locked", sow.in_scope_md == before_in_scope)

    # =========================================================================
    # 6. Proposal generation consumes the approved SOW
    # =========================================================================
    print("\n=== Proposal generation with approved SOW ===")
    capture = {}
    with patch('app.generate_proposal', side_effect=fake_generate_proposal_capturing(capture)):
        resp = client.post(f'/projects/{proj.id}/generate', data={
            'vertical': 'auto',
            'output_format': 'docx',
        }, follow_redirects=False)

    test("Generate returns a redirect", resp.status_code == 302)
    test("generate_proposal was called", capture.get("called") is True)
    test("approved_sow was passed in", "PLC control panel" in (capture.get("approved_sow") or ""))
    test("approved_sow has three headings",
         all(h in (capture.get("approved_sow") or "")
             for h in ("## In Scope", "## Out of Scope", "## Assumptions")))

    prop = Proposal.query.filter_by(project_id=proj.id).first()
    test("Proposal row created", prop is not None)
    test("Proposal.sow_id set", prop.sow_id == sow.id)
    test("Proposal.sow_version_id set", prop.sow_version_id is not None)

    # =========================================================================
    # 7. Reopen marks the proposal stale (computed, via view_proposal render)
    # =========================================================================
    print("\n=== Reopen + stale flag ===")
    resp = client.post(f'/projects/{proj.id}/sow/reopen', follow_redirects=False)
    db.session.expire_all()
    sow = db.session.get(ScopeOfWork, sow.id)
    test("Reopen sets status=draft", sow.status == "draft")
    test("Reopen unlocks", sow.locked is False)

    resp = client.get(f'/proposal/{prop.id}')
    test("Proposal page loads", resp.status_code == 200)
    test("Stale banner shown", b"SOW has changed" in resp.data)

    # =========================================================================
    # 8. Draft SOW blocks proposal generation unless ignored
    # =========================================================================
    print("\n=== Draft blocks proposal generation ===")
    # Create a second project with a draft SOW
    proj2, _ = make_project_with_rfp(owner)
    with patch('app.generate_sow', side_effect=fake_generate_sow):
        client.post(f'/projects/{proj2.id}/sow/generate', data={'vertical': 'auto'})
    db.session.expire_all()
    proj2 = db.session.get(Project, proj2.id)
    test("Draft SOW exists on proj2", proj2.sow and proj2.sow.status == "draft")

    # Without ignore_sow: should be blocked (redirect back to upload; no Proposal created)
    before_count = Proposal.query.filter_by(project_id=proj2.id).count()
    with patch('app.generate_proposal', side_effect=fake_generate_proposal_capturing({})):
        resp = client.post(f'/projects/{proj2.id}/generate',
                           data={'vertical': 'auto', 'output_format': 'docx'},
                           follow_redirects=False)
    after_count = Proposal.query.filter_by(project_id=proj2.id).count()
    test("Draft SOW blocks proposal", after_count == before_count)

    # With ignore_sow: proposal is generated, sow_id is NULL
    capture2 = {}
    with patch('app.generate_proposal', side_effect=fake_generate_proposal_capturing(capture2)):
        resp = client.post(f'/projects/{proj2.id}/generate',
                           data={'vertical': 'auto', 'output_format': 'docx', 'ignore_sow': '1'},
                           follow_redirects=False)
    test("Ignore_sow allows generation", Proposal.query.filter_by(project_id=proj2.id).count() == before_count + 1)
    test("Ignore_sow sends empty approved_sow", (capture2.get("approved_sow") or "") == "")
    prop2 = Proposal.query.filter_by(project_id=proj2.id).order_by(Proposal.generated_at.desc()).first()
    test("Ignored path leaves sow_id NULL", prop2.sow_id is None)

    # =========================================================================
    # 9. Delete SOW
    # =========================================================================
    print("\n=== SOW delete ===")
    sow2_id = proj2.sow.id
    resp = client.post(f'/projects/{proj2.id}/sow/delete', follow_redirects=False)
    test("Delete redirects", resp.status_code == 302)
    test("SOW row gone", db.session.get(ScopeOfWork, sow2_id) is None)
    test("SOW versions gone", ScopeOfWorkVersion.query.filter_by(sow_id=sow2_id).count() == 0)

    # =========================================================================
    # 10. Include SOW in deliverable — download bundles them
    # =========================================================================
    print("\n=== Include SOW in deliverable ===")
    # Re-approve proj's SOW and regenerate a proposal with include_sow flag
    client.get('/logout')
    login_as(client, "sowowner")
    resp = client.post(f'/projects/{proj.id}/sow/approve', follow_redirects=False)
    db.session.expire_all()
    sow = db.session.get(ScopeOfWork, sow.id)
    test("Re-approved", sow.status == "approved")

    capture3 = {}
    with patch('app.generate_proposal', side_effect=fake_generate_proposal_capturing(capture3)):
        client.post(f'/projects/{proj.id}/generate',
                    data={
                        'vertical': 'auto',
                        'output_format': 'docx',
                        'include_sow_in_deliverable': '1',
                    }, follow_redirects=False)
    bundled_prop = Proposal.query.filter_by(project_id=proj.id).order_by(Proposal.generated_at.desc()).first()
    test("include_sow_in_deliverable stored", bundled_prop.include_sow_in_deliverable is True)

    resp = client.get(f'/download/{bundled_prop.id}/md')
    test("MD download succeeds", resp.status_code == 200)
    test("Bundled MD contains proposal", b"# Proposal" in resp.data)
    test("Bundled MD contains SOW", b"## In Scope" in resp.data and b"## Assumptions" in resp.data)

    # =========================================================================
    # Summary
    # =========================================================================
    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    if failed:
        raise SystemExit(1)

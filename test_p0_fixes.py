"""Regression tests for the five P0 audit fixes.

P0-1  Org templates load with Auto-Detect (vertical resolved BEFORE template
      queries, with a 'general' fallback tier).
P0-2  Answered clarification questions are fed back into regeneration, re-asked
      duplicates don't loop/duplicate rows, and question-only runs don't
      consume the monthly generation quota.
P0-3  Hosted (non-SELF_HOSTED) generations never inject the install-level
      sample boilerplate / reference proposals into the prompt.
P0-4  Proposal reads are DB-canonical (survive a missing/stale local file);
      edit writes mirror to object storage.
P0-5  The customer portal renders the version pinned at share time, not
      whatever is latest; re-sharing re-pins.

Standalone runner: python test_p0_fixes.py
"""
import io
import os
import sys
from unittest.mock import patch

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'
os.environ.pop('APP_ENV', None)
os.environ.pop('SELF_HOSTED', None)

import docx as _docx

import app as appmod
from app import app, db
import billing
import proposal_agent
from models import (Organization, Project, Proposal, ProposalQuestion,
                    ProposalShare, ProposalVersion, User)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = failed = 0


def test(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS: {name}")
    else:
        failed += 1; print(f"  FAIL: {name} - {detail}")


def make_docx_bytes(text):
    d = _docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    buf.seek(0)
    return buf


# Canned generation results -------------------------------------------------

QUESTION = {"question": "What margin target should be used?", "context": "",
            "resolution_path": "internal", "category": "pricing", "ai_suggestion": ""}

def result_with_questions(**kw):
    return {
        "proposal_markdown": "# Proposal V1MARKER\n\nConfidence Score: 80%",
        "action_items": [], "confidence_score": 80, "document_type": "RFP",
        "vertical": kw.get("vertical", "data_center"),
        "vertical_label": "Data Center / Mission Critical",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "questions": [dict(QUESTION)],
    }

def result_full(**kw):
    r = result_with_questions(**kw)
    return r  # same shape; caller decides whether questions halt


captured_calls = []

def fake_generate(rfp_text, **kwargs):
    captured_calls.append(kwargs)
    # First call: halt with a question. Second call: re-ask the SAME question
    # (worst case) alongside a full proposal — dedupe must fall through.
    return result_with_questions(vertical=kwargs.get("vertical"))


with app.app_context():
    db.drop_all(); db.create_all()
    c = app.test_client()

    # --- Workspace with a 'general' template, generating with Auto-Detect ---
    c.post('/signup', data={'username': 'pat', 'email': 'pat@a.com',
                            'password': 'password123', 'display_name': 'Pat',
                            'company_name': 'OrgA'})
    pat = User.query.filter_by(username='pat').first()

    r = c.post('/settings/upload-template', data={
        'template_name': 'House Style', 'vertical': 'general', 'template_type': 'proposal',
        'template_file': (make_docx_bytes('TEMPLATEMARKER structure'), 'house.docx'),
    }, content_type='multipart/form-data')
    test("template upload accepted", r.status_code == 302)

    r = c.post('/projects/new', data={'project_name': 'DC Bid', 'client_name': 'Acme'})
    project = Project.query.filter_by(name='DC Bid').first()
    test("project created", project is not None)

    rfp_text = ("Request for proposal: data center EPMS for a new data hall. "
                "Meter all switchgear and PDU equipment; integrate the EPMS "
                "with the colocation data center BMS.")
    c.post(f'/projects/{project.id}/upload', data={
        'file_type': 'rfp',
        'documents': (io.BytesIO(rfp_text.encode()), 'rfp.txt'),
    }, content_type='multipart/form-data')

    print("\n=== P0-1 + P0-2 + P0-3 (call-site): generation flow ===")
    with patch('app.generate_proposal', side_effect=fake_generate):
        r = c.post(f'/projects/{project.id}/generate',
                   data={'vertical': 'auto', 'output_format': 'docx'})
        test("generate enqueued", r.status_code == 302)

    kw = captured_calls[-1]
    test("P0-1: vertical resolved before call (not 'auto')",
         kw.get("vertical") == "data_center", f"got {kw.get('vertical')}")
    tmpls = kw.get("user_templates") or {}
    test("P0-1: org 'general' template loaded for detected vertical",
         "TEMPLATEMARKER" in (tmpls.get("proposal") or ""), f"got {list(tmpls)}")
    test("P0-3: hosted mode passes include_global_boilerplate=False",
         kw.get("include_global_boilerplate") is False)
    test("P0-2: first run passes no answered questions",
         not kw.get("answered_questions"))

    q_count = ProposalQuestion.query.filter_by(project_id=project.id).count()
    test("question recorded once", q_count == 1, f"got {q_count}")
    test("question-only run produced no proposal",
         Proposal.query.filter_by(project_id=project.id).count() == 0)
    test("P0-2: question-only run consumed NO generation quota",
         billing.generations_this_month(pat.org_id) == 0,
         f"got {billing.generations_this_month(pat.org_id)}")

    # Answer the question, regenerate; the AI re-asks the same question.
    q = ProposalQuestion.query.filter_by(project_id=project.id).first()
    c.post(f'/projects/{project.id}/questions', data={f'answer_{q.id}': '12% margin'})
    db.session.refresh(q)
    test("answer stored", q.status == 'answered' and q.answer == '12% margin')

    with patch('app.generate_proposal', side_effect=fake_generate):
        c.post(f'/projects/{project.id}/generate',
               data={'vertical': 'auto', 'output_format': 'docx'})

    kw2 = captured_calls[-1]
    aq = kw2.get("answered_questions") or []
    test("P0-2: regeneration receives the answer",
         any(a.get("answer") == "12% margin" for a in aq), f"got {aq}")
    test("P0-2: re-asked duplicate not re-recorded",
         ProposalQuestion.query.filter_by(project_id=project.id).count() == 1)
    proposal = Proposal.query.filter_by(project_id=project.id).first()
    test("P0-2: duplicate-only questions fall through to a saved proposal",
         proposal is not None)
    test("P0-2: produced proposal now counts as 1 generation",
         billing.generations_this_month(pat.org_id) == 1,
         f"got {billing.generations_this_month(pat.org_id)}")

    print("\n=== P0-4: DB-canonical reads + synced writes ===")
    from config.settings import GENERATED_DIR
    md_path = GENERATED_DIR / proposal.md_file
    if md_path.exists():
        md_path.unlink()  # simulate redeploy / other instance: local file gone
    r = c.get(f'/proposal/{proposal.id}')
    test("view works with md file missing (DB version fallback)",
         r.status_code == 200 and b'V1MARKER' in r.data)
    r = c.get(f'/proposal/{proposal.id}/edit')
    test("editor loads content with md file missing",
         r.status_code == 200 and b'V1MARKER' in r.data)

    with patch.object(appmod.storage, 'sync_up') as sync_mock:
        c.post(f'/proposal/{proposal.id}/edit', data={
            'markdown_content': '# Proposal V2MARKER\n\nrevised',
            'change_summary': 'v2',
        })
        test("edit mirrors md+docx to object storage", sync_mock.call_count >= 2,
             f"sync_up called {sync_mock.call_count}x")
    test("edit created v2",
         ProposalVersion.query.filter_by(proposal_id=proposal.id).count() == 2)

    # Legacy fallback: version-less proposal still reads from the file
    legacy = Proposal(project_id=project.id, job_id='legacy1', md_file='legacy1.md')
    db.session.add(legacy); db.session.commit()
    (GENERATED_DIR / 'legacy1.md').write_text('# LEGACYMARKER', encoding='utf-8')
    test("legacy file-only proposal still renders",
         b'LEGACYMARKER' in c.get(f'/proposal/{legacy.id}').data)
    (GENERATED_DIR / 'legacy1.md').unlink()

    print("\n=== P0-5: portal pinned to shared version ===")
    # Re-pin share flow: share was never created yet; create pins current (v2)
    c.post(f'/proposal/{proposal.id}/share', data={})
    share = ProposalShare.query.filter_by(proposal_id=proposal.id).first()
    test("share pinned to current version", share is not None and share.version_number == 2,
         f"got {share.version_number if share else None}")

    # Internal edit AFTER sharing — customer must keep seeing v2
    c.post(f'/proposal/{proposal.id}/edit', data={
        'markdown_content': '# Proposal V3MARKER\n\ninternal work-in-progress',
        'change_summary': 'v3',
    })
    body = c.get(f'/p/{share.token}').data
    test("portal shows the pinned v2, not the newer v3",
         b'V2MARKER' in body and b'V3MARKER' not in body)

    # Owner explicitly updates the customer view -> re-pins to latest
    c.post(f'/proposal/{proposal.id}/share', data={})
    db.session.refresh(share)
    body = c.get(f'/p/{share.token}').data
    test("re-share re-pins to v3", share.version_number == 3 and b'V3MARKER' in body)

    # Legacy shares (version_number 0) fall back to latest
    share.version_number = 0; db.session.commit()
    test("legacy unpinned share falls back to latest",
         b'V3MARKER' in c.get(f'/p/{share.token}').data)

print("\n=== P0-3: prompt assembly (real generate_proposal, fake client) ===")

class _FakeStream:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    @property
    def text_stream(self):
        yield "# Proposal\n\nConfidence Score: 90%"

class _FakeClient:
    def __init__(self, sink): self._sink = sink
    @property
    def messages(self): return self
    def stream(self, **kwargs):
        self._sink.append(kwargs)
        return _FakeStream()

for flag, name in ((False, "hosted"), (True, "self-hosted")):
    sink = []
    with patch.object(proposal_agent, '_make_client', lambda key, s=sink: _FakeClient(s)):
        proposal_agent.generate_proposal(
            "data center EPMS test document", vertical="data_center",
            user_api_key="test-key", include_global_boilerplate=flag,
        )
    system = sink[0]["system"]
    leaked = [m for m in ("E Tech Group", "Apex Technology", "[Your Company Name]")
              if m in system]
    if flag:
        test(f"{name}: sample boilerplate still available", len(leaked) > 0,
             "expected sample content in self-hosted prompt")
    else:
        test(f"{name}: no sample-company content in prompt", not leaked,
             f"leaked: {leaked}")

print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
sys.exit(0 if failed == 0 else 1)

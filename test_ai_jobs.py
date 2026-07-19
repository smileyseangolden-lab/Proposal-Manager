"""Regression tests for jobifying the last inline AI calls.

Addendum impact analysis, targeted section regeneration, and the posture
"AI import & review" flows (rates + standards) previously ran their AI calls
in the request path — minutes-long calls holding a gunicorn worker. They now
enqueue background jobs (progress page + cancel, like generation), carry the
platform-AI verified-email gate, parse documents with ocr=True (OCR is
allowed inside jobs), and the ingest review screen reads its rows from the
finished job's result.

Also covers two fixes landed alongside:
  - the job runner executes handlers in a *request* context so the final
    url_for-built redirect works in production workers (no SERVER_NAME is
    configured, so a bare app context would fail every job at the finish line);
  - the addendum route rejects document ids belonging to another project
    (previously any doc id parsed into the analysis).

Standalone runner: python test_ai_jobs.py
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
from config.settings import GENERATED_DIR
from models import (BackgroundJob, ClarificationItem, CompanyStandard, Project,
                    ProjectDocument, Proposal, ProposalVersion, StaffRole, User)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = failed = 0


def test(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS: {name}")
    else:
        failed += 1; print(f"  FAIL: {name} - {detail}")


PROPOSAL_MD = ("# Proposal V1MARKER\n\n"
               "## Scope of Work\n\nOld scope line\n\n"
               "## Pricing\n\nOld pricing line\n\n"
               "Confidence Score: 80%")


def fake_generate(rfp_text, **kwargs):
    return {"proposal_markdown": PROPOSAL_MD,
            "action_items": [], "confidence_score": 80, "document_type": "RFP",
            "vertical": kwargs.get("vertical") or "general",
            "vertical_label": "General",
            "generated_at": "2026-01-01T00:00:00+00:00"}


def job_of(kind):
    return (BackgroundJob.query.filter_by(kind=kind)
            .order_by(BackgroundJob.created_at.desc()).first())


def job_count(kind):
    return BackgroundJob.query.filter_by(kind=kind).count()


with app.app_context():
    db.drop_all(); db.create_all()
    c = app.test_client()
    c.post('/signup', data={'username': 'zoe', 'email': 'zoe@corp.com',
                            'password': 'password123', 'display_name': 'Zoe',
                            'company_name': 'ZoeCorp'})
    zoe = User.query.filter_by(username='zoe').first()
    zoe.email_verified = True
    db.session.commit()

    c.post('/projects/new', data={'project_name': 'JobsBid', 'client_name': 'Acme'})
    proj = Project.query.filter_by(name='JobsBid').first()
    c.post(f'/projects/{proj.id}/upload', data={
        'file_type': 'rfp',
        'documents': (io.BytesIO(b'RFP: build a control system for Acme'), 'rfp.txt'),
    }, content_type='multipart/form-data')
    with patch('app.generate_proposal', side_effect=fake_generate):
        c.post(f'/projects/{proj.id}/generate',
               data={'vertical': 'general', 'output_format': 'docx'})
    prop = Proposal.query.filter_by(project_id=proj.id).first()
    base_versions = ProposalVersion.query.filter_by(proposal_id=prop.id).count()

    print("\n=== Addendum analysis runs as a background job ===")
    c.post(f'/projects/{proj.id}/upload', data={
        'file_type': 'rfp',
        'documents': (io.BytesIO(b'Addendum 1: voltage changed to 480V'), 'addendum.txt'),
    }, content_type='multipart/form-data')
    addendum_doc = (ProjectDocument.query.filter_by(project_id=proj.id)
                    .filter(ProjectDocument.original_filename == 'addendum.txt').first())
    test("addendum doc uploaded", addendum_doc is not None)

    parse_calls = []
    real_parse = appmod.parse_document
    def spy_parse(path, **kw):
        parse_calls.append(kw)
        return real_parse(path, **kw)

    def fake_addendum(rfp_text, addendum_text, current_md, **kwargs):
        assert 'voltage changed' in addendum_text
        return {"changes": [
            {"addendum_item": "Voltage now 480V", "severity": "high",
             "impact_description": "Electrical scope changes",
             "suggested_resolution": "Update the scope section",
             "affected_sections": ["Scope of Work", "Pricing"],
             "can_ai_resolve": True},
            {"addendum_item": "Deadline moved up", "severity": "medium",
             "impact_description": "Schedule impact",
             "suggested_resolution": "Revise timeline",
             "affected_sections": ["Schedule"], "can_ai_resolve": False},
        ]}

    with patch('app.analyze_addendum_impact', side_effect=fake_addendum), \
         patch('app.parse_document', side_effect=spy_parse):
        r = c.post(f'/projects/{proj.id}/addendum-analysis',
                   data={'addendum_doc_id': addendum_doc.id})
    test("route redirects to the job progress page",
         r.status_code == 302 and '/jobs/' in r.headers['Location'],
         r.headers.get('Location', ''))
    job = job_of('analyze_addendum')
    test("addendum job ran to done", job is not None and job.status == 'done',
         f"{job and job.status}: {job and job.error}")
    test("documents parsed with OCR allowed",
         parse_calls and all(kw.get('ocr') is True for kw in parse_calls),
         str(parse_calls))
    items = ClarificationItem.query.filter_by(project_id=proj.id, source='addendum').all()
    test("two impacts filed in the clarification register", len(items) == 2)
    hi = next((i for i in items if i.priority == 'high'), None)
    test("impact fields recorded", hi is not None
         and hi.question == 'Voltage now 480V'
         and hi.proposal_section == 'Scope of Work, Pricing'
         and hi.created_by == zoe.id and hi.status == 'open')
    db.session.refresh(proj)
    test("project flagged clarification_pending",
         proj.clarification_sub_status == 'clarification_pending')
    r = c.get(f'/jobs/{job.id}/status.json')
    test("job redirect points at the clarification register",
         r.get_json().get('redirect', '').endswith(f'/projects/{proj.id}/clarifications'))
    r = c.get(f'/jobs/{job.id}')
    test("progress page labels the job kind", b'Analyzing addendum' in r.data)

    print("\n=== Addendum guards ===")
    c.post('/projects/new', data={'project_name': 'OtherBid', 'client_name': 'B'})
    projB = Project.query.filter_by(name='OtherBid').first()
    c.post(f'/projects/{projB.id}/upload', data={
        'file_type': 'rfp',
        'documents': (io.BytesIO(b'Some other RFP'), 'other.txt'),
    }, content_type='multipart/form-data')
    docB = ProjectDocument.query.filter_by(project_id=projB.id).first()

    n = job_count('analyze_addendum')
    r = c.post(f'/projects/{proj.id}/addendum-analysis',
               data={'addendum_doc_id': docB.id}, follow_redirects=True)
    test("doc from another project is rejected",
         b'Addendum document not found' in r.data and job_count('analyze_addendum') == n)

    r = c.post(f'/projects/{projB.id}/addendum-analysis',
               data={'addendum_doc_id': docB.id}, follow_redirects=True)
    test("no proposal yet -> friendly error, no job",
         b'No existing proposal' in r.data and job_count('analyze_addendum') == n)

    zoe.email_verified = False
    db.session.commit()
    r = c.post(f'/projects/{proj.id}/addendum-analysis',
               data={'addendum_doc_id': addendum_doc.id}, follow_redirects=True)
    test("unverified email is gated from platform AI",
         b'verify your email' in r.data and job_count('analyze_addendum') == n)
    zoe.email_verified = True
    db.session.commit()

    with patch('app.analyze_addendum_impact', side_effect=Exception('kaboom')):
        c.post(f'/projects/{proj.id}/addendum-analysis',
               data={'addendum_doc_id': addendum_doc.id})
    job = job_of('analyze_addendum')
    test("AI failure marks the job failed with the message",
         job.status == 'failed' and 'kaboom' in (job.error or ''),
         f"{job.status}: {job.error}")
    test("failed analysis files no impacts",
         ClarificationItem.query.filter_by(project_id=proj.id, source='addendum').count() == 2)

    print("\n=== Section regeneration runs as a background job ===")
    def fake_regen(**kwargs):
        assert kwargs['section_heading'] == '## Scope of Work'
        assert 'Old scope line' in kwargs['full_proposal_md']
        return {"section_markdown": "## Scope of Work\n\nNEW REGEN CONTENT "
                                    "[ACTION REQUIRED: confirm voltage]"}

    with patch('app.regenerate_section', side_effect=fake_regen):
        r = c.post(f'/proposal/{prop.id}/regenerate-section',
                   data={'section_heading': '## Scope of Work',
                         'clarification_answer': 'Voltage is 480V'})
    test("route redirects to the job progress page",
         r.status_code == 302 and '/jobs/' in r.headers['Location'])
    job = job_of('regenerate_section')
    test("regeneration job ran to done", job is not None and job.status == 'done',
         f"{job and job.status}: {job and job.error}")
    versions = (ProposalVersion.query.filter_by(proposal_id=prop.id)
                .order_by(ProposalVersion.version_number.desc()).all())
    test("a new version was saved", len(versions) == base_versions + 1)
    test("only the target section was replaced",
         'NEW REGEN CONTENT' in versions[0].markdown_content
         and 'Old pricing line' in versions[0].markdown_content
         and 'Old scope line' not in versions[0].markdown_content)
    test("version credited to the requesting user (ai edit)",
         versions[0].edit_source == 'ai' and versions[0].editor_id == zoe.id)
    db.session.refresh(prop)
    test("action items recounted from the new content", prop.action_items_count == 1)
    md_on_disk = (GENERATED_DIR / prop.md_file).read_text(encoding='utf-8')
    test("markdown artifact mirrors the new version", 'NEW REGEN CONTENT' in md_on_disk)
    r = c.get(f'/jobs/{job.id}/status.json')
    test("job redirect returns to the editor",
         r.get_json().get('redirect', '').endswith(f'/proposal/{prop.id}/edit'))

    print("\n=== Section regeneration guards ===")
    n = job_count('regenerate_section')
    r = c.post(f'/proposal/{prop.id}/regenerate-section',
               data={'section_heading': '## Not A Real Section',
                     'clarification_answer': 'info'}, follow_redirects=True)
    test("unknown heading fails fast without a job",
         b'Couldn&#39;t find' in r.data and job_count('regenerate_section') == n,
         r.data[:200])
    r = c.post(f'/proposal/{prop.id}/regenerate-section',
               data={'section_heading': '', 'clarification_answer': 'x'},
               follow_redirects=True)
    test("missing fields fail fast without a job",
         b'required' in r.data and job_count('regenerate_section') == n)

    print("\n=== Rates ingest runs as a background job ===")
    def fake_rates(raw_text, target, **kwargs):
        assert 'PM,100' in raw_text
        return [{"role_name": "PM", "category": "Management",
                 "hourly_rate": 100, "overtime_rate": 150},
                {"role_name": "Tech", "category": "Field",
                 "hourly_rate": 80, "overtime_rate": 120}]

    with patch('app.extract_rates_from_sheet', side_effect=fake_rates):
        r = c.post('/posture/ingest-rates', data={
            'target_type': 'labor_rates',
            'ingest_file': (io.BytesIO(b'Role,Rate\nPM,100\nTech,80'), 'rates.csv'),
        }, content_type='multipart/form-data')
    test("route redirects to the job progress page",
         r.status_code == 302 and '/jobs/' in r.headers['Location'])
    job = job_of('ingest_rates')
    test("rates job ran to done", job is not None and job.status == 'done',
         f"{job and job.status}: {job and job.error}")
    r = c.get(f'/jobs/{job.id}/status.json')
    review_url = r.get_json().get('redirect', '')
    test("job redirect points at the review screen",
         review_url.endswith(f'/posture/ingest-review/{job.id}'), review_url)
    r = c.get(review_url)
    test("review screen renders the extracted rows",
         r.status_code == 200 and b'value="PM"' in r.data and b'value="Tech"' in r.data
         and b'rates.csv' in r.data)

    c.post('/posture/ingest-rates/confirm', data={
        'target_type': 'labor_rates',
        'row_marker': ['0', '1'], 'include': ['0'],
        'role_name': ['PM', 'Tech'], 'category': ['Management', 'Field'],
        'hourly_rate': ['100', '80'], 'overtime_rate': ['150', '120'],
    })
    test("confirm import still writes the kept rows",
         StaffRole.query.filter_by(org_id=zoe.org_id, role_name='PM').count() == 1
         and StaffRole.query.filter_by(org_id=zoe.org_id, role_name='Tech').count() == 0)

    with patch('app.extract_rates_from_sheet', return_value=[]):
        c.post('/posture/ingest-rates', data={
            'target_type': 'labor_rates',
            'ingest_file': (io.BytesIO(b'nothing useful'), 'empty.csv'),
        }, content_type='multipart/form-data')
    job = job_of('ingest_rates')
    test("zero extracted rows fails the job with guidance",
         job.status == 'failed' and 'any rows' in (job.error or ''))

    print("\n=== Standards ingest runs as a background job ===")
    def fake_standards(text, **kwargs):
        assert 'Always torque-check' in text
        return [{"category": "qa", "title": "Torque Standard",
                 "content": "Always torque-check terminations."}]

    with patch('app.extract_standards', side_effect=fake_standards):
        r = c.post('/posture/ingest-standards', data={
            'standards_file': (io.BytesIO(b'Always torque-check terminations.'),
                               'standards.txt'),
        }, content_type='multipart/form-data')
    job = job_of('ingest_standards')
    test("standards job ran to done", job is not None and job.status == 'done',
         f"{job and job.status}: {job and job.error}")
    r = c.get(f'/posture/ingest-review/{job.id}')
    test("review screen renders the extracted blocks",
         r.status_code == 200 and b'Torque Standard' in r.data)
    c.post('/posture/ingest-standards/confirm', data={
        'include': ['0'], 'category': ['qa'], 'title': ['Torque Standard'],
        'content': ['Always torque-check terminations.'],
    })
    test("confirm import writes the standard",
         CompanyStandard.query.filter_by(org_id=zoe.org_id,
                                         title='Torque Standard').count() == 1)
    rates_job_id = job_of('ingest_rates').id

    print("\n=== Review screen access control ===")
    r = c.get(f'/posture/ingest-review/{prop.id}')
    test("non-job id 404s", r.status_code == 404)
    gen_job = job_of('generate_proposal')
    r = c.get(f'/posture/ingest-review/{gen_job.id}')
    test("non-ingest job kinds 404", r.status_code == 404)

    c.post('/logout')
    c.post('/signup', data={'username': 'rival', 'email': 'rival@other.com',
                            'password': 'password123', 'company_name': 'OtherCo'})
    r = c.get(f'/posture/ingest-review/{rates_job_id}')
    test("another user's review screen 404s", r.status_code == 404)

    n = job_count('ingest_rates')
    r = c.post('/posture/ingest-rates', data={
        'target_type': 'labor_rates',
        'ingest_file': (io.BytesIO(b'Role,Rate\nX,1'), 'r.csv'),
    }, content_type='multipart/form-data', follow_redirects=True)
    test("unverified email is gated from rates ingest",
         b'verify your email' in r.data and job_count('ingest_rates') == n)
    n = job_count('ingest_standards')
    r = c.post('/posture/ingest-standards', data={
        'standards_file': (io.BytesIO(b'std'), 's.txt'),
    }, content_type='multipart/form-data', follow_redirects=True)
    test("unverified email is gated from standards ingest",
         b'verify your email' in r.data and job_count('ingest_standards') == n)

    print("\n=== Worker context: url_for works outside a request ===")
    @jobs.register("ctx_url_probe")
    def _probe(payload, j):
        from flask import url_for
        return {"redirect": url_for("dashboard")}

    probe = BackgroundJob(kind='ctx_url_probe', user_id=zoe.id, org_id=zoe.org_id,
                          status='queued', payload='{}')
    db.session.add(probe); db.session.commit()
    with jobs._job_context():
        jobs._run_job(probe.id)
    db.session.refresh(probe)
    test("handler builds its redirect in the worker context",
         probe.status == 'done' and 'redirect' in (probe.result or ''),
         f"{probe.status}: {probe.error}")

print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
sys.exit(0 if failed == 0 else 1)

"""Regression tests for the P2 batch: N+1 elimination + pagination, trial
enforcement, local-timezone rendering, editor autosave/conflict guard, and the
accessibility pass.

Standalone runner: python test_p2_fixes.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'
os.environ.pop('APP_ENV', None)

from sqlalchemy import event

import app as appmod
from app import app, db
import billing
from models import (Organization, Project, ProjectDocument, Proposal,
                    ProposalVersion, User)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = failed = 0


def test(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS: {name}")
    else:
        failed += 1; print(f"  FAIL: {name} - {detail}")


class QueryCounter:
    """Counts SELECT statements issued while attached to the engine."""
    def __init__(self):
        self.n = 0

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("select"):
            self.n += 1


with app.app_context():
    db.drop_all(); db.create_all()
    c = app.test_client()
    c.post('/signup', data={'username': 'pia', 'email': 'pia@corp.com',
                            'password': 'password123', 'display_name': 'Pia',
                            'company_name': 'PiaCorp'})
    pia = User.query.filter_by(username='pia').first()
    pia.email_verified = True
    org = db.session.get(Organization, pia.org_id)
    db.session.commit()

    print("\n=== Trial enforcement ===")
    test("signup stamps a trial window", org.trial_ends_at is not None)
    test("trial is active for a fresh workspace", billing.trial_active(org))
    test("trial grants PRO limits on the free plan",
         billing.limits_for(org)["generations_per_month"]
         == billing.PLANS["pro"]["limits"]["generations_per_month"])
    test("trial_days_left positive", billing.trial_days_left(org) > 0)

    r = c.get('/billing')
    test("billing page announces the trial", b'Pro trial active' in r.data)
    r = c.get('/')
    test("sidebar shows the trial chip", b'Pro trial' in r.data)

    org.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.session.commit()
    test("expired trial falls back to FREE limits",
         billing.limits_for(org)["generations_per_month"]
         == billing.PLANS["free"]["limits"]["generations_per_month"])
    test("expired trial not active", not billing.trial_active(org))
    r = c.get('/billing')
    test("billing page announces trial ended", b'trial has ended' in r.data)

    org.trial_ends_at = datetime.now(timezone.utc) + timedelta(days=5)
    org.billing_status = 'past_due'
    db.session.commit()
    test("delinquency overrides trial (free limits)",
         billing.limits_for(org)["generations_per_month"]
         == billing.PLANS["free"]["limits"]["generations_per_month"])
    org.billing_status = ''
    org.plan = 'business'
    db.session.commit()
    test("paid plan ignores trial (business limits)",
         billing.limits_for(org)["generations_per_month"] == -1)

    print("\n=== N+1 elimination + pagination ===")
    # 60 projects, each with a document; half with a proposal + version
    for i in range(60):
        p = Project(user_id=pia.id, org_id=pia.org_id, name=f'Bid {i:03d}',
                    client_name='Acme', status='active')
        db.session.add(p); db.session.flush()
        db.session.add(ProjectDocument(
            project_id=p.id, filename=f'd{i}', original_filename=f'd{i}.txt',
            file_type='rfp', file_path=f'/tmp/none{i}.txt', file_size=10,
            text_chars=100))
        if i % 2 == 0:
            prop = Proposal(project_id=p.id, job_id=f'job{i:03d}',
                            md_file=f'p{i}.md', review_status='draft')
            db.session.add(prop); db.session.flush()
            db.session.add(ProposalVersion(
                proposal_id=prop.id, version_number=1,
                markdown_content=f'# Bid {i:03d}\n\nNEEDLE{i:03d} content'))
    db.session.commit()

    counter = QueryCounter()
    event.listen(db.engine, "before_cursor_execute", counter)
    r = c.get('/proposals')
    event.remove(db.engine, "before_cursor_execute", counter)
    test("proposals page renders with 60 projects", r.status_code == 200)
    test("proposals page query count is bounded (no N+1)",
         counter.n <= 40, f"issued {counter.n} SELECTs for 60 projects")
    row_marker = b'class="clickable-row"'
    n_rows = r.data.count(row_marker)
    test("table paginated to 50 rows", n_rows == 50, f"got {n_rows}")
    test("pagination nav present", b'Page 1 of 2' in r.data)
    r2 = c.get('/proposals?page=2')
    n_rows2 = r2.data.count(row_marker)
    test("page 2 shows the remainder", n_rows2 == 10, f"got {n_rows2}")

    counter = QueryCounter()
    event.listen(db.engine, "before_cursor_execute", counter)
    r = c.get('/')
    event.remove(db.engine, "before_cursor_execute", counter)
    test("dashboard renders with 60 active projects", r.status_code == 200)
    test("dashboard query count is bounded (no N+1)",
         counter.n <= 40, f"issued {counter.n} SELECTs for 60 projects")

    r = c.get('/admin')
    test("admin panel renders (bulk stats)",
         r.status_code == 200 and b'Rep Performance' in r.data)

    r = c.get('/documents')
    test("document library renders paginated", r.status_code == 200
         and b'Page 1 of 2' in r.data)

    print("\n=== SQL-side proposal search ===")
    counter = QueryCounter()
    event.listen(db.engine, "before_cursor_execute", counter)
    r = c.get('/search?q=NEEDLE042')
    event.remove(db.engine, "before_cursor_execute", counter)
    test("content search finds the proposal", b'Bid 042' in r.data)
    test("search query count bounded", counter.n <= 30,
         f"issued {counter.n} SELECTs")

    print("\n=== Local timezone rendering ===")
    out = str(appmod.localtime_filter(datetime(2026, 1, 5, 14, 30)))
    test("localtime emits a <time> element with UTC ISO",
         '<time' in out and 'data-utc="2026-01-05T14:30:00Z"' in out, out)
    out_date = str(appmod.localtime_filter(datetime(2026, 1, 5, 14, 30), 'date'))
    test("date style falls back to date-only", 'Jan 05, 2026' in out_date, out_date)
    test("empty datetime renders empty", str(appmod.localtime_filter(None)) == "")
    prop = Proposal.query.filter_by(job_id='job000').first()
    proj = db.session.get(Project, prop.project_id)
    r = c.get(f'/projects/{proj.id}/upload')
    test("project page uses localized timestamps", b'data-utc=' in r.data)
    r = c.get('/')
    test("base layout ships the localizer script", b'time[data-utc]' in r.data)

    print("\n=== Editor autosave + conflict guard ===")
    r = c.get(f'/proposal/{prop.id}/edit')
    test("editor ships autosave hooks",
         b'pm-editor-draft-' in r.data and b'draft-restore-bar' in r.data)
    test("editor carries base_version", b'name="base_version" value="1"' in r.data)

    # Stale base_version (someone else saved) -> blocked, text preserved, no v2
    r = c.post(f'/proposal/{prop.id}/edit', data={
        'markdown_content': '# MINE conflict-edit PRESERVEME',
        'change_summary': 'x', 'base_version': '0',
    })
    test("stale save blocked with text preserved",
         r.status_code == 200 and b'PRESERVEME' in r.data
         and b'Save blocked' in r.data)
    test("no version created on conflict",
         ProposalVersion.query.filter_by(proposal_id=prop.id).count() == 1)

    r = c.post(f'/proposal/{prop.id}/edit', data={
        'markdown_content': '# MINE clean save',
        'change_summary': 'x', 'base_version': '1',
    }, follow_redirects=True)
    test("matching base_version saves v2",
         ProposalVersion.query.filter_by(proposal_id=prop.id).count() == 2)

    print("\n=== Accessibility ===")
    r = c.get('/')
    test("skip link present", b'skip-link' in r.data and b'id="main-content"' in r.data)
    test("primary nav labeled", b'aria-label="Primary"' in r.data)
    css = open('static/css/style.css', encoding='utf-8').read()
    test("focus-visible styles exist", ':focus-visible' in css)
    test("reduced-motion honored", 'prefers-reduced-motion' in css)
    r = c.post('/settings', data={'display_name': 'Pia', 'email': pia.email,
                                  'font_preference': 'Calibri',
                                  'llm_model': pia.llm_model},
               follow_redirects=True)
    test("flash messages announce via role=alert",
         b'role="alert"' in r.data and b'Settings saved.' in r.data)

print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
sys.exit(0 if failed == 0 else 1)

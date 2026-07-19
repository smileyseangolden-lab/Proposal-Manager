"""Regression tests for the SQL-aggregated /reports page.

/reports previously loaded every accessible project into Python and bucketed
there — linear cost per request. Every section now aggregates in SQL
(grouped counts/sums, SQL-side caps). These tests pin the numbers each
section renders, the access scoping (admin = org-wide, member = own +
assigned, never cross-org), the 6-month trend window, and the
missing-close-details selection.

Standalone runner: python test_reports.py
"""
import os
import sys
from datetime import datetime, timezone

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'
os.environ.pop('APP_ENV', None)

from app import app, db
from models import Organization, Project, User

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = failed = 0


def test(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS: {name}")
    else:
        failed += 1; print(f"  FAIL: {name} - {detail}")


def month_shift(base, months_back, day=15):
    m = base.month - months_back
    y = base.year
    while m <= 0:
        m += 12
        y -= 1
    return datetime(y, m, day)


with app.app_context():
    from flask import g

    def _fresh_request_state():
        # These suites run inside ONE long-lived app context, so flask-login's
        # per-context user cache (g._login_user) survives across simulated
        # requests. Clearing it between interleaved clients reproduces
        # production's fresh-context-per-request behavior.
        if hasattr(g, '_login_user'):
            delattr(g, '_login_user')

    db.drop_all(); db.create_all()
    c = app.test_client()
    c.post('/signup', data={'username': 'zoe', 'email': 'zoe@corp.com',
                            'password': 'password123', 'company_name': 'ZoeCorp'})
    zoe = User.query.filter_by(username='zoe').first()
    zoe.email_verified = True
    sam = User(username='sam', email='sam@corp.com', org_id=zoe.org_id, role='sales')
    sam.set_password('password123')
    db.session.add(sam)
    db.session.commit()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    this_month = month_shift(now, 0)
    prev_month = month_shift(now, 1)
    long_ago = month_shift(now, 8)

    DC = 'Data Center / Mission Critical'

    def proj(name, owner, **kw):
        p = Project(name=name, client_name='Acme', user_id=owner.id,
                    org_id=owner.org_id, **kw)
        db.session.add(p)
        return p

    # Zoe's org fixture (7 projects)
    proj('WonBig', zoe, status='won', dollar_amount=100000.0, vertical_label=DC,
         close_category='price', competitor_name='Rival Corp', closed_at=this_month)
    proj('WonSmall', zoe, status='won', dollar_amount=50000.0, vertical_label=DC,
         close_category='relationship', closed_at=this_month)
    proj('LostOne', zoe, status='lost', dollar_amount=30000.0, vertical_label='General',
         competitor_name='Rival Corp', closed_at=prev_month)
    proj('ActiveOne', zoe, status='active', vertical_label='General')
    proj('SubmittedOne', zoe, status='submitted', vertical_label='')
    proj('GhostClose', zoe, status='lost', dollar_amount=10000.0,
         closed_at=long_ago)  # no close details at all; outside trend window
    proj('SamWin', sam, status='won', dollar_amount=20000.0, vertical_label='General',
         close_category='price', closed_at=this_month)

    # A different org that must never leak into ZoeCorp's reports
    c2 = app.test_client()
    c2.post('/signup', data={'username': 'other', 'email': 'other@elsewhere.com',
                             'password': 'password123', 'company_name': 'ElseCo'})
    other = User.query.filter_by(username='other').first()
    proj('GhostBid', other, status='won', dollar_amount=999999.0,
         vertical_label=DC, closed_at=this_month)
    db.session.commit()

    print("\n=== Admin view: org-wide aggregates ===")
    _fresh_request_state()
    c.post('/login', data={'username': 'zoe', 'password': 'password123'})
    _fresh_request_state()
    r = c.get('/reports')
    html = r.data.decode()
    test("page renders", r.status_code == 200)
    test("overall win rate 3/5 decided = 60%", '60%' in html)
    test("won/lost counts", '3 / 2' in html)
    test("revenue won $170,000", '$170,000' in html)
    test("revenue lost $40,000", '$40,000' in html)
    test("total projects = 7", '<span class="stat-number">7</span>' in html)
    test("active pipeline = 1", '<span class="stat-number">1</span>' in html)

    test("vertical rows present", DC in html and 'General' in html)
    test("DC win rate 100%", '100%' in html)
    # Vertical table is ordered by closed value: DC ($150k) before General ($60k)
    test("verticals ordered by book of business",
         html.index(DC) < html.index('<span class="badge badge-vertical">General</span>'),
         "DC should render before General")

    test("competitor table shows Rival Corp", 'Rival Corp' in html)
    test("competitor win rate 1 of 2 = 50%", '50%' in html)

    test("won reasons: Price counted",
         'Price' in html and '<span class="reason-count">1</span>' in html)
    test("lost reasons: blanks land in Other",
         'Other' in html and '<span class="reason-count">2</span>' in html)

    test("trend: current month has 3 wins", 'title="Won: 3' in html)
    test("trend: previous month has 1 loss", 'title="Lost: 1' in html)
    test("trend: 8-month-old closure outside the window",
         'title="Won: 4' not in html and 'title="Lost: 2' not in html)

    test("missing-details lists the detail-less closure", 'GhostClose' in html)
    test("closures with any detail are not nagged",
         'WonSmall' not in html.split('Fill in Missing Close Details')[-1])

    print("\n=== Cross-org isolation ===")
    test("other org's project name absent", 'GhostBid' not in html)
    test("other org's value absent", '999,999' not in html)

    print("\n=== Member view: own + assigned only ===")
    c.post('/logout')
    _fresh_request_state()
    c.post('/login', data={'username': 'sam', 'password': 'password123'})
    _fresh_request_state()
    r = c.get('/reports')
    html = r.data.decode()
    test("member sees only their own project",
         '<span class="stat-number">1</span>' in html and 'SamWin' not in html
         and '$20,000' in html and '1 / 0' in html)
    test("member does not see org-mates' totals", '$170,000' not in html)

    # Assignment pulls a project into the member's report
    lost = Project.query.filter_by(name='LostOne').first()
    lost.assigned_to = sam.id
    db.session.commit()
    _fresh_request_state()
    r = c.get('/reports')
    html = r.data.decode()
    test("assigned project joins the member's numbers",
         '1 / 1' in html and '$30,000' in html)

    print("\n=== Empty state ===")
    c3 = app.test_client()
    _fresh_request_state()
    c3.post('/signup', data={'username': 'newbie', 'email': 'new@fresh.com',
                             'password': 'password123', 'company_name': 'FreshCo'})
    _fresh_request_state()
    r = c3.get('/reports')
    html = r.data.decode()
    test("fresh org renders zeros without errors",
         r.status_code == 200 and '0%' in html and 'No projects yet' in html)

print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
sys.exit(0 if failed == 0 else 1)

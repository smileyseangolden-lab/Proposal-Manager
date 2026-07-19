"""Regression tests for the mobile-responsive sweep + WCAG AA pass.

Pins: the off-canvas sidebar breakpoint and backdrop, the .table-scroll
wrappers on wide tables (and the ≤860px safety net for unwrapped ones), the
iOS no-zoom input sizing, palette contrast ratios computed with the real
WCAG formula (so a future color tweak that breaks AA fails loudly), the
ink-300 text retargeting, and the ARIA wiring (sidebar toggle state, search /
chatbot / ingest-review / reports-inline form labels). Also compiles every
template touched by the sweep so a malformed wrapper can't ship.

Standalone runner: python test_responsive_a11y.py
"""
import os
import re
import sys
from datetime import datetime, timezone

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'
os.environ.pop('APP_ENV', None)

from app import app, db
from models import Project, User

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = failed = 0


def test(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS: {name}")
    else:
        failed += 1; print(f"  FAIL: {name} - {detail}")


CSS = open('static/css/style.css').read()


def token(name):
    m = re.search(rf'--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})', CSS)
    return m.group(1) if m else None


def luminance(hexc):
    rgb = [int(hexc.lstrip('#')[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(fg, bg):
    la, lb = luminance(fg), luminance(bg)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


print("\n=== CSS: responsive structure ===")
test("table-scroll utility exists",
     re.search(r'\.table-scroll\s*\{[^}]*overflow-x:\s*auto', CSS) is not None)
tablet = re.search(r'@media \(max-width: 1024px\)\s*\{(.*?)\n\}', CSS, re.DOTALL)
test("tablet breakpoint exists", tablet is not None)
tb = tablet.group(1) if tablet else ""
test("sidebar goes off-canvas on tablet", 'translateX(-100%)' in tb)
test("drawer state slides it back in", '.sidebar.mobile-open' in tb and 'translateX(0)' in tb)
test("main content reclaims the sidebar margin", 'margin-left: 0' in tb)
test("backdrop styled for the open drawer", '.sidebar-backdrop' in tb)

mobile = re.search(r'@media \(max-width: 860px\)\s*\{(.*?)\n\}', CSS, re.DOTALL)
mb = mobile.group(1) if mobile else ""
test("mobile breakpoint exists", mobile is not None)
test("unwrapped wide tables scroll in their own box",
     '.proposals-table, .mini-table' in mb and 'overflow-x: auto' in mb)
test("16px inputs prevent iOS focus zoom", 'font-size: 16px' in mb)
# Found by the browser drive: flex children without min-width: 0 pushed the
# topbar wider than the viewport, and the trend chart / month calendar have
# intrinsic widths that must scroll inside their cards.
test("topbar search can shrink below content size", 'min-width: 0' in mb)
test("avatar name hidden on phones", '.avatar-name' in mb)
test("keyboard hint hidden with matching specificity",
     '.topbar-search .kbd-hint { display: none; }' in mb)
test("trend chart scrolls in its card", '.trend-chart { overflow-x: auto' in mb)
test("calendar keeps usable day cells and scrolls",
     '.calendar-grid { min-width: 640px; }' in mb)
test("admin activity filter wraps", '.activity-filter { flex-wrap: wrap; }' in mb)
test("focus-visible ring survives the sweep", ':focus-visible' in CSS and 'outline: 2px solid' in CSS)
test("reduced-motion preference honored", 'prefers-reduced-motion' in CSS)

print("\n=== CSS: WCAG AA contrast (computed) ===")
paper, mist = token('paper'), token('mist')
checks = [
    ("body text (ink-900 on paper)", token('ink-900'), paper, 4.5),
    ("secondary text (ink-700 on paper)", token('ink-700'), paper, 4.5),
    ("muted text (ink-500 on paper)", token('ink-500'), paper, 4.5),
    ("muted text (ink-500 on mist)", token('ink-500'), mist, 4.5),
    ("gold badge text on gold-bg", token('gold'), token('gold-bg'), 4.5),
    ("red badge text on red-bg", token('red'), token('red-bg'), 4.5),
    ("ok badge text on ok-bg", token('ok'), token('ok-bg'), 4.5),
    ("violet badge text on violet-bg", token('violet'), token('violet-bg'), 4.5),
    ("sidebar text on sea-900", token('sidebar-text') or '#b9d6cc', token('sea-900'), 4.5),
]
for name, fg, bg, minimum in checks:
    if not (fg and bg):
        test(f"{name}: tokens found", False, f"fg={fg} bg={bg}")
        continue
    r = contrast(fg, bg)
    test(f"{name} >= {minimum}:1", r >= minimum, f"{r:.2f}")

for selector in ('.footer', '.timeline .tl-meta', '.step.step-pending'):
    block = CSS.split(selector, 1)[1][:220] if selector in CSS else ''
    test(f"{selector} text no longer uses ink-300",
         'var(--ink-300)' not in block and 'var(--ink-500)' in block, block[:80])

print("\n=== Templates compile after the table wrapping ===")
for name in ('base.html', 'proposals.html', 'admin.html', 'reports.html',
             'proposal_estimate.html', 'ingest_review.html',
             'clarification_register.html', 'calendar.html'):
    try:
        app.jinja_env.get_template(name)
        test(f"{name} compiles", True)
    except Exception as e:
        test(f"{name} compiles", False, str(e))

test("calendar grid is wrapped in a scroll container",
     'table-scroll' in open('web_templates/calendar.html').read())

for name in ('proposals.html', 'admin.html', 'reports.html',
             'proposal_estimate.html', 'ingest_review.html',
             'clarification_register.html'):
    src = open(f'web_templates/{name}').read()
    test(f"{name} wide tables are wrapped",
         src.count('table-scroll') >= src.count('<table class="proposals-table')
         and 'table-scroll' in src)

print("\n=== Rendered pages: ARIA wiring ===")
with app.app_context():
    db.drop_all(); db.create_all()
    c = app.test_client()
    c.post('/signup', data={'username': 'ada', 'email': 'ada@corp.com',
                            'password': 'password123', 'company_name': 'AdaCo'})
    ada = User.query.filter_by(username='ada').first()
    ada.email_verified = True
    db.session.add(Project(name='ClosedNoDetails', client_name='Acme',
                           user_id=ada.id, org_id=ada.org_id, status='lost',
                           dollar_amount=1000.0,
                           closed_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    db.session.commit()

    html = c.get('/').data.decode()
    test("viewport meta present", 'name="viewport"' in html)
    test("skip link present", 'skip-link' in html)
    test("sidebar has an id the toggle controls",
         'id="app-sidebar"' in html and 'aria-controls="app-sidebar"' in html)
    toggle_html = html[html.find('id="sidebar-toggle"'):][:300]
    test("sidebar toggle exposes expanded state", 'aria-expanded' in toggle_html)
    backdrop_html = html[html.find('class="sidebar-backdrop"'):][:120]
    test("drawer backdrop rendered hidden",
         'id="sidebar-backdrop"' in backdrop_html and 'hidden' in backdrop_html)
    test("global search input is labelled",
         'aria-label="Search projects, proposals, and documents"' in html)
    test("chatbot input is labelled", 'aria-label="Ask the help assistant"' in html)
    test("chatbot close is labelled", 'aria-label="Close help assistant"' in html)

    html = c.get('/proposals').data.decode()
    test("proposals table scrolls in a wrapper", 'table-scroll' in html)

    html = c.get('/admin').data.decode()
    test("admin tables scroll in wrappers", html.count('table-scroll') >= 2)

    html = c.get('/reports').data.decode()
    test("reports tables scroll in wrappers", html.count('table-scroll') >= 1)
    test("inline close-details fields are labelled",
         'aria-label="Close category for ClosedNoDetails"' in html
         and 'aria-label="Competitor for ClosedNoDetails"' in html
         and 'aria-label="Close reason for ClosedNoDetails"' in html)

    r = c.get('/logout')  # POST-only; GET should not 500
    test("auth pages unaffected (login renders)",
         c.post('/logout').status_code in (302,) and c.get('/login').status_code == 200)

print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
sys.exit(0 if failed == 0 else 1)

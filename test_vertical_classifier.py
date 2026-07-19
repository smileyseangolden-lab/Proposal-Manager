"""Regression tests for the LLM-based vertical classifier.

document_parser.detect_vertical (keyword counting) missed RFPs that
paraphrase — a pharma cleanroom fit-out that never says "GMP" landed in
'general' and lost its vertical templates. proposal_agent.classify_vertical
now asks a small, fast Claude model (CLAUDE_CLASSIFIER_MODEL) to read an
excerpt, with the keyword scorer as the fallback on every failure path
(no API key, API error, budget exhausted, nonsense answer) so generation
never breaks on classification.

Covers: the classifier's happy path and normalization, every fallback path,
excerpt truncation, model selection, wiring into draft_scope_of_work and the
generation job, and the approved-scope short-circuit that must NOT classify.

Standalone runner: python test_vertical_classifier.py
"""
import io
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'
os.environ.pop('APP_ENV', None)

import app as appmod
from app import app, db
import proposal_agent
from config.settings import CLAUDE_CLASSIFIER_MODEL
from document_parser import detect_vertical
from models import Project, ProjectScope, Proposal, User

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = failed = 0


def test(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  PASS: {name}")
    else:
        failed += 1; print(f"  FAIL: {name} - {detail}")


DC_TEXT = ("RFP for a hyperscale data center: BMS and EPMS integration, "
           "hot aisle containment, UPS and switchgear monitoring, "
           "generator controls for the new data hall.")
PLAIN_TEXT = "Please provide a quotation for controls services at our facility."


class FakeCreateClient:
    """Stands in for the metered client; records the .messages.create kwargs."""

    def __init__(self, answer=None, error=None):
        self.calls = []
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                if error is not None:
                    raise error
                return SimpleNamespace(
                    content=[SimpleNamespace(type='text', text=answer)])

        self.messages = _Messages()


class FakeStreamClient:
    """Fake for the streaming calls (draft_scope_of_work)."""

    def __init__(self, text):
        outer_text = text

        class _Stream:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            @property
            def text_stream(self):
                return iter([outer_text])

        class _Messages:
            def stream(self, **kwargs):
                return _Stream()

        self.messages = _Messages()


print("\n=== classify_vertical: LLM path ===")
fake = FakeCreateClient(answer='life_science')
with patch('proposal_agent._make_client', return_value=fake):
    result = proposal_agent.classify_vertical(PLAIN_TEXT, user_api_key='sk-test')
test("LLM answer wins over keywords", result == 'life_science', result)
test("classifier uses the fast model, not CLAUDE_MODEL",
     fake.calls[0]['model'] == CLAUDE_CLASSIFIER_MODEL, fake.calls[0]['model'])
test("classifier caps output tokens small", fake.calls[0]['max_tokens'] <= 64)

fake = FakeCreateClient(answer='  "Data_Center".  ')
with patch('proposal_agent._make_client', return_value=fake):
    result = proposal_agent.classify_vertical(PLAIN_TEXT, user_api_key='sk-test')
test("sloppy answers are normalized", result == 'data_center', result)

huge = "controls " + ("x" * 100_000)
fake = FakeCreateClient(answer='general')
with patch('proposal_agent._make_client', return_value=fake):
    proposal_agent.classify_vertical(huge, user_api_key='sk-test')
sent = fake.calls[0]['messages'][0]['content']
test("only an excerpt is sent to the model",
     len(sent) <= proposal_agent.CLASSIFIER_MAX_CHARS + 200, len(sent))

print("\n=== classify_vertical: fallback paths ===")
fake = FakeCreateClient(answer='underwater_basket_weaving')
with patch('proposal_agent._make_client', return_value=fake):
    result = proposal_agent.classify_vertical(DC_TEXT, user_api_key='sk-test')
test("nonsense answer falls back to keywords", result == 'data_center', result)

fake = FakeCreateClient(error=RuntimeError('api down'))
with patch('proposal_agent._make_client', return_value=fake):
    result = proposal_agent.classify_vertical(DC_TEXT, user_api_key='sk-test')
test("API error falls back to keywords", result == 'data_center', result)

fake = FakeCreateClient(error=proposal_agent.AiBudgetExceeded('budget gone'))
with patch('proposal_agent._make_client', return_value=fake):
    result = proposal_agent.classify_vertical(DC_TEXT, user_api_key='sk-test')
test("budget exhaustion falls back (main call surfaces the error)",
     result == 'data_center', result)

with patch('proposal_agent._make_client') as mc, \
     patch('proposal_agent.ANTHROPIC_API_KEY', ''):
    result = proposal_agent.classify_vertical(DC_TEXT)
test("no API key -> keyword path without constructing a client",
     result == 'data_center' and not mc.called)

with patch('proposal_agent._make_client') as mc:
    result = proposal_agent.classify_vertical('   ', user_api_key='sk-test')
test("blank text skips the LLM call", result == 'general' and not mc.called)

print("\n=== keyword fallback still behaves ===")
test("keyword: data_center", detect_vertical(DC_TEXT) == 'data_center')
test("keyword: life_science",
     detect_vertical("GMP cleanroom validation IQ/OQ/PQ for aseptic pharma "
                     "fill finish suite with sterile WFI loop") == 'life_science')
test("keyword: food_beverage",
     detect_vertical("HACCP and FSMA compliant food processing line with "
                     "washdown sanitary design and USDA packaging line") == 'food_beverage')
test("keyword: weak signal -> general", detect_vertical(PLAIN_TEXT) == 'general')
test("keyword: empty -> general", detect_vertical('') == 'general')

print("\n=== draft_scope_of_work auto-detect uses the classifier ===")
scope_json = ('{"summary": "s", "items": [{"item": "Do the work", '
              '"category": "general"}]}')
with patch('proposal_agent.classify_vertical', return_value='food_beverage') as cv, \
     patch('proposal_agent._make_client', return_value=FakeStreamClient(scope_json)):
    result = proposal_agent.draft_scope_of_work(
        "some rfp text", vertical="auto", user_api_key='sk-test')
test("scope draft classifies via the LLM path",
     cv.called and result['vertical'] == 'food_beverage'
     and result['vertical_label'] == 'Food & Beverage / CPG',
     str(result))
with patch('proposal_agent.classify_vertical') as cv, \
     patch('proposal_agent._make_client', return_value=FakeStreamClient(scope_json)):
    result = proposal_agent.draft_scope_of_work(
        "some rfp text", vertical="data_center", user_api_key='sk-test')
test("explicit vertical skips classification",
     not cv.called and result['vertical'] == 'data_center')

print("\n=== generation job wiring ===")


def fake_generate(rfp_text, **kwargs):
    return {"proposal_markdown": "# P\n\n## Scope of Work\n\nx",
            "action_items": [], "confidence_score": 80, "document_type": "RFP",
            "vertical": kwargs.get("vertical") or "general",
            "vertical_label": "General",
            "generated_at": "2026-01-01T00:00:00+00:00"}


with app.app_context():
    db.drop_all(); db.create_all()
    c = app.test_client()
    c.post('/signup', data={'username': 'vic', 'email': 'vic@corp.com',
                            'password': 'password123', 'company_name': 'VicCo'})
    vic = User.query.filter_by(username='vic').first()
    vic.email_verified = True
    db.session.commit()

    c.post('/projects/new', data={'project_name': 'ClassifyBid', 'client_name': 'Acme'})
    proj = Project.query.filter_by(name='ClassifyBid').first()
    c.post(f'/projects/{proj.id}/upload', data={
        'file_type': 'rfp',
        'documents': (io.BytesIO(b'Build a facility control system'), 'rfp.txt'),
    }, content_type='multipart/form-data')

    with patch('app.generate_proposal', side_effect=fake_generate), \
         patch('app.classify_vertical', return_value='life_science') as cv:
        c.post(f'/projects/{proj.id}/generate',
               data={'vertical': 'auto', 'output_format': 'md'})
    prop = Proposal.query.filter_by(project_id=proj.id).first()
    test("auto generation classifies with the LLM and uses the result",
         cv.called and prop is not None and prop.vertical == 'life_science',
         f"called={cv.called} vertical={prop and prop.vertical}")

    # An approved scope's vertical takes precedence — no classify call at all.
    scope = ProjectScope.query.filter_by(project_id=proj.id).first()
    if not scope:
        scope = ProjectScope(project_id=proj.id, status='approved',
                             vertical='data_center', vertical_label='Data Center')
        db.session.add(scope)
    else:
        scope.status = 'approved'; scope.vertical = 'data_center'
    db.session.commit()
    with patch('app.generate_proposal', side_effect=fake_generate), \
         patch('app.classify_vertical') as cv:
        c.post(f'/projects/{proj.id}/generate',
               data={'vertical': 'auto', 'output_format': 'md'})
    latest = (Proposal.query.filter_by(project_id=proj.id)
              .order_by(Proposal.generated_at.desc()).first())
    test("approved scope vertical short-circuits classification",
         not cv.called and latest.vertical == 'data_center',
         f"called={cv.called} vertical={latest.vertical}")

print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
sys.exit(0 if failed == 0 else 1)

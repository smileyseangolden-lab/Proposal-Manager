"""Comprehensive test suite for Proposal Manager new features."""
import os
import sys
import uuid

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'

from app import app, db
from models import (
    User, Project, ProjectDocument, Proposal, ProposalQuestion,
    UserRateSheet, UserVerticalTemplate, ActivityLog,
    StaffRole, EquipmentItem, TravelExpenseRate,
    CompanyStandard, ProposalCorrection, ProposalVersion,
    ProposalComment,
)
from proposal_export import markdown_to_docx, markdown_to_redline_docx

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


with app.app_context():
    db.drop_all()
    db.create_all()
    client = app.test_client()

    # ===== AUTH =====
    print("\n=== Auth & Setup ===")
    client.post('/signup', data={
        'username': 'testuser', 'email': 'test@test.com',
        'password': 'testpass123', 'display_name': 'Test User',
        'company_name': 'Test Corp',
    })
    client.post('/login', data={'username': 'testuser', 'password': 'testpass123'})
    user = User.query.filter_by(username='testuser').first()
    test("Signup creates user", user is not None)
    test("User is admin (first user)", user.is_admin)

    # ===== SETTINGS PAGE =====
    print("\n=== Settings Page Rendering ===")
    resp = client.get('/settings')
    test("Settings page loads", resp.status_code == 200)
    test("Has Staff Rates section", b'Staff Hourly Sell Rates' in resp.data)
    test("Has Equipment section", b'Equipment' in resp.data and b'Materials Price List' in resp.data)
    test("Has Travel section", b'Travel' in resp.data and b'Expense Rates' in resp.data)
    test("Has Company Standards section", b'Company Standards' in resp.data and b'Posture' in resp.data)
    test("Has Rate Sheets section", b'Rate &amp; Price Sheets' in resp.data or b'Rate' in resp.data)

    # ===== STAFF ROLES CRUD =====
    print("\n=== Staff Roles CRUD ===")
    resp = client.post('/settings/add-staff-role', data={
        'role_name': 'Senior Engineer', 'category': 'Engineering',
        'hourly_rate': '175.00', 'overtime_rate': '262.50',
        'description': 'Controls/automation engineer',
    }, follow_redirects=True)
    test("Add staff role returns 200", resp.status_code == 200)
    test("Staff role created in DB", StaffRole.query.count() == 1)
    sr = StaffRole.query.first()
    test("Role name correct", sr.role_name == 'Senior Engineer')
    test("Hourly rate correct", sr.hourly_rate == 175.00)
    test("OT rate correct", sr.overtime_rate == 262.50)

    # Add another
    client.post('/settings/add-staff-role', data={
        'role_name': 'PM', 'category': 'Management',
        'hourly_rate': '200', 'overtime_rate': '',
    }, follow_redirects=True)
    test("Second role added", StaffRole.query.count() == 2)

    # Edit
    resp = client.post(f'/settings/edit-staff-role/{sr.id}', data={
        'role_name': 'Lead Engineer', 'category': 'Engineering',
        'hourly_rate': '185', 'overtime_rate': '277.50', 'description': 'Updated',
    }, follow_redirects=True)
    test("Edit returns 200", resp.status_code == 200)
    db.session.refresh(sr)
    test("Edit applied", sr.role_name == 'Lead Engineer' and sr.hourly_rate == 185.00)

    # Delete
    pm = StaffRole.query.filter_by(role_name='PM').first()
    resp = client.post(f'/settings/delete-staff-role/{pm.id}', follow_redirects=True)
    test("Delete returns 200", resp.status_code == 200)
    test("Deleted from DB", StaffRole.query.count() == 1)

    # Validation
    resp = client.post('/settings/add-staff-role', data={
        'role_name': '', 'hourly_rate': '',
    }, follow_redirects=True)
    test("Empty fields rejected gracefully", resp.status_code == 200 and StaffRole.query.count() == 1)

    resp = client.post('/settings/add-staff-role', data={
        'role_name': 'Bad', 'hourly_rate': 'abc',
    }, follow_redirects=True)
    test("Non-numeric rate rejected", resp.status_code == 200 and StaffRole.query.count() == 1)

    # ===== EQUIPMENT CRUD =====
    print("\n=== Equipment Items CRUD ===")
    resp = client.post('/settings/add-equipment-item', data={
        'item_name': 'PLC M580', 'eq_category': 'Controls',
        'part_number': 'BMEP584040', 'manufacturer': 'Schneider',
        'unit_cost': '4500', 'unit': 'each', 'eq_description': 'PLC',
    }, follow_redirects=True)
    test("Add equipment returns 200", resp.status_code == 200)
    test("Equipment created", EquipmentItem.query.count() == 1)
    eq = EquipmentItem.query.first()
    test("Part number correct", eq.part_number == 'BMEP584040')

    resp = client.post(f'/settings/delete-equipment-item/{eq.id}', follow_redirects=True)
    test("Delete equipment OK", EquipmentItem.query.count() == 0)

    # Re-add for later tests
    client.post('/settings/add-equipment-item', data={
        'item_name': 'VFD', 'eq_category': 'Electrical',
        'unit_cost': '2500', 'unit': 'each',
    })

    # ===== TRAVEL RATES CRUD =====
    print("\n=== Travel Rates CRUD ===")
    resp = client.post('/settings/add-travel-rate', data={
        'expense_type': 'Per Diem', 'travel_rate': '75',
        'travel_unit': 'per day', 'travel_description': 'GSA',
    }, follow_redirects=True)
    test("Add travel rate returns 200", resp.status_code == 200)
    test("Travel rate created", TravelExpenseRate.query.count() == 1)

    client.post('/settings/add-travel-rate', data={
        'expense_type': 'Hotel', 'travel_rate': '150',
        'travel_unit': 'per night',
    })
    test("Second travel rate added", TravelExpenseRate.query.count() == 2)

    tr = TravelExpenseRate.query.filter_by(expense_type='Hotel').first()
    resp = client.post(f'/settings/delete-travel-rate/{tr.id}', follow_redirects=True)
    test("Delete travel rate OK", TravelExpenseRate.query.count() == 1)

    # ===== COMPANY STANDARDS CRUD =====
    print("\n=== Company Standards CRUD ===")
    resp = client.post('/settings/add-company-standard', data={
        'standard_category': 'Certifications',
        'standard_title': 'ISO 9001:2015',
        'standard_content': 'We are certified to ISO 9001:2015.',
    }, follow_redirects=True)
    test("Add standard returns 200", resp.status_code == 200)
    test("Standard created", CompanyStandard.query.count() == 1)

    client.post('/settings/add-company-standard', data={
        'standard_category': 'Mission Statement',
        'standard_title': 'Our Mission',
        'standard_content': 'We deliver excellence in engineering.',
    })
    test("Second standard added", CompanyStandard.query.count() == 2)

    # Empty content should fail
    resp = client.post('/settings/add-company-standard', data={
        'standard_category': 'Other', 'standard_title': '', 'standard_content': '',
    }, follow_redirects=True)
    test("Empty standard rejected", CompanyStandard.query.count() == 2)

    cs = CompanyStandard.query.filter_by(title='Our Mission').first()
    resp = client.post(f'/settings/delete-company-standard/{cs.id}', follow_redirects=True)
    test("Delete standard OK", CompanyStandard.query.count() == 1)

    # ===== PROJECT UPLOAD WITH CHECKBOXES =====
    print("\n=== Project Upload with Checkboxes ===")
    client.post('/projects/new', data={
        'project_name': 'Test RFP', 'client_name': 'ACME',
    })
    project = Project.query.first()

    # Need to upload a document so the Generate section (with checkboxes) renders
    from pathlib import Path
    uploads_dir = Path(__file__).resolve().parent / 'uploads' / 'projects' / project.id
    uploads_dir.mkdir(parents=True, exist_ok=True)
    dummy_file = uploads_dir / 'test_rfp.txt'
    dummy_file.write_text('Request for Proposal: Build a control system.')
    doc = ProjectDocument(
        project_id=project.id, filename='test_rfp.txt',
        original_filename='test_rfp.txt', file_type='rfp',
        file_path=str(dummy_file), file_size=50,
    )
    db.session.add(doc)
    db.session.commit()

    resp = client.get(f'/projects/{project.id}/upload')
    test("Project upload loads", resp.status_code == 200)
    test("Has cost estimation section", b'Proposal Cost Estimation Options' in resp.data)
    test("Has staff types checkbox", b'Include estimate for staff types' in resp.data)
    test("Has staff hours checkbox", b'Include estimate for staff hours' in resp.data)
    test("Has equipment checkbox", b'Include equipment' in resp.data)
    test("Has travel checkbox", b'Include travel' in resp.data)
    test("Shows staff role count", b'role(s) configured' in resp.data)
    test("Shows equipment count", b'item(s) in price list' in resp.data)
    test("Shows travel rate count", b'rate(s) configured' in resp.data)

    # ===== SETTINGS WITH DATA =====
    print("\n=== Settings with Populated Data ===")
    resp = client.get('/settings')
    test("Shows existing staff role", b'Lead Engineer' in resp.data)
    test("Shows staff rate", b'185.00' in resp.data)
    test("Shows equipment item", b'VFD' in resp.data)
    test("Shows travel rate", b'Per Diem' in resp.data)
    test("Shows company standard", b'ISO 9001' in resp.data)

    # ===== PROPOSAL EDITOR =====
    print("\n=== Proposal Editor ===")
    # Create a proposal manually
    from pathlib import Path
    gen_dir = Path(__file__).resolve().parent / 'generated_proposals'
    gen_dir.mkdir(exist_ok=True)

    md_content = "# Test Proposal\n\nThis is a test proposal.\n\n## Scope\n\nTest scope content."
    md_file = 'proposal_test_001.md'
    (gen_dir / md_file).write_text(md_content)

    docx_file = 'proposal_test_001.docx'
    markdown_to_docx(md_content, str(gen_dir / docx_file))

    job_id = f'20260409_{uuid.uuid4().hex[:8]}'
    prop = Proposal(
        project_id=project.id, job_id=job_id,
        document_type='RFP', vertical='general', vertical_label='General',
        confidence_score=85, md_file=md_file, docx_file=docx_file,
    )
    db.session.add(prop)
    db.session.flush()

    v1 = ProposalVersion(
        proposal_id=prop.id, version_number=1,
        markdown_content=md_content, edit_source='ai',
        change_summary='AI-generated original',
    )
    db.session.add(v1)
    db.session.commit()

    # View proposal
    resp = client.get(f'/proposal/{prop.id}')
    test("View proposal loads", resp.status_code == 200)
    test("Has Edit button", b'Edit Proposal' in resp.data)
    test("Has Redline button", b'Download Redline' in resp.data)

    # Editor page
    resp = client.get(f'/proposal/{prop.id}/edit')
    test("Editor loads", resp.status_code == 200)
    test("Editor has content", b'Test Proposal' in resp.data)
    test("Has version history", b'Version History' in resp.data)
    test("Has v1 listed", b'v1' in resp.data)
    test("Shows AI source", b'AI' in resp.data)

    # Save an edit
    edited = md_content.replace('test proposal', 'professional proposal for ACME Corp')
    resp = client.post(f'/proposal/{prop.id}/edit', data={
        'markdown_content': edited,
        'change_summary': 'Updated language to be more professional',
    }, follow_redirects=True)
    test("Save edit returns 200", resp.status_code == 200)
    test("Version 2 created", ProposalVersion.query.filter_by(proposal_id=prop.id).count() == 2)

    v2 = ProposalVersion.query.filter_by(proposal_id=prop.id, version_number=2).first()
    test("v2 is human_web source", v2.edit_source == 'human_web')
    test("v2 has change summary", v2.change_summary == 'Updated language to be more professional')

    # View version
    resp = client.get(f'/proposal/{prop.id}/version/{v1.id}')
    test("View version loads", resp.status_code == 200)
    test("Shows version content", b'Test Proposal' in resp.data)

    # Restore version
    resp = client.post(f'/proposal/{prop.id}/restore/{v1.id}', follow_redirects=True)
    test("Restore version OK", resp.status_code == 200)
    test("Version 3 created (restore)", ProposalVersion.query.filter_by(proposal_id=prop.id).count() == 3)

    # ===== REDLINE EXPORT =====
    print("\n=== Redline DOCX Export ===")
    # First need v1 != latest for redline
    client.post(f'/proposal/{prop.id}/edit', data={
        'markdown_content': edited,
        'change_summary': 'Re-apply edits for redline test',
    })

    resp = client.get(f'/proposal/{prop.id}/redline')
    test("Redline download returns 200", resp.status_code == 200)
    test("Returns DOCX content type",
         'officedocument' in resp.content_type or 'application' in resp.content_type)

    # Test redline function directly
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as f:
        output = markdown_to_redline_docx(
            "# Original\n\nThis is the original text.\n\n## Section A\n\nOriginal content here.",
            "# Revised\n\nThis is the revised and improved text.\n\n## Section A\n\nUpdated content here.\n\n## Section B\n\nNew section added.",
            f.name,
            author="Test User"
        )
        test("Redline DOCX created", os.path.exists(output))
        test("Redline file has content", os.path.getsize(output) > 1000)
        os.unlink(output)

    # ===== FINALIZE & LEARN =====
    print("\n=== Finalize & Learn (AI Corrections) ===")
    resp = client.post(f'/proposal/{prop.id}/finalize', follow_redirects=True)
    test("Finalize returns 200", resp.status_code == 200)
    test("Correction created", ProposalCorrection.query.count() >= 1)
    corr = ProposalCorrection.query.first()
    test("Correction has summary", len(corr.correction_summary) > 0)
    test("Correction linked to user", corr.user_id == user.id)

    # ===== CROSS-USER PROTECTION =====
    print("\n=== Cross-User Protection ===")
    client.get('/logout')
    client.post('/signup', data={
        'username': 'user2', 'email': 'u2@test.com', 'password': 'testpass123',
    })
    client.post('/login', data={'username': 'user2', 'password': 'testpass123'})

    sr = StaffRole.query.first()
    resp = client.post(f'/settings/delete-staff-role/{sr.id}')
    test("Can't delete other user's staff role", resp.status_code == 404)

    eq = EquipmentItem.query.first()
    resp = client.post(f'/settings/delete-equipment-item/{eq.id}')
    test("Can't delete other user's equipment", resp.status_code == 404)

    cs = CompanyStandard.query.first()
    resp = client.post(f'/settings/delete-company-standard/{cs.id}')
    test("Can't delete other user's standard", resp.status_code == 404)

    resp = client.get(f'/proposal/{prop.id}/edit')
    test("Can't edit other user's proposal", resp.status_code == 404)

    # ===== PROPOSAL AGENT PROMPT =====
    print("\n=== Proposal Agent Prompt Building ===")
    from proposal_agent import _build_system_prompt
    from document_parser import load_vertical_resources, load_templates, load_reference_documents
    from config.settings import TEMPLATES_DIR, REFERENCE_DIR

    prompt = _build_system_prompt(
        'general',
        load_vertical_resources('general'),
        load_templates(TEMPLATES_DIR),
        load_reference_documents(REFERENCE_DIR),
        rate_sheet_text="Role: Engineer, Rate: $150/hr",
        company_name="Test Corp",
        cost_options={
            'include_staff_types': True,
            'include_staff_hours': True,
            'include_equipment_bom': True,
            'include_travel_expenses': True,
        },
        staff_roles_data=[
            {'role_name': 'Engineer', 'category': 'Engineering', 'hourly_rate': 150.0,
             'overtime_rate': 225.0, 'description': 'Controls engineer'}
        ],
        equipment_data=[
            {'item_name': 'PLC', 'category': 'Controls', 'part_number': 'M580',
             'manufacturer': 'Schneider', 'unit_cost': 4500.0, 'unit': 'each',
             'description': ''}
        ],
        travel_data=[
            {'expense_type': 'Per Diem', 'rate': 75.0, 'unit': 'per day', 'description': ''}
        ],
        past_corrections=[
            {'vertical': 'general', 'summary': 'Made tone more formal',
             'original': 'Hey', 'corrected': 'Dear Sir', 'type': 'tone'}
        ],
        company_standards=[
            {'category': 'Certifications', 'title': 'ISO 9001', 'content': 'We are certified.'}
        ],
    )

    test("Prompt includes cost estimation", 'Staff Type Estimation' in prompt)
    test("Prompt includes staff hours", 'Labor Cost Estimate' in prompt)
    test("Prompt includes equipment", 'Bill of Materials' in prompt)
    test("Prompt includes travel", 'Travel & Expense Estimation' in prompt)
    test("Prompt includes cost summary", 'Total Project Cost Summary' in prompt)
    test("Prompt includes company standards", 'Company Standards' in prompt)
    test("Prompt includes ISO 9001", 'ISO 9001' in prompt)
    test("Prompt includes learning block", 'Learning from Past Corrections' in prompt)
    test("Prompt includes correction detail", 'Made tone more formal' in prompt)
    test("Prompt includes staff role data", 'Engineer' in prompt and '$150.00' in prompt)
    test("Prompt includes equipment data", 'PLC' in prompt and '$4500.00' in prompt)
    test("Prompt includes travel data", 'Per Diem' in prompt and '$75.00' in prompt)

    # ===== DOCX EXPORT =====
    print("\n=== DOCX Export ===")
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as f:
        markdown_to_docx("# Proposal\n\n## Staffing Plan\n\n| Role | Hours | Rate |\n|---|---|---|\n| Engineer | 100 | $150 |\n", f.name)
        test("DOCX with tables created", os.path.exists(f.name) and os.path.getsize(f.name) > 1000)
        os.unlink(f.name)

    # ===== PART 2: RE-LOGIN AS ORIGINAL USER =====
    client.get('/logout')
    client.post('/login', data={'username': 'testuser', 'password': 'testpass123'})

    # ===== PART 2: DUE DATES & CALENDAR =====
    print("\n=== Part 2: Due Dates & Calendar ===")
    # Create a project with a due date
    from datetime import datetime as _dt, timedelta as _td
    due = (_dt.utcnow() + _td(days=5)).strftime('%Y-%m-%d')
    resp = client.post('/projects/new', data={
        'project_name': 'Deadline Project',
        'client_name': 'TimeSensitive LLC',
        'due_date': due,
    }, follow_redirects=True)
    test("New project with due_date returns 200", resp.status_code == 200)
    dl_project = Project.query.filter_by(name='Deadline Project').first()
    test("Due date saved", dl_project is not None and dl_project.due_date is not None)
    test("Due date correct day", dl_project.due_date.strftime('%Y-%m-%d') == due)

    # Update the due date
    new_due = (_dt.utcnow() + _td(days=10)).strftime('%Y-%m-%d')
    resp = client.post(f'/projects/{dl_project.id}/set-due-date', data={
        'due_date': new_due,
    }, follow_redirects=True)
    test("Set due date returns 200", resp.status_code == 200)
    db.session.refresh(dl_project)
    test("Due date updated", dl_project.due_date.strftime('%Y-%m-%d') == new_due)

    # Clear due date
    client.post(f'/projects/{dl_project.id}/set-due-date', data={'due_date': ''}, follow_redirects=True)
    db.session.refresh(dl_project)
    test("Due date cleared", dl_project.due_date is None)

    # Set it to a date within 7 days for dashboard "Due Soon" widget
    soon_due = (_dt.utcnow() + _td(days=3)).strftime('%Y-%m-%d')
    client.post(f'/projects/{dl_project.id}/set-due-date', data={'due_date': soon_due})

    # Calendar view
    resp = client.get('/calendar')
    test("Calendar loads", resp.status_code == 200)
    test("Calendar shows project", b'Deadline Project' in resp.data)

    # Calendar with explicit year/month
    now = _dt.utcnow()
    resp = client.get(f'/calendar?year={now.year}&month={now.month}')
    test("Calendar with year/month loads", resp.status_code == 200)

    # Dashboard should show upcoming deadline
    resp = client.get('/')
    test("Dashboard loads with deadlines", resp.status_code == 200)
    test("Dashboard shows upcoming deadline", b'Due Soon' in resp.data)

    # ===== PART 2: WIN/LOSS CAPTURE =====
    print("\n=== Part 2: Win/Loss Analysis ===")
    # Create a project and close it as lost with reason/competitor
    client.post('/projects/new', data={
        'project_name': 'Lost Deal Project',
        'client_name': 'BigCorp',
    }, follow_redirects=True)
    lost_proj = Project.query.filter_by(name='Lost Deal Project').first()

    resp = client.post(f'/projects/{lost_proj.id}/update-status', data={
        'status': 'lost',
        'close_reason': 'Price was 15% higher than competitor',
        'close_category': 'price',
        'competitor_name': 'Acme Controls',
        'dollar_amount': '250000',
    }, follow_redirects=True)
    test("Update status to lost returns 200", resp.status_code == 200)
    db.session.refresh(lost_proj)
    test("Status is lost", lost_proj.status == 'lost')
    test("Close reason saved", 'Price' in (lost_proj.close_reason or ''))
    test("Close category saved", lost_proj.close_category == 'price')
    test("Competitor saved", lost_proj.competitor_name == 'Acme Controls')
    test("Dollar amount saved", lost_proj.dollar_amount == 250000.0)
    test("Closed_at stamped", lost_proj.closed_at is not None)

    # Create a won project
    client.post('/projects/new', data={'project_name': 'Won Deal', 'client_name': 'GoodCorp'}, follow_redirects=True)
    won_proj = Project.query.filter_by(name='Won Deal').first()
    client.post(f'/projects/{won_proj.id}/update-status', data={
        'status': 'won',
        'close_reason': 'Strong technical approach and past performance',
        'close_category': 'technical',
        'dollar_amount': '500000',
    }, follow_redirects=True)
    db.session.refresh(won_proj)
    test("Won project saved", won_proj.status == 'won' and won_proj.close_category == 'technical')

    # Update close details after the fact
    resp = client.post(f'/projects/{won_proj.id}/close-details', data={
        'close_reason': 'Updated reason',
        'close_category': 'relationship',
        'competitor_name': 'OtherCorp',
        'dollar_amount': '550000',
    }, follow_redirects=True)
    test("Close-details update returns 200", resp.status_code == 200)
    db.session.refresh(won_proj)
    test("Close reason updated", won_proj.close_reason == 'Updated reason')
    test("Close category updated", won_proj.close_category == 'relationship')
    test("Competitor updated", won_proj.competitor_name == 'OtherCorp')
    test("Dollar updated", won_proj.dollar_amount == 550000.0)

    # Reports page
    resp = client.get('/reports')
    test("Reports page loads", resp.status_code == 200)
    test("Reports shows Acme Controls", b'Acme Controls' in resp.data)
    test("Reports shows OtherCorp", b'OtherCorp' in resp.data)
    test("Reports has win rate", b'Win Rate' in resp.data)
    test("Reports has trend chart", b'trend-chart' in resp.data)

    # ===== PART 2: SEARCH =====
    print("\n=== Part 2: Global Search ===")
    resp = client.get('/search')
    test("Empty search loads", resp.status_code == 200)

    resp = client.get('/search?q=a')
    test("Too-short search loads", resp.status_code == 200)

    resp = client.get('/search?q=Deadline')
    test("Search for project name", resp.status_code == 200 and b'Deadline Project' in resp.data)

    resp = client.get('/search?q=Acme')
    test("Search finds competitor", resp.status_code == 200 and b'Lost Deal' in resp.data)

    resp = client.get('/search?q=BigCorp')
    test("Search finds client name", resp.status_code == 200 and b'Lost Deal' in resp.data)

    # ===== PART 2: PROPOSAL COMMENTS =====
    print("\n=== Part 2: Proposal Comments ===")
    # Use the existing proposal from earlier tests
    prop_for_comments = Proposal.query.first()
    test("Have a proposal to comment on", prop_for_comments is not None)

    resp = client.post(f'/proposal/{prop_for_comments.id}/comments', data={
        'body': 'Needs stronger pricing justification',
        'section_anchor': 'Pricing',
    }, follow_redirects=True)
    test("Add comment returns 200", resp.status_code == 200)
    test("Comment saved", ProposalComment.query.count() == 1)
    c1 = ProposalComment.query.first()
    test("Comment body correct", c1.body == 'Needs stronger pricing justification')
    test("Comment anchor correct", c1.section_anchor == 'Pricing')
    test("Comment not resolved initially", c1.is_resolved is False)
    test("Comment author correct", c1.author_id == user.id)

    # Empty comment rejected
    resp = client.post(f'/proposal/{prop_for_comments.id}/comments', data={'body': ''}, follow_redirects=True)
    test("Empty comment rejected", ProposalComment.query.count() == 1)

    # Add second comment
    client.post(f'/proposal/{prop_for_comments.id}/comments', data={'body': 'Second note'}, follow_redirects=True)
    test("Second comment added", ProposalComment.query.count() == 2)

    # Resolve comment
    resp = client.post(f'/proposal/{prop_for_comments.id}/comments/{c1.id}/resolve', follow_redirects=True)
    test("Resolve comment returns 200", resp.status_code == 200)
    db.session.refresh(c1)
    test("Comment marked resolved", c1.is_resolved is True)
    test("Resolver recorded", c1.resolved_by == user.id)

    # Unresolve
    client.post(f'/proposal/{prop_for_comments.id}/comments/{c1.id}/resolve', follow_redirects=True)
    db.session.refresh(c1)
    test("Comment unresolved", c1.is_resolved is False)

    # Proposal page shows comments
    resp = client.get(f'/proposal/{prop_for_comments.id}')
    test("Proposal page shows comments section", b'Review Comments' in resp.data)
    test("Proposal page shows comment body", b'Needs stronger pricing' in resp.data)

    # Delete comment (as author)
    c2 = ProposalComment.query.filter(ProposalComment.id != c1.id).first()
    resp = client.post(f'/proposal/{prop_for_comments.id}/comments/{c2.id}/delete', follow_redirects=True)
    test("Delete comment returns 200", resp.status_code == 200)
    test("Comment deleted", ProposalComment.query.count() == 1)

    # Search finds comments
    resp = client.get('/search?q=pricing')
    test("Search finds comments", b'Needs stronger pricing' in resp.data)

    # ===== PART 2: CROSS-USER COMMENT PROTECTION =====
    print("\n=== Part 2: Cross-user Comment Protection ===")
    client.get('/logout')
    client.post('/signup', data={
        'username': 'user3', 'email': 'u3@test.com', 'password': 'testpass123',
    })
    client.post('/login', data={'username': 'user3', 'password': 'testpass123'})

    # user3 can't post comments on user1's proposal
    resp = client.post(f'/proposal/{prop_for_comments.id}/comments', data={'body': 'Intrusion'})
    test("Other user can't comment on proposal", resp.status_code == 404)

    # user3 can't resolve user1's comment
    resp = client.post(f'/proposal/{prop_for_comments.id}/comments/{c1.id}/resolve')
    test("Other user can't resolve comment", resp.status_code == 404)

    # user3 search doesn't leak user1's projects (user3 is not admin since they're not first user)
    resp = client.get('/search?q=Deadline')
    test("Other user can't find user1's projects in search", b'Deadline Project' not in resp.data)

    # ===== SUMMARY =====
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests")
    if failed:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")

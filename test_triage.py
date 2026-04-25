"""Tests for the Pre-Proposal Triage Phase 1 feature.

Mirrors the style of test_features.py: custom `test()` helper, in-memory
SQLite, no pytest. Run with `python test_triage.py`.
"""
from __future__ import annotations

import io
import os
import sys
import zipfile

os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-12345'

from app import app, db  # noqa: E402
from bid_package_service import (  # noqa: E402
    BidPackageError,
    MAX_PACKAGE_SIZE_BYTES,
    ingest_zip_filelike,
)
from context_retrieval import retrieve_context  # noqa: E402
from etg_knowledge_seed import (  # noqa: E402
    migrate_company_standards,
    seed_system_assets,
    upsert_from_company_standard,
)
from models import (  # noqa: E402
    BidPackage,
    CompanyStandard,
    DocumentAnalysis,
    EtgKnowledgeAsset,
    Project,
    ProjectDocument,
    TriageJob,
    User,
)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['TESTING'] = True

passed = 0
failed = 0


def test(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name} - {detail}")


def make_zip(files: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    buf.seek(0)
    return buf


with app.app_context():
    db.drop_all()
    db.create_all()
    client = app.test_client()

    # ===== AUTH =====
    print("\n=== Auth & Setup ===")
    client.post('/signup', data={
        'username': 'aeuser', 'email': 'ae@etg.test',
        'password': 'testpass123', 'display_name': 'Stephanie',
        'company_name': 'E Tech Group',
    })
    client.post('/login', data={'username': 'aeuser', 'password': 'testpass123'})
    user = User.query.filter_by(username='aeuser').first()
    test("User created", user is not None)

    # ===== ETG KNOWLEDGE SEED =====
    print("\n=== ETG Knowledge Seed ===")
    seed_system_assets()
    taxonomy_count = EtgKnowledgeAsset.query.filter_by(
        user_id=None, section='taxonomy'
    ).count()
    expected_count = EtgKnowledgeAsset.query.filter_by(
        user_id=None, section='expected_documents'
    ).count()
    test("System taxonomy seeded", taxonomy_count >= 2,
         f"got {taxonomy_count}")
    test("Expected-document checklists seeded for verticals",
         expected_count >= 4, f"got {expected_count}")

    # Idempotency: running again does not duplicate.
    seed_system_assets()
    test("Seed is idempotent",
         EtgKnowledgeAsset.query.filter_by(user_id=None, section='taxonomy').count()
         == taxonomy_count)

    # ===== COMPANY STANDARD MIGRATION =====
    print("\n=== CompanyStandard → EtgKnowledgeAsset Mirror ===")
    cs = CompanyStandard(
        user_id=user.id,
        category='Mission Statement',
        title='ETG Mission',
        content='Engineer the world\'s most advanced operations.',
    )
    db.session.add(cs)
    db.session.commit()

    upsert_from_company_standard(cs)
    mirrored = EtgKnowledgeAsset.query.filter_by(
        user_id=user.id, source='migrated_company_standard', title='ETG Mission'
    ).first()
    test("CompanyStandard mirrored to EtgKnowledgeAsset", mirrored is not None)
    test("Mirror picks correct section",
         mirrored is not None and mirrored.section == 'company_profile')

    # Idempotency on upsert.
    upsert_from_company_standard(cs)
    count = EtgKnowledgeAsset.query.filter_by(
        user_id=user.id, source='migrated_company_standard', title='ETG Mission'
    ).count()
    test("Upsert is idempotent", count == 1)

    # bulk migration helper
    cs2 = CompanyStandard(
        user_id=user.id, category='Certifications', title='CSIA Enterprise',
        content='Certified Enterprise CSI integrator since 2015.',
    )
    db.session.add(cs2)
    db.session.commit()
    inserted = migrate_company_standards()
    test("migrate_company_standards inserted new rows", inserted >= 1)

    # ===== CONTEXT RETRIEVAL =====
    print("\n=== Context Retrieval ===")
    block = retrieve_context(['company_profile'], user_id=user.id, max_chars=1000)
    test("Retrieval returns the migrated mission",
         'ETG Mission' in block, block[:80])
    block = retrieve_context(['taxonomy'], user_id=user.id, max_chars=200)
    test("Retrieval honors max_chars", len(block) <= 260,
         f"len={len(block)}")
    empty = retrieve_context(['nonexistent_section'], user_id=user.id)
    test("Retrieval returns empty for missing sections", empty == "")

    # ===== ETG KNOWLEDGE ROUTES =====
    print("\n=== ETG Knowledge Settings UI ===")
    resp = client.get('/settings/etg-knowledge')
    test("ETG knowledge index loads", resp.status_code == 200)
    test("Index page lists section headings",
         b'Capabilities Matrix' in resp.data and b'Technology Stack' in resp.data)

    resp = client.post('/settings/etg-knowledge/add', data={
        'section': 'capabilities',
        'title': 'Pharma Cleanroom Capability',
        'content_md': 'ISO 5/7/8 cleanroom MEP/automation experience.',
        'vertical': 'life_science',
        'tags': 'cleanroom,pharma',
    }, follow_redirects=True)
    test("Add knowledge asset succeeds", resp.status_code == 200)
    asset = EtgKnowledgeAsset.query.filter_by(
        user_id=user.id, title='Pharma Cleanroom Capability'
    ).first()
    test("Asset persisted", asset is not None)
    test("Asset section saved",
         asset is not None and asset.section == 'capabilities')

    # ===== BID PACKAGE INGESTION =====
    print("\n=== Bid Package Ingestion ===")
    resp = client.post('/projects/new', data={
        'project_name': 'Acme Pharma Project',
        'client_name': 'Acme Bio',
    }, follow_redirects=False)
    project = Project.query.filter_by(name='Acme Pharma Project').first()
    test("Project created for triage test", project is not None)

    zip_contents = {
        'Base/RFP_Narrative.pdf': b'%PDF-1.4 fake pdf bytes',
        'Base/Specifications/spec_section_25_5000.txt': (
            b'Building Management System sequence of operations. '
            b'Includes IO points and equipment list.'
        ),
        'Addendum 1/spec_section_25_5000.txt': (
            b'Building Management System sequence of operations. '
            b'Updated agitator capacity and added one more skid.'
        ),
        '__MACOSX/junk_file': b'should be skipped',
    }
    zip_file = make_zip(zip_contents)

    resp = client.post(
        f'/projects/{project.id}/bid-package/upload',
        data={'bid_package_zip': (zip_file, 'acme_bid_package.zip')},
        content_type='multipart/form-data',
        follow_redirects=False,
    )
    test("Bid package upload returns redirect",
         resp.status_code == 302, f"got {resp.status_code}")

    package = BidPackage.query.filter_by(project_id=project.id).first()
    test("Bid package row created", package is not None)
    test("Bid package status = ready",
         package is not None and package.status == 'ready',
         package.status if package else "no package")
    test("Junk files skipped", package is not None and package.file_count == 3)

    docs = ProjectDocument.query.filter_by(bid_package_id=package.id).all()
    test("ProjectDocument rows created", len(docs) == 3)
    test("Documents have sha256",
         all(d.sha256 for d in docs))
    test("Documents preserve relative path",
         any(d.relative_path.startswith('Base/') for d in docs))

    # Re-uploading the same zip should hit the dedup path (same hashes already present).
    zip_file_2 = make_zip(zip_contents)
    resp2 = client.post(
        f'/projects/{project.id}/bid-package/upload',
        data={'bid_package_zip': (zip_file_2, 'acme_bid_package.zip')},
        content_type='multipart/form-data',
        follow_redirects=False,
    )
    test("Re-upload returns redirect",
         resp2.status_code == 302, f"got {resp2.status_code}")
    packages = BidPackage.query.filter_by(project_id=project.id).all()
    second = [p for p in packages if p.id != package.id]
    test("Second package row exists", len(second) == 1)
    test("Second package ingested zero new docs (all duplicates)",
         second and second[0].file_count == 0,
         f"file_count={second[0].file_count if second else 'n/a'}")

    # ===== INDEX PAGE =====
    print("\n=== Index Page ===")
    resp = client.get(f'/projects/{project.id}/bid-package')
    test("Bid package landing loads", resp.status_code == 200)
    test("Landing shows package listing",
         b'Bid Packages' in resp.data)

    resp = client.get(f'/projects/{project.id}/bid-package/{package.id}')
    test("Bid package index loads", resp.status_code == 200)
    test("Index shows the document filenames",
         b'spec_section_25_5000' in resp.data)

    # XLSX export.
    resp = client.get(f'/projects/{project.id}/bid-package/{package.id}.xlsx')
    test("XLSX export returns 200", resp.status_code == 200)
    test("XLSX content type set",
         'spreadsheetml' in resp.headers.get('Content-Type', ''))

    # ===== ANALYSIS QUEUEING =====
    print("\n=== Analysis Job Queue ===")
    resp = client.post(
        f'/projects/{project.id}/bid-package/{package.id}/analyze',
        follow_redirects=False,
    )
    test("Analyze enqueue returns redirect",
         resp.status_code == 302, f"got {resp.status_code}")
    queued = TriageJob.query.filter_by(bid_package_id=package.id, status='pending').count()
    test("Queued one job per document", queued == 3, f"got {queued}")

    # ===== REVIEWER STATUS =====
    print("\n=== Per-Document Review State ===")
    sample_doc = docs[0]
    # Stub an analysis row so the route accepts the review.
    analysis = DocumentAnalysis(
        document_id=sample_doc.id,
        project_id=sample_doc.project_id,
        bid_package_id=package.id,
        status='analyzed',
        synopsis='Test synopsis.',
    )
    db.session.add(analysis)
    db.session.commit()

    resp = client.post(
        f'/projects/{project.id}/bid-package/{package.id}/document/{sample_doc.id}/review',
        data={'reviewer_status': 'flagged', 'reviewer_notes': 'Missing IO list?'},
        follow_redirects=False,
    )
    test("Review state save redirects",
         resp.status_code == 302, f"got {resp.status_code}")
    db.session.refresh(analysis)
    test("Review state recorded",
         analysis.reviewer_status == 'flagged'
         and 'IO list' in (analysis.reviewer_notes or ''))

    # ===== SIZE CAP REJECTION (unit-level) =====
    print("\n=== Size Cap Enforcement ===")
    too_big = make_zip({'big_file.txt': b'x' * 1024})
    # Force a reject by bumping the limit envvar locally won't take effect after
    # import, so we test the function path directly with a stub.
    try:
        # Create an oversized zip member by pretending the limit is tiny.
        import bid_package_service
        original = bid_package_service.MAX_PACKAGE_SIZE_BYTES
        bid_package_service.MAX_PACKAGE_SIZE_BYTES = 10
        too_big.seek(0)

        class _Stream:
            def __init__(self, b):
                self.stream = b
                self.filename = 'too_big.zip'
        try:
            ingest_zip_filelike(
                project_id=project.id,
                user_id=user.id,
                file_storage=_Stream(too_big),
                original_filename='too_big.zip',
            )
            raised = False
        except BidPackageError:
            raised = True
        bid_package_service.MAX_PACKAGE_SIZE_BYTES = original
    except Exception as exc:
        raised = False
        print(f"  unexpected: {exc}")
    test("Size cap raises BidPackageError", raised)

    # ===== SUMMARY =====
    print(f"\n=== Results ===\n  Passed: {passed}\n  Failed: {failed}")
    sys.exit(0 if failed == 0 else 1)

"""Proven native conflicts on Mylar records offer a safe recheck or skip."""

from unittest.mock import Mock

import pytest
from sqlalchemy import select

from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.services.import_review_source_actions import (
    finish_source_action,
    process_source_action,
)
from tests.integration.test_metron_folder_import import _archive
from tests.integration.test_mylar_native_extras import seed_mylar_native_extra
from tests.ui.test_import_review_file_routes import csrf, token

pytest_plugins = ["conftest_security"]


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("correction", ["none", "links", "file"])
@pytest.mark.parametrize("in_place", [False, True])
async def test_recorded_mylar_conflict_recheck_keeps_ownership_and_checks_fresh_evidence(
    authenticated_client, sec_db, tmp_path, monkeypatch, namespace, correction, in_place
):
    async with sec_db() as session:
        job, service, _, _, legacy, issue, originals, extra = await seed_mylar_native_extra(
            session, tmp_path, namespace, in_place=in_place, mapped_source=not in_place
        )
        tracked = extra.parent / "Old Harbor 001.cbz"
        originals[tracked] = _archive(tracked, primary=namespace, number="1")
        await service.start_scan(session, job.id)
        file = await session.scalar(
            select(ImportedFile).where(ImportedFile.file_path == str(tracked))
        )
        assert file.status == ImportedFileStatus.NO_MATCH
        job_id, file_id, parent_id, issue_id = job.id, file.id, file.import_series_id, issue.id
        if correction == "links":
            series_link = await session.scalar(
                select(SeriesExternalIdentity).where(SeriesExternalIdentity.external_id == "21")
            )
            issue_link = await session.scalar(
                select(IssueExternalIdentity).where(IssueExternalIdentity.external_id == "101")
            )
            series_link.series_id = legacy.id
            issue_link.issue_id = issue.id
            await session.commit()
        elif correction == "file":
            originals[tracked] = _archive(
                tracked, primary=namespace, number="1", series_id="22", issue_id="102"
            )
    review = await authenticated_client.get(
        f"/import/{job_id}/review-partial?status=decide&reason=needs_issue"
    )
    assert review.status_code == 200
    assert 'data-testid="import-review-mylar-native-conflict"' in review.text
    assert "Recheck source" in review.text and "Review linked metadata" in review.text
    assert "Skip this file" in review.text
    preview = await authenticated_client.get(f"/import/{job_id}/files/{file_id}/source")
    assert preview.status_code == 200
    trigger = Mock()
    monkeypatch.setattr("pullbox.tasks.import_task.trigger_import_review_source_action", trigger)
    response = await authenticated_client.post(
        f"/import/{job_id}/files/{file_id}/source",
        headers=csrf(authenticated_client),
        data={"source_action": "recheck", "token": token(preview.text)},
    )
    assert response.status_code == 200, response.text
    trigger.assert_called_once_with(job_id, file_id)
    rematch = await process_source_action(sec_db, job_id, file_id)
    async with sec_db() as session:
        checked = await session.get(ImportedFile, file_id)
        assert rematch == parent_id, repr(checked.diagnostics)
    async with sec_db() as session:
        await service.rematch_imported_series_files(session, job_id, parent_id)
        await session.commit()
    await finish_source_action(sec_db, job_id, file_id)
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        assert file.import_series_id == parent_id
        assert file.diagnostics["source_metadata"]["mylar3_issue"]
        assert file.diagnostics["source_metadata"]["metron_import"]["identities"]
        if correction == "none":
            assert file.status == ImportedFileStatus.NO_MATCH
            assert file.diagnostics["conflict_type"] == "mylar_native_identity_conflict"
            assert not file.include_in_import
        else:
            assert file.status == ImportedFileStatus.MATCHED
            assert file.matched_issue_id == issue_id
    assert all(path.read_bytes() == content for path, content in originals.items())


@pytest.mark.parametrize("decision", ["skip", "assign"])
async def test_recorded_mylar_conflict_can_be_skipped_but_not_overridden(
    authenticated_client, sec_db, tmp_path, decision
):
    async with sec_db() as session:
        job, service, _, _, _, _, originals, extra = await seed_mylar_native_extra(
            session, tmp_path
        )
        tracked = extra.parent / "Old Harbor 001.cbz"
        originals[tracked] = _archive(tracked, number="1")
        await service.start_scan(session, job.id)
        file = await session.scalar(
            select(ImportedFile).where(ImportedFile.file_path == str(tracked))
        )
        job_id, file_id, parent_id = job.id, file.id, file.import_series_id
    response = await authenticated_client.post(
        f"/api/v1/import/{job_id}/series/{parent_id}/reconcile",
        json={
            "decisions": [
                {
                    "imported_file_id": file_id,
                    "action": decision,
                    **({"issue_cv_id": 901} if decision == "assign" else {}),
                }
            ]
        },
        headers=csrf(authenticated_client),
    )
    assert response.status_code == (200 if decision == "skip" else 409), response.text
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        assert file.status == (
            ImportedFileStatus.SKIPPED if decision == "skip" else ImportedFileStatus.NO_MATCH
        )
        assert not file.include_in_import
        assert file.import_series_id == parent_id
    assert all(path.read_bytes() == content for path, content in originals.items())

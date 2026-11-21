"""Unrecorded native Mylar archives remain actionable after finishing an import."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.models.library import LibraryFile
from pullbox.models.metadata_identity import IssueExternalIdentity
from pullbox.schemas.import_job import ConfirmImportRequest
from tests.integration.test_mylar_native_extras import seed_mylar_native_extra
from tests.ui.test_import_review_file_routes import csrf, token

pytest_plugins = ["conftest_security"]


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
async def test_mylar_ready_selection_names_the_matched_series_not_its_source_folder(
    authenticated_client, sec_db, tmp_path, namespace
):
    async with sec_db() as session:
        job, service, native, _issue, legacy, *_ = await seed_mylar_native_extra(
            session, tmp_path, namespace
        )
        await service.start_scan(session, job.id)
        job_id = job.id
        native_title, legacy_title = native.title, legacy.title
    response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=ready")
    assert response.status_code == 200, response.text
    assert response.text.count(f'aria-label="Import ready files for {native_title}"') == 1
    assert response.text.count(f'aria-label="Import ready files for {legacy_title}"') == 1


async def completed_mylar_extra(session, tmp_path, namespace, **kwargs):
    (
        job,
        service,
        native,
        issue,
        _recorded,
        recorded_issue,
        originals,
        extra,
    ) = await seed_mylar_native_extra(session, tmp_path, namespace, **kwargs)
    link = await session.scalar(
        select(IssueExternalIdentity).where(IssueExternalIdentity.issue_id == issue.id)
    )
    link.verification_state = IdentityVerificationState.STALE
    await session.commit()
    await service.start_scan(session, job.id)
    file = await session.scalar(
        select(ImportedFile).where(
            ImportedFile.import_job_id == job.id, ImportedFile.file_path == str(extra)
        )
    )
    assert file.status == ImportedFileStatus.NO_MATCH
    tracked = await session.scalar(
        select(ImportedFile).where(
            ImportedFile.import_job_id == job.id, ImportedFile.matched_issue_id == recorded_issue.id
        )
    )
    await service.update_file_selection(session, job.id, tracked.id, include_in_import=True)
    await service.confirm_import(
        session, job.id, ConfirmImportRequest(series_ids=[tracked.import_series_id])
    )
    await service.run_import(session, job.id)
    link.verification_state = IdentityVerificationState.VERIFIED
    await session.commit()
    service._metadata_service.reset_mock()
    return job.id, file.id, file.import_series_id, issue.id, native.id, service, originals


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
async def test_mylar_native_extra_has_actionable_signed_follow_up(
    authenticated_client, sec_db, tmp_path, monkeypatch, namespace
):
    async with sec_db() as session:
        (
            job_id,
            file_id,
            parent_id,
            issue_id,
            _series_id,
            service,
            originals,
        ) = await completed_mylar_extra(session, tmp_path, namespace)
    monkeypatch.setattr(
        "pullbox.composition.services.build_import_service", AsyncMock(return_value=service)
    )
    preview = await authenticated_client.get(f"/import/orphaned/{parent_id}/find-series")
    assert preview.status_code == 200, preview.text
    assert 'data-testid="import-library-series-choices"' in preview.text
    assert "Search ComicVine" not in preview.text
    choices = await authenticated_client.get(
        f"/import/{job_id}/files/{file_id}/assign-native?follow_up=true"
    )
    assert choices.status_code == 200, choices.text
    result = await authenticated_client.post(
        f"/api/v1/import/orphaned/{parent_id}/recover",
        headers=csrf(authenticated_client),
        json={
            "decisions": [
                {
                    "imported_file_id": file_id,
                    "action": "assign",
                    "library_issue_id": issue_id,
                    "review_token": token(choices.text),
                }
            ]
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["imported_count"] == 1 and result.json()["files_remaining"] == 0
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        assert file.status == ImportedFileStatus.IMPORTED
        assert await session.scalar(select(LibraryFile.id).where(LibraryFile.issue_id == issue_id))
        assert await service.rollback_import(session, job_id)
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert service._metadata_service.mock_calls == []

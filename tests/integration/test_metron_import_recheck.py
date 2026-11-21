"""Metron link corrections can resume the same import without losing decisions."""

from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
from pullbox.services.import_metron_matching import prepare_metron_series_recheck
from tests.integration.test_metron_folder_import import _archive, _service, _setup


async def _unlinked_scan(session: AsyncSession, root: Path):
    destination, _, issue = await _setup(session, root)
    link = (await session.scalars(select(SeriesExternalIdentity))).one()
    link.verification_state = IdentityVerificationState.STALE
    await session.commit()
    source = root / "source"
    comic = source / "Unsorted" / "unidentified.cbz"
    original = _archive(comic)
    service = _service()
    job = await service.create_job(
        session,
        ImportJobCreate(
            source_path=str(source),
            source_type="filesystem",
            target_library_root_id=destination.id,
        ),
    )
    await service.start_scan(session, job.id)
    file = (await session.scalars(select(ImportedFile))).one()
    group = await session.get(ImportedSeries, file.import_series_id)
    assert group is not None and group.status == ImportSeriesStatus.NO_MATCH
    assert file.status == ImportedFileStatus.NO_MATCH
    return service, job, group, file, link, issue, comic, original


async def test_recheck_links_verified_metron_series_without_new_scan(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    service, job, group, file, link, issue, comic, original = await _unlinked_scan(
        db_session, tmp_path
    )
    signature = deepcopy(file.source_signature)
    evidence = deepcopy(file.diagnostics["source_metadata"])
    link.verification_state = IdentityVerificationState.VERIFIED
    await db_session.commit()

    result = await prepare_metron_series_recheck(db_session, job.id, group.id)
    assert result.id == group.id
    assert result.diagnostics["rematch_pending"] is True
    assert file.include_in_import is False
    await db_session.commit()
    await service.rematch_imported_series_files(db_session, job.id, group.id)
    await db_session.refresh(file)
    assert job.status == ImportJobStatus.REVIEW
    assert group.status == ImportSeriesStatus.DUPLICATE
    assert file.status == ImportedFileStatus.MATCHED
    assert file.matched_issue_id == issue.id
    assert file.include_in_import is False
    assert file.source_signature == signature
    assert file.diagnostics["source_metadata"] == evidence
    assert comic.read_bytes() == original
    assert "rejection_reason" not in group.diagnostics
    assert "rematch_pending" not in group.diagnostics
    assert job.total_files_matched == 1
    assert job.series_no_match == 0

    await service.update_file_selection(db_session, job.id, file.id, include_in_import=True)
    await service.confirm_import(db_session, job.id, ConfirmImportRequest(series_ids=[group.id]))
    await service.run_import(db_session, job.id)
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.IMPORTED
    assert await service.rollback_import(db_session, job.id)
    assert comic.read_bytes() == original
    service._metadata_service._provider.search_series.assert_not_awaited()
    service._metadata_service._provider.get_series.assert_not_awaited()
    service._metadata_service._provider.get_issues_for_series.assert_not_awaited()


@pytest.mark.parametrize(
    "protected_status",
    [
        ImportedFileStatus.SKIPPED,
        ImportedFileStatus.SAFETY_BLOCKED,
        ImportedFileStatus.CONFIRMED,
        ImportedFileStatus.ALREADY_OWNED,
        ImportedFileStatus.CONFLICT,
    ],
)
async def test_recheck_preserves_other_file_decisions(
    db_session: AsyncSession, tmp_path: Path, protected_status: ImportedFileStatus
) -> None:
    _, job, group, file, link, _, _, _ = await _unlinked_scan(db_session, tmp_path)
    link.verification_state = IdentityVerificationState.VERIFIED
    protected = ImportedFile(
        import_job_id=job.id,
        import_series_id=group.id,
        file_path=str(tmp_path / "source" / "protected.cbz"),
        file_name="protected.cbz",
        file_format="cbz",
        status=protected_status,
        include_in_import=protected_status == ImportedFileStatus.CONFIRMED,
        diagnostics={"kind": "user_decision", "decision": "retained"},
    )
    db_session.add(protected)
    await db_session.commit()
    before = (protected.status, protected.include_in_import, deepcopy(protected.diagnostics))
    await prepare_metron_series_recheck(db_session, job.id, group.id)
    await db_session.refresh(protected)
    assert (protected.status, protected.include_in_import, protected.diagnostics) == before
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.PENDING


@pytest.mark.parametrize(
    "blocked", ["unverified", "other_job", "busy", "skipped", "override", "mixed"]
)
async def test_recheck_rejects_ineligible_or_conflicting_scope_without_changes(
    db_session: AsyncSession, tmp_path: Path, blocked: str
) -> None:
    _, job, group, file, link, _, _, _ = await _unlinked_scan(db_session, tmp_path)
    link.verification_state = IdentityVerificationState.VERIFIED
    if blocked == "unverified":
        link.verification_state = IdentityVerificationState.CONFLICTED
    elif blocked == "busy":
        job.status = ImportJobStatus.IMPORTING
    elif blocked == "skipped":
        group.status = ImportSeriesStatus.SKIPPED
    elif blocked == "override":
        group.user_selected_cv_id = 500
    elif blocked == "mixed":
        group.diagnostics = {
            **group.diagnostics,
            "metron_import": {
                "identities": [
                    {"namespace": "metron", "id": "21"},
                    {"namespace": "metron", "id": "22"},
                ]
            },
        }
    await db_session.commit()
    before = (group.status, group.series_id, deepcopy(group.diagnostics), file.status)
    with pytest.raises((ValidationError, NotFoundError)):
        await prepare_metron_series_recheck(
            db_session, job.id + 1 if blocked == "other_job" else job.id, group.id
        )
    assert (group.status, group.series_id, group.diagnostics, file.status) == before


@pytest.mark.parametrize("link_changed_after_queue", [False, True])
async def test_recheck_issue_identity_uses_current_proof(
    db_session: AsyncSession, tmp_path: Path, link_changed_after_queue: bool
) -> None:
    root, _, issue = await _setup(db_session, tmp_path)
    link = (await db_session.scalars(select(IssueExternalIdentity))).one()
    link.verification_state = IdentityVerificationState.STALE
    await db_session.commit()
    source = tmp_path / "source"
    comic = source / "Unsorted" / "unidentified.cbz"
    original = _archive(comic)
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source), source_type="filesystem", target_library_root_id=root.id
        ),
    )
    await service.start_scan(db_session, job.id)
    file = (await db_session.scalars(select(ImportedFile))).one()
    group = await db_session.get(ImportedSeries, file.import_series_id)
    assert file.status == ImportedFileStatus.NO_MATCH
    assert file.diagnostics["conflict_type"] == "metron_identity_needs_review"
    link.verification_state = IdentityVerificationState.VERIFIED
    await db_session.commit()
    await prepare_metron_series_recheck(db_session, job.id, group.id)
    await db_session.commit()
    if link_changed_after_queue:
        link.verification_state = IdentityVerificationState.CONFLICTED
        await db_session.commit()
    await service.rematch_imported_series_files(db_session, job.id, group.id)
    await db_session.refresh(file)
    assert file.status == (
        ImportedFileStatus.NO_MATCH if link_changed_after_queue else ImportedFileStatus.MATCHED
    )
    assert file.matched_issue_id == (None if link_changed_after_queue else issue.id)
    assert file.include_in_import is False
    assert comic.read_bytes() == original

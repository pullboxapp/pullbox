"""GCD-primary archives share exact local identity and import safety rules."""

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.issue import IssueStatus
from pullbox.models.library import LibraryFile
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
from tests.integration.test_metron_folder_import import _archive, _service, _setup


@pytest.mark.parametrize("number", ["3", "13a", None])
async def test_gcd_folder_import_and_rollback_use_verified_local_identity(
    db_session: AsyncSession, tmp_path: Path, number: str | None
) -> None:
    root, series, issue = await _setup(
        db_session, tmp_path, number=number or "3", namespace=IdentityNamespace.GCD
    )
    issue.status = IssueStatus.SKIPPED
    await db_session.commit()
    source = tmp_path / "source"
    comic = source / "Unsorted" / "unidentified.cbz"
    original = _archive(comic, primary="Grand Comics Database", number=number)
    service = _service()
    request = ImportJobCreate(
        source_path=str(source), source_type="filesystem", target_library_root_id=root.id
    )
    job = await service.create_job(db_session, request)
    await service.start_scan(db_session, job.id)
    file = (await db_session.scalars(select(ImportedFile))).one()
    group = await db_session.get(ImportedSeries, file.import_series_id)
    assert group.series_id == series.id
    assert group.cv_match_method == "gcd_id"
    assert file.status == ImportedFileStatus.MATCHED
    assert file.matched_issue_id == issue.id and file.matched_issue_cv_id is None
    assert file.match_method == "gcd_id" and not file.include_in_import
    await service.update_file_selection(db_session, job.id, file.id, include_in_import=True)
    await service.confirm_import(db_session, job.id, ConfirmImportRequest(series_ids=[group.id]))
    await service.run_import(db_session, job.id)
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.IMPORTED
    placed = (await db_session.scalars(select(LibraryFile))).one()
    destination = Path(placed.file_path)
    assert destination.is_file()
    assert comic.read_bytes() == original
    repeat = await service.create_job(db_session, request)
    await service.start_scan(db_session, repeat.id)
    repeated = (
        await db_session.scalars(
            select(ImportedFile).where(ImportedFile.import_job_id == repeat.id)
        )
    ).one()
    assert repeated.status == ImportedFileStatus.ALREADY_OWNED
    assert not repeated.include_in_import
    assert await service.rollback_import(db_session, job.id)
    await db_session.refresh(issue)
    assert issue.status == IssueStatus.SKIPPED
    assert not destination.exists() and comic.read_bytes() == original
    assert await db_session.scalar(select(LibraryFile.id)) is None
    for method in ("search_series", "get_series", "get_issues_for_series"):
        getattr(service._metadata_service._provider, method).assert_not_awaited()


@pytest.mark.parametrize("invalid", ["series", "issue", "number", "comicvine", "stale"])
async def test_gcd_identity_conflict_does_not_fall_back_to_filename(
    db_session: AsyncSession, tmp_path: Path, invalid: str
) -> None:
    root, _, _ = await _setup(db_session, tmp_path, namespace=IdentityNamespace.GCD)
    if invalid == "stale":
        link = (await db_session.scalars(select(IssueExternalIdentity))).one()
        link.verification_state = IdentityVerificationState.STALE
        await db_session.commit()
    source = tmp_path / "source"
    comic = source / "Harbor Lights (2024)" / "Harbor Lights 003.cbz"
    original = _archive(
        comic,
        primary="GCD",
        series_id="999" if invalid == "series" else "21",
        issue_id="999" if invalid == "issue" else "101",
        number="4" if invalid == "number" else "3",
        extra=(
            "<ComicInfo><Web>https://comicvine.gamespot.com/issue/4000-999/</Web></ComicInfo>"
            if invalid == "comicvine"
            else ""
        ),
    )
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source), source_type="filesystem", target_library_root_id=root.id
        ),
    )
    await service.start_scan(db_session, job.id)
    file = (await db_session.scalars(select(ImportedFile))).one()
    assert file.status in {ImportedFileStatus.NO_MATCH, ImportedFileStatus.CONFLICT}
    assert not file.include_in_import and file.matched_issue_id is None
    assert comic.read_bytes() == original
    service._metadata_service._provider.search_series.assert_not_awaited()


@pytest.mark.parametrize("changed", ["series", "issue"])
async def test_gcd_import_rechecks_link_before_placement(
    db_session: AsyncSession, tmp_path: Path, changed: str
) -> None:
    root, _, _ = await _setup(db_session, tmp_path, namespace=IdentityNamespace.GCD)
    source = tmp_path / "source"
    comic = source / "Unsorted" / "test.cbz"
    original = _archive(comic, primary="GCD")
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source), source_type="filesystem", target_library_root_id=root.id
        ),
    )
    await service.start_scan(db_session, job.id)
    file = (await db_session.scalars(select(ImportedFile))).one()
    assert file.status == ImportedFileStatus.MATCHED
    await service.update_file_selection(db_session, job.id, file.id, include_in_import=True)
    await service.confirm_import(
        db_session, job.id, ConfirmImportRequest(series_ids=[file.import_series_id])
    )
    model = SeriesExternalIdentity if changed == "series" else IssueExternalIdentity
    link = (await db_session.scalars(select(model))).one()
    link.verification_state = IdentityVerificationState.STALE
    await db_session.commit()
    await service.run_import(db_session, job.id)
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.FAILED
    assert comic.read_bytes() == original
    assert await db_session.scalar(select(LibraryFile.id)) is None

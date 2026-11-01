"""Folder imports use verified local Metron identities without a CV dependency."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from pullbox.core.events import EventBus
from pullbox.core.metadata_identity import IdentityEvidenceKind, IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.series import Series
from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
from pullbox.services.import_service import ImportService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _setup(
    session: AsyncSession, root: Path, *, number: str = "3"
) -> tuple[LibraryRoot, Series, Issue]:
    library = root / "library"
    library.mkdir()
    destination = LibraryRoot(
        name="Comics",
        path=str(library),
        enabled=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )
    series = Series(title="Harbor Lights", sort_title="Harbor Lights", year_start=2024)
    session.add_all([destination, series])
    await session.flush()
    issue = Issue(
        series_id=series.id, issue_number=float(number.rstrip("a")), issue_number_text=number
    )
    session.add(issue)
    await session.flush()
    session.add_all(
        [
            SeriesExternalIdentity(
                series_id=series.id,
                identity_namespace=IdentityNamespace.METRON,
                external_id="21",
                verification_state=IdentityVerificationState.VERIFIED,
                evidence_kind=IdentityEvidenceKind.PROVIDER_RESULT,
            ),
            IssueExternalIdentity(
                issue_id=issue.id,
                identity_namespace=IdentityNamespace.METRON,
                external_id="101",
                verification_state=IdentityVerificationState.VERIFIED,
                evidence_kind=IdentityEvidenceKind.PROVIDER_RESULT,
            ),
        ]
    )
    await session.commit()
    return destination, series, issue


def _archive(
    path: Path,
    *,
    number: str | None = "3",
    issue_id: str = "101",
    series_id: str = "21",
    extra: str = "",
    primary: str = "Metron",
) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("page001.jpg", b"page one")
        archive.writestr("page002.jpg", b"page two")
        archive.writestr(
            "MetronInfo.xml",
            (
                f'<MetronInfo><IDS><ID source="{primary}" primary="true">{issue_id}</ID></IDS>'
                f'<Series id="{series_id}"><Name>Harbor Lights</Name>'
                "<StartYear>2024</StartYear></Series>"
                + (f"<Number>{number}</Number>" if number is not None else "")
                + "</MetronInfo>"
            ),
        )
        if extra:
            archive.writestr("ComicInfo.xml", extra)
    return path.read_bytes()


def _service() -> ImportService:
    provider = AsyncMock()
    provider.search_series.return_value = []
    metadata = AsyncMock()
    metadata._provider = provider
    return ImportService(
        series_service=AsyncMock(), metadata_service=metadata, event_bus=EventBus()
    )


@pytest.mark.parametrize("number", ["3", "13a"])
async def test_metron_only_folder_imports_to_verified_local_issue(
    db_session: AsyncSession,
    tmp_path: Path,
    number: str,
) -> None:
    root, series, issue = await _setup(db_session, tmp_path, number=number)
    source = tmp_path / "source"
    comic = source / "Unsorted" / "unidentified.cbz"
    original = _archive(comic, number=number)
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source),
            source_type="filesystem",
            target_library_root_id=root.id,
        ),
    )
    await service.start_scan(db_session, job.id)
    row = (
        await db_session.scalars(select(ImportedFile).where(ImportedFile.import_job_id == job.id))
    ).one()
    group = await db_session.get(ImportedSeries, row.import_series_id)
    assert group is not None
    assert group.series_id == series.id
    assert group.cv_id is None
    assert row.status == ImportedFileStatus.MATCHED
    assert row.matched_issue_id == issue.id
    assert row.matched_issue_cv_id is None
    assert row.match_method == "metron_id"
    await service.update_file_selection(db_session, job.id, row.id, include_in_import=True)
    await service.confirm_import(db_session, job.id, ConfirmImportRequest(series_ids=[group.id]))
    await service.run_import(db_session, job.id)
    await db_session.refresh(row)
    assert row.status == ImportedFileStatus.IMPORTED
    placed = (
        await db_session.scalars(select(LibraryFile).where(LibraryFile.issue_id == issue.id))
    ).one()
    assert Path(placed.file_path).is_file()
    assert comic.read_bytes() == original
    assert series.comicvine_id is None and issue.comicvine_id is None
    service._metadata_service._provider.search_series.assert_not_awaited()
    service._metadata_service._provider.get_series.assert_not_awaited()
    service._metadata_service._provider.get_issues_for_series.assert_not_awaited()
    retry = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source),
            source_type="filesystem",
            target_library_root_id=root.id,
        ),
    )
    await service.start_scan(db_session, retry.id)
    repeated = (
        await db_session.scalars(select(ImportedFile).where(ImportedFile.import_job_id == retry.id))
    ).one()
    assert repeated.status == ImportedFileStatus.ALREADY_OWNED
    assert repeated.include_in_import is False
    assert len((await db_session.scalars(select(LibraryFile))).all()) == 1


async def test_comicvine_primary_paired_archive_keeps_existing_import_path(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    root, series, issue = await _setup(db_session, tmp_path)
    series.comicvine_id = 500
    issue.comicvine_id = 600
    await db_session.commit()
    source = tmp_path / "source"
    _archive(
        source / "Harbor Lights (2024)" / "Harbor Lights 003.cbz",
        primary="Comic Vine",
        issue_id="600",
        series_id="500",
        extra=(
            "<ComicInfo><Series>Harbor Lights</Series><Number>3</Number>"
            "<Notes>[cv_vol_id:500]</Notes>"
            "<Web>https://comicvine.gamespot.com/issue/4000-600/</Web></ComicInfo>"
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
    row = (await db_session.scalars(select(ImportedFile))).one()
    group = await db_session.get(ImportedSeries, row.import_series_id)
    assert row.status == ImportedFileStatus.MATCHED
    assert row.matched_issue_id == issue.id
    assert row.match_method != "metron_id"
    assert "metron_import" not in group.diagnostics


@pytest.mark.parametrize(
    "issue_id,extra",
    [
        ("999", ""),
        ("101", "<ComicInfo><Series>Harbor Lights</Series><Number>4</Number></ComicInfo>"),
        ("101", "<ComicInfo><Web>https://comicvine.gamespot.com/issue/4000-999/</Web></ComicInfo>"),
    ],
)
async def test_exact_metron_evidence_cannot_fall_back_to_title_or_number(
    db_session: AsyncSession,
    tmp_path: Path,
    issue_id: str,
    extra: str,
) -> None:
    root, _, _ = await _setup(db_session, tmp_path)
    source = tmp_path / "source"
    comic = source / "Harbor Lights (2024)" / "Harbor Lights 003 (2024).cbz"
    original = _archive(comic, issue_id=issue_id, extra=extra)
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source),
            source_type="filesystem",
            target_library_root_id=root.id,
        ),
    )
    await service.start_scan(db_session, job.id)
    row = (
        await db_session.scalars(select(ImportedFile).where(ImportedFile.import_job_id == job.id))
    ).one()
    assert row.status in {ImportedFileStatus.NO_MATCH, ImportedFileStatus.CONFLICT}
    assert row.include_in_import is False
    assert comic.read_bytes() == original
    assert await db_session.scalar(select(LibraryFile.id)) is None


@pytest.mark.parametrize("state", ["unknown_series", "stale_series", "stale_issue", "no_number"])
async def test_local_metron_identity_boundaries(
    db_session: AsyncSession, tmp_path: Path, state: str
) -> None:
    root, _, issue = await _setup(db_session, tmp_path)
    if state in {"stale_series", "stale_issue"}:
        model = SeriesExternalIdentity if state == "stale_series" else IssueExternalIdentity
        link = (await db_session.scalars(select(model))).one()
        link.verification_state = IdentityVerificationState.STALE
        await db_session.commit()
    source = tmp_path / "source"
    _archive(
        source / "Harbor Lights (2024)" / "unidentified.cbz",
        series_id="999" if state == "unknown_series" else "21",
        number=None if state == "no_number" else "3",
    )
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source),
            source_type="filesystem",
            target_library_root_id=root.id,
        ),
    )
    await service.start_scan(db_session, job.id)
    row = (
        await db_session.scalars(select(ImportedFile).where(ImportedFile.import_job_id == job.id))
    ).one()
    if state == "no_number":
        assert row.status == ImportedFileStatus.MATCHED
        assert row.matched_issue_id == issue.id
    else:
        assert row.status in {ImportedFileStatus.NO_MATCH, ImportedFileStatus.CONFLICT}
        assert row.include_in_import is False


@pytest.mark.parametrize("changed", ["source", "series_link", "issue_link"])
async def test_metron_import_rechecks_scan_time_proof_before_placement(
    db_session: AsyncSession,
    tmp_path: Path,
    changed: str,
) -> None:
    root, _, _ = await _setup(db_session, tmp_path)
    source = tmp_path / "source"
    comic = source / "Unsorted" / "unidentified.cbz"
    original = _archive(comic)
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source),
            source_type="filesystem",
            target_library_root_id=root.id,
        ),
    )
    await service.start_scan(db_session, job.id)
    row = (
        await db_session.scalars(select(ImportedFile).where(ImportedFile.import_job_id == job.id))
    ).one()
    await service.update_file_selection(db_session, job.id, row.id, include_in_import=True)
    await service.confirm_import(db_session, job.id, ConfirmImportRequest(series_ids=[]))
    if changed == "source":
        original = _archive(comic, issue_id="999")
    else:
        model = SeriesExternalIdentity if changed == "series_link" else IssueExternalIdentity
        link = (await db_session.scalars(select(model))).one()
        link.verification_state = IdentityVerificationState.STALE
        await db_session.commit()
    await service.run_import(db_session, job.id)
    await db_session.refresh(row)
    assert row.status == ImportedFileStatus.FAILED
    assert comic.read_bytes() == original
    assert await db_session.scalar(select(LibraryFile.id)) is None


async def test_metron_folder_identity_does_not_claim_untagged_neighbor(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    root, series, _ = await _setup(db_session, tmp_path)
    other_issue = Issue(series_id=series.id, issue_number=4, issue_number_text="4")
    db_session.add(other_issue)
    await db_session.commit()
    source = tmp_path / "source"
    _archive(source / "Harbor Lights (2024)" / "Harbor Lights 003.cbz")
    neighbor = source / "Harbor Lights (2024)" / "Harbor Lights 004.cbz"
    with zipfile.ZipFile(neighbor, "w") as archive:
        archive.writestr("page001.jpg", b"one")
        archive.writestr("page002.jpg", b"two")
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source), source_type="filesystem", target_library_root_id=root.id
        ),
    )
    await service.start_scan(db_session, job.id)
    files = (await db_session.scalars(select(ImportedFile))).all()
    tagged = next(file for file in files if file.file_path != str(neighbor))
    untagged = next(file for file in files if file.file_path == str(neighbor))
    assert tagged.status == ImportedFileStatus.MATCHED
    assert untagged.status in {ImportedFileStatus.NO_MATCH, ImportedFileStatus.CONFLICT}
    assert untagged.matched_issue_id is None
    assert untagged.include_in_import is False


async def test_distinct_metron_suffix_identities_do_not_collapse_to_one_issue(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    root, series, first = await _setup(db_session, tmp_path, number="13a")
    second = Issue(series_id=series.id, issue_number=13, issue_number_text="13b")
    db_session.add(second)
    await db_session.flush()
    db_session.add(
        IssueExternalIdentity(
            issue_id=second.id,
            identity_namespace=IdentityNamespace.METRON,
            external_id="102",
            verification_state=IdentityVerificationState.VERIFIED,
            evidence_kind=IdentityEvidenceKind.PROVIDER_RESULT,
        )
    )
    await db_session.commit()
    source = tmp_path / "source"
    _archive(source / "Unsorted" / "first.cbz", number="13a")
    _archive(source / "Unsorted" / "second.cbz", number="13b", issue_id="102")
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source), source_type="filesystem", target_library_root_id=root.id
        ),
    )
    await service.start_scan(db_session, job.id)
    files = (await db_session.scalars(select(ImportedFile).order_by(ImportedFile.file_name))).all()
    assert [file.matched_issue_id for file in files] == [first.id, second.id]
    assert all(file.status == ImportedFileStatus.MATCHED for file in files)


@pytest.mark.parametrize(
    "comicinfo,matched",
    [
        ("<ComicInfo><Number>3</Number><UnknownTag>kept</UnknownTag></ComicInfo>", True),
        ("<ComicInfo><Number>3</Number><PageCount>invalid</PageCount></ComicInfo>", True),
        ("<ComicInfo><Number>bad number</Number></ComicInfo>", False),
        ("<ComicInfo><Number>3</Number><Number>4</Number></ComicInfo>", False),
        ("<ComicInfo><Number>3a</Number></ComicInfo>", False),
        ("<ComicInfo><Number>3</Number>", False),
    ],
)
async def test_metron_import_distinguishes_identity_errors_from_optional_metadata(
    db_session: AsyncSession, tmp_path: Path, comicinfo: str, matched: bool
) -> None:
    root, _, issue = await _setup(db_session, tmp_path)
    source = tmp_path / "source"
    comic = source / "Unsorted" / "unidentified.cbz"
    original = _archive(comic, extra=comicinfo)
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source), source_type="filesystem", target_library_root_id=root.id
        ),
    )
    await service.start_scan(db_session, job.id)
    row = (await db_session.scalars(select(ImportedFile))).one()
    if matched:
        assert row.status == ImportedFileStatus.MATCHED
        assert row.matched_issue_id == issue.id
        assert row.diagnostics["source_metadata"]["metron_import"]["diagnostics"]
    else:
        assert row.status in {ImportedFileStatus.NO_MATCH, ImportedFileStatus.CONFLICT}
        assert row.include_in_import is False
    assert comic.read_bytes() == original

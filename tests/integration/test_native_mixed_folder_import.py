"""Mixed folders keep each exact native identity and unresolved neighbor separate."""

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.metadata_identity import IdentityEvidenceKind, IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.series import Series
from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
from pullbox.services.import_metron_matching import native_series_preview_identity
from tests.integration.test_metron_folder_import import _archive, _service, _setup


@pytest.mark.parametrize(
    "first_namespace,second_namespace",
    [
        (IdentityNamespace.METRON, IdentityNamespace.METRON),
        (IdentityNamespace.GCD, IdentityNamespace.GCD),
        (IdentityNamespace.METRON, IdentityNamespace.GCD),
    ],
)
@pytest.mark.parametrize("selected_files", [False, True])
async def test_mixed_native_folder_groups_imports_and_rolls_back_without_claiming_neighbors(
    db_session: AsyncSession,
    tmp_path: Path,
    first_namespace: IdentityNamespace,
    second_namespace: IdentityNamespace,
    selected_files: bool,
) -> None:
    root, _, first_issue = await _setup(db_session, tmp_path, namespace=first_namespace)
    second_series = Series(title="Other Harbor", sort_title="Other Harbor", year_start=2024)
    db_session.add(second_series)
    await db_session.flush()
    second_issue = Issue(
        series_id=second_series.id,
        issue_number=13,
        issue_number_text="13a",
        status=IssueStatus.SKIPPED,
    )
    db_session.add(second_issue)
    await db_session.flush()
    db_session.add_all(
        [
            SeriesExternalIdentity(
                series_id=second_series.id,
                identity_namespace=second_namespace,
                external_id="22",
                verification_state=IdentityVerificationState.VERIFIED,
                evidence_kind=IdentityEvidenceKind.PROVIDER_RESULT,
            ),
            IssueExternalIdentity(
                issue_id=second_issue.id,
                identity_namespace=second_namespace,
                external_id="102",
                verification_state=IdentityVerificationState.VERIFIED,
                evidence_kind=IdentityEvidenceKind.PROVIDER_RESULT,
            ),
        ]
    )
    first_issue.status = IssueStatus.WANTED
    await db_session.commit()
    source = tmp_path / "source"
    folder = source / "Harbor Lights (2024)"
    first = folder / "Harbor Lights 003.cbz"
    second = folder / "Harbor Lights 013a.cbz"
    unknown = folder / "Harbor Lights 099.cbz"
    originals = {
        first: _archive(first, primary=first_namespace.value),
        second: _archive(
            second, primary=second_namespace.value, series_id="22", issue_id="102", number="13a"
        ),
        unknown: _archive(
            unknown, primary=first_namespace.value, series_id="99", issue_id="999", number="99"
        ),
    }
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_path=str(source),
            source_type="filesystem",
            target_library_root_id=root.id,
            file_paths=[str(path) for path in originals] if selected_files else None,
        ),
    )
    await service.start_scan(db_session, job.id)
    groups = (
        await db_session.scalars(
            select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
    ).all()
    assert len(groups) == 3
    assert job.series_found == 3 and job.scan_total_files == 3
    assert all(group.file_count == group.files_total == 1 for group in groups)
    assert all(group.source_folder == str(folder) for group in groups)
    files = {
        Path(file.file_path): file
        for file in (
            await db_session.scalars(
                select(ImportedFile).where(ImportedFile.import_job_id == job.id)
            )
        ).all()
    }
    assert files[first].matched_issue_id == first_issue.id
    assert files[second].matched_issue_id == second_issue.id
    assert all(files[path].status == ImportedFileStatus.MATCHED for path in (first, second))
    assert all(not file.include_in_import for file in files.values())
    assert files[unknown].status == ImportedFileStatus.NO_MATCH
    assert files[unknown].matched_issue_id is None
    _, _, namespace, external_id = await native_series_preview_identity(
        db_session, job.id, files[unknown].import_series_id
    )
    assert (namespace, external_id) == (first_namespace, "99")

    selected_groups = []
    for path in (first, second):
        await service.update_file_selection(
            db_session, job.id, files[path].id, include_in_import=True
        )
        selected_groups.append(files[path].import_series_id)
    await service.confirm_import(
        db_session, job.id, ConfirmImportRequest(series_ids=selected_groups)
    )
    await service.run_import(db_session, job.id)
    placed = (await db_session.scalars(select(LibraryFile))).all()
    assert {file.issue_id for file in placed} == {first_issue.id, second_issue.id}
    destinations = [Path(file.file_path) for file in placed]
    assert all(path.is_file() for path in destinations)
    assert files[unknown].matched_issue_id is None
    assert await service.rollback_import(db_session, job.id)
    await db_session.refresh(first_issue)
    await db_session.refresh(second_issue)
    assert first_issue.status == IssueStatus.WANTED
    assert second_issue.status == IssueStatus.SKIPPED
    assert not any(path.exists() for path in destinations)
    assert await db_session.scalar(select(LibraryFile.id)) is None
    assert all(path.read_bytes() == original for path, original in originals.items())
    for method in ("search_series", "get_series", "get_issues_for_series"):
        getattr(service._metadata_service._provider, method).assert_not_awaited()


@pytest.mark.parametrize("namespace", [IdentityNamespace.METRON, IdentityNamespace.GCD])
async def test_conflicting_cross_identity_cannot_poison_clean_neighbor(
    db_session: AsyncSession, tmp_path: Path, namespace: IdentityNamespace
) -> None:
    root, _, issue = await _setup(db_session, tmp_path, namespace=namespace)
    source = tmp_path / "source"
    folder = source / "Unsorted [cv-500]"
    clean = folder / "clean.cbz"
    conflict = folder / "conflict.cbz"
    originals = {
        clean: _archive(clean, primary=namespace.value),
        conflict: _archive(
            conflict,
            primary=namespace.value,
            extra=("<ComicInfo><Notes>[cv_vol_id:999]</Notes><Number>3</Number></ComicInfo>"),
        ),
    }
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
    files = {
        file.file_name: file for file in (await db_session.scalars(select(ImportedFile))).all()
    }
    assert files[clean.name].status == ImportedFileStatus.MATCHED
    assert files[clean.name].matched_issue_id == issue.id
    assert files[conflict.name].status in {ImportedFileStatus.NO_MATCH, ImportedFileStatus.CONFLICT}
    assert files[conflict.name].matched_issue_id is None
    assert files[clean.name].import_series_id != files[conflict.name].import_series_id
    assert not any(file.include_in_import for file in files.values())
    assert all(path.read_bytes() == original for path, original in originals.items())
    service._metadata_service._provider.search_series.assert_not_awaited()


async def test_native_group_counts_survive_incremental_scan_batches(db_session, tmp_path):
    root, _, _ = await _setup(db_session, tmp_path)
    source = tmp_path / "source"
    for index in range(26):
        folder = source / f"Unsorted {index:02d}"
        _archive(folder / "first.cbz", series_id=str(1000 + index), issue_id=str(2000 + index))
        _archive(
            folder / "second.cbz",
            primary="GCD",
            series_id=str(3000 + index),
            issue_id=str(4000 + index),
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
    groups = (await db_session.scalars(select(ImportedSeries))).all()
    files = (await db_session.scalars(select(ImportedFile))).all()
    assert job.series_found == len(groups) == 52
    assert job.scan_total_files == len(files) == 52
    assert all(group.file_count == group.files_total == 1 for group in groups)
    assert len({file.file_path for file in files}) == 52
    assert all(
        file.status == ImportedFileStatus.NO_MATCH and not file.include_in_import for file in files
    )
    service._metadata_service._provider.search_series.assert_not_awaited()

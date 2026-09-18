"""Catalog recovery repairs assignments, never the user's source files."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.models.import_job import (
    ImportedFileStatus,
    ImportedSeries,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, LibraryFileStorageMode
from pullbox.models.series import Series
from pullbox.providers.base import IssueSummary, SeriesSearchResult
from pullbox.services.import_completed_cleanup import (
    CompletedImportCleanupAction,
    apply_completed_import_cleanup,
    count_completed_import_cleanup_scope,
    preview_completed_import_cleanup,
)
from pullbox.services.import_deferred_recovery_execution import prepare_deferred_recovery
from pullbox.services.metadata_service import MetadataService
from tests.unit.test_import_deferred_recovery import add_file, register, seed


async def reference_case(session, tmp_path, source_type=ImportSourceType.MYLAR3):
    job, item, series, issue, root = await seed(session, source_type=source_type)
    series.title = item.raw_series_name = "Fritzi Ritz"
    issue.status = IssueStatus.OWNED
    root.path = str(tmp_path)
    path = tmp_path / "Thunderbolts 104 (2021).cbz"
    path.write_bytes(b"unchanged comic content")
    file = await add_file(
        session,
        job,
        item,
        file_path=str(path),
        file_name=path.name,
        file_size=path.stat().st_size,
        status=ImportedFileStatus.IMPORTED,
        parsed_series="Thunderbolts",
        comicvine_issue_id=None,
        matched_issue_id=issue.id,
        matched_issue_cv_id=issue.comicvine_id,
        source_signature=build_file_identity_signature(path),
        diagnostics={"metadata_signals": {"issue_number": "release_title"}},
    )
    library = await register(session, file, issue, root)
    library.storage_mode = LibraryFileStorageMode.REFERENCED
    file.library_file_id = library.id
    item.files_no_match = 1
    metadata = MetadataService(AsyncMock(), tmp_path / "covers")
    metadata.search_catalog_series = AsyncMock(
        return_value=[
            SeriesSearchResult(
                provider_id="700",
                title="Thunderbolts",
                year_start=2016,
                publisher=None,
                issue_count=130,
                status=None,
                cover_url=None,
                description=None,
            )
        ]
    )
    metadata.get_catalog_issue_summaries_for_series = AsyncMock(
        return_value=[
            IssueSummary(
                provider_id="7001",
                issue_number=104,
                issue_number_text="104",
                title=None,
                release_date="2021-01-01",
                cover_url=None,
                issue_type="issue",
            )
        ]
    )
    job.status = ImportJobStatus.IMPORTING
    job.progress_snapshot = {"deferred_recovery": {"state": "queued"}}
    await session.commit()
    return job, file, library, issue, metadata, path


@pytest.mark.parametrize("source_type", list(ImportSourceType))
async def test_recovery_creates_missing_target_and_reassigns_reference_in_place(
    db_session,
    tmp_path,
    source_type,
):
    job, file, library, wrong_issue, metadata, path = await reference_case(
        db_session,
        tmp_path,
        source_type,
    )
    before = path.read_bytes(), path.stat().st_mtime_ns
    original_library_id = library.id

    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)

    target = await db_session.scalar(select(Issue).where(Issue.comicvine_id == 7001))
    assert target is not None, "Missing catalog targets must not strand already-imported comics"
    assert library.issue_id == target.id == file.matched_issue_id
    assert library.id == original_library_id == file.library_file_id
    assert file.status is ImportedFileStatus.IMPORTED
    assert target.status is IssueStatus.OWNED
    assert wrong_issue.status is not IssueStatus.OWNED
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert library.file_path == file.file_path == str(path)
    assert (await db_session.get(Series, target.series_id)).path is None
    assert await db_session.scalar(select(func.count(LibraryFile.id))) == 1
    assert file.diagnostics["completed_import_cleanup"]["source_preserved"]
    assert file.diagnostics["completed_import_cleanup"]["source_issue_id"] == wrong_issue.id
    group = await db_session.get(ImportedSeries, file.import_series_id)
    assert group.status.value == "imported"

    job.status = ImportJobStatus.IMPORTING
    job.progress_snapshot = {"deferred_recovery": {"state": "queued"}}
    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)
    assert await db_session.scalar(select(func.count(LibraryFile.id))) == 1
    assert await db_session.scalar(select(func.count(Issue.id))) == 2


async def test_reference_candidates_make_existing_recheck_action_available(db_session, tmp_path):
    job, *_ = await reference_case(db_session, tmp_path)
    counts = await count_completed_import_cleanup_scope(
        db_session,
        job.id,
        CompletedImportCleanupAction.RECHECK_DEFERRED_FILES,
    )
    assert counts == (1, 1), "Recovery must remain available even without NO_MATCH rows"


@pytest.mark.parametrize(
    "protection",
    [
        "manual",
        "parent_manual",
        "managed",
        "changed",
        "missing",
        "safety",
        "ambiguous",
        "wrong_year",
        "wrong_type",
        "conflicting_id",
        "changed_registration",
    ],
)
async def test_reference_recovery_preserves_unsafe_or_ambiguous_assignments(
    db_session,
    tmp_path,
    protection,
):
    job, file, library, issue, metadata, path = await reference_case(db_session, tmp_path)
    if protection == "manual":
        file.match_method = "manual_issue"
    elif protection == "parent_manual":
        from pullbox.models.import_job import ImportedSeries

        (await db_session.get(ImportedSeries, file.import_series_id)).user_selected_cv_id = 100
    elif protection == "managed":
        library.storage_mode = LibraryFileStorageMode.MANAGED
    elif protection == "changed":
        path.write_bytes(b"a different comic")
    elif protection == "missing":
        path.unlink()
    elif protection == "safety":
        file.diagnostics = {**file.diagnostics, "safety_exception": {"approved": True}}
    elif protection == "ambiguous":
        first = metadata.search_catalog_series.return_value[0]
        from dataclasses import replace

        metadata.search_catalog_series.return_value = [first, replace(first, provider_id="701")]
    elif protection in {"wrong_year", "wrong_type"}:
        from dataclasses import replace

        first = metadata.get_catalog_issue_summaries_for_series.return_value[0]
        updates = (
            {"release_date": "1990-01-01"} if protection == "wrong_year" else {"issue_type": "tpb"}
        )
        metadata.get_catalog_issue_summaries_for_series.return_value = [replace(first, **updates)]
    elif protection == "conflicting_id":
        file.comicvine_issue_id = 9001
    else:
        library.source_signature = {**library.source_signature, "mtime_ns": 1}
    await db_session.commit()

    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)

    assert library.issue_id == issue.id == file.matched_issue_id
    assert await db_session.scalar(select(Issue).where(Issue.comicvine_id == 7001)) is None


async def test_recheck_preview_queues_reference_repair_without_changing_files(db_session, tmp_path):
    from pullbox.models.import_job import ImportFileHandlingMode
    from pullbox.models.user import User

    job, file, library, issue, metadata, path = await reference_case(db_session, tmp_path)
    db_session.add(User(id=42, username="recovery-test", password_hash="unused"))
    job.status = ImportJobStatus.COMPLETED
    job.progress_snapshot = {}
    job.file_handling_mode = ImportFileHandlingMode.IN_PLACE
    job.move_to_library = False
    await db_session.commit()
    action = CompletedImportCleanupAction.RECHECK_DEFERRED_FILES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    assert preview.affected_file_count == 1
    result = await apply_completed_import_cleanup(
        db_session,
        job.id,
        action,
        actor_id=42,
        preview_token=preview.preview_token,
    )
    assert result.requires_import_retry
    assert file.matched_issue_id == library.issue_id == issue.id
    metadata.search_catalog_series.assert_not_awaited()
    assert path.exists()
    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)
    assert file.matched_issue_id != issue.id


@pytest.mark.parametrize(
    "existing", ["series_only", "unowned_issue", "owned_issue", "wrong_number"]
)
async def test_recovery_respects_existing_catalog_and_ownership(db_session, tmp_path, existing):
    job, file, library, wrong_issue, metadata, _ = await reference_case(db_session, tmp_path)
    target_series = Series(
        title="Thunderbolts",
        sort_title="thunderbolts",
        comicvine_id=700,
        description="Keep full metadata",
    )
    db_session.add(target_series)
    await db_session.flush()
    target = None
    if existing != "series_only":
        target = Issue(
            series_id=target_series.id,
            comicvine_id=7001,
            issue_number=105 if existing == "wrong_number" else 104,
            issue_number_text="105" if existing == "wrong_number" else "104",
        )
        db_session.add(target)
        await db_session.flush()
    if existing == "owned_issue":
        from pullbox.models.library import LibraryRoot

        item = await db_session.get(ImportedSeries, file.import_series_id)
        other = await add_file(db_session, job, item, file_path="/comics/owned.cbz")
        other.status = ImportedFileStatus.IMPORTED
        root = await db_session.get(LibraryRoot, library.library_root_id)
        await register(db_session, other, target, root)
    await db_session.commit()
    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)
    assert target_series.description == "Keep full metadata"
    if existing in {"owned_issue", "wrong_number"}:
        assert library.issue_id == wrong_issue.id
    else:
        target = await db_session.scalar(select(Issue).where(Issue.comicvine_id == 7001))
        assert library.issue_id == target.id


async def add_second_reference(session, job, file, library, tmp_path, *, number=105):
    from pullbox.models.import_job import ImportedSeries
    from pullbox.models.library import LibraryRoot

    directory = tmp_path / "another source folder"
    directory.mkdir(exist_ok=True)
    path = directory / f"Thunderbolts {number} (2021).cbz"
    path.write_bytes(b"second comic")
    item = await session.get(ImportedSeries, file.import_series_id)
    root = await session.get(LibraryRoot, library.library_root_id)
    issue = await session.get(Issue, library.issue_id)
    other = await add_file(
        session,
        job,
        item,
        file_name=path.name,
        file_path=str(path),
        file_size=path.stat().st_size,
        status=ImportedFileStatus.IMPORTED,
        parsed_series="Thunderbolts",
        parsed_issue_number=number,
        comicvine_issue_id=None,
        matched_issue_id=issue.id,
        source_signature=build_file_identity_signature(path),
        diagnostics=file.diagnostics,
    )
    other_library = await register(session, other, issue, root)
    other_library.storage_mode = LibraryFileStorageMode.REFERENCED
    other.library_file_id = other_library.id
    await session.commit()
    return other, other_library


async def test_two_physical_files_for_one_catalog_target_remain_for_review(db_session, tmp_path):
    job, file, library, wrong, metadata, _ = await reference_case(db_session, tmp_path)
    other, other_library = await add_second_reference(
        db_session,
        job,
        file,
        library,
        tmp_path,
        number=104,
    )
    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)
    assert file.matched_issue_id == other.matched_issue_id == wrong.id
    assert library.issue_id == other_library.issue_id == wrong.id
    assert await db_session.scalar(select(func.count(LibraryFile.id))) == 2


async def test_interrupted_repairs_resume_without_repeating_or_losing_completed_work(
    db_session,
    tmp_path,
    monkeypatch,
):
    from dataclasses import replace

    from pullbox.models.import_job import ImportJob
    from pullbox.services import import_completed_cleanup as cleanup

    job, file, library, wrong, metadata, _ = await reference_case(db_session, tmp_path)
    _other, other_library = await add_second_reference(db_session, job, file, library, tmp_path)
    first = metadata.get_catalog_issue_summaries_for_series.return_value[0]
    metadata.get_catalog_issue_summaries_for_series.return_value.append(
        replace(
            first,
            provider_id="7002",
            issue_number=105,
            issue_number_text="105",
        )
    )
    original = cleanup._apply_mixed_folder_resolutions
    calls = 0

    async def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Simulated worker interruption")
        return await original(*args, **kwargs)

    monkeypatch.setattr(cleanup, "_apply_mixed_folder_resolutions", interrupted)
    job_id = job.id
    with pytest.raises(RuntimeError, match="Simulated"):
        await prepare_deferred_recovery(db_session, job_id, metadata_service=metadata)
    await db_session.rollback()
    job = await db_session.get(ImportJob, job_id)
    assert job.progress_snapshot["deferred_recovery"]["reference_files_repaired"] == 1
    monkeypatch.setattr(cleanup, "_apply_mixed_folder_resolutions", original)
    await prepare_deferred_recovery(db_session, job_id, metadata_service=metadata)
    await db_session.refresh(library)
    await db_session.refresh(other_library)
    await db_session.refresh(wrong)
    assert library.issue_id != wrong.id != other_library.issue_id
    assert library.issue_id != other_library.issue_id
    assert job.progress_snapshot["deferred_recovery"]["reference_files_repaired"] == 2
    metadata.search_catalog_series.assert_awaited_once()
    assert job.status is ImportJobStatus.COMPLETED


async def test_catalog_pause_does_not_modify_source_and_resume_checks_current_evidence(
    db_session,
    tmp_path,
):
    from pullbox.core.exceptions import JobPausedError, ProviderError

    job, file, library, wrong, metadata, path = await reference_case(db_session, tmp_path)
    metadata.search_catalog_series.side_effect = ProviderError("catalog", "unavailable")
    with pytest.raises(JobPausedError):
        await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)
    assert file.matched_issue_id == library.issue_id == wrong.id
    path.write_bytes(b"replaced while paused")
    metadata.search_catalog_series.side_effect = None
    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)
    assert file.matched_issue_id == library.issue_id == wrong.id


async def test_catalog_lookup_does_not_hold_database_transaction(db_session, tmp_path):
    job, _, _, _, metadata, _ = await reference_case(db_session, tmp_path)
    results = metadata.search_catalog_series.return_value

    async def search(*args, **kwargs):
        assert not db_session.in_transaction()
        return results

    metadata.search_catalog_series.side_effect = search
    events = []

    async def progress(event):
        events.append(event)

    await prepare_deferred_recovery(
        db_session,
        job.id,
        metadata_service=metadata,
        progress_callback=progress,
    )
    assert any(event.current_file_progress_unit == "files" for event in events)
    assert job.progress_snapshot["progress"] == 100


async def test_recovery_accepts_reference_only_root_without_enabling_writes(db_session, tmp_path):
    from pullbox.models.library import LibraryRoot

    job, file, library, wrong, metadata, path = await reference_case(db_session, tmp_path)
    root = await db_session.get(LibraryRoot, library.library_root_id)
    root.allow_managed_writes = False
    path.chmod(0o444)
    await db_session.commit()
    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)
    assert file.matched_issue_id != wrong.id
    assert not root.allow_managed_writes
    assert path.stat().st_mode & 0o777 == 0o444


async def test_manual_decision_during_catalog_lookup_is_not_overridden(db_session, tmp_path):
    job, file, library, wrong, metadata, _ = await reference_case(db_session, tmp_path)
    results = metadata.search_catalog_series.return_value

    async def search(*args, **kwargs):
        file.match_method = "manual_issue"
        await db_session.commit()
        return results

    metadata.search_catalog_series.side_effect = search
    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)
    assert file.matched_issue_id == library.issue_id == wrong.id
    assert file.match_method == "manual_issue"


async def test_cancel_between_repairs_keeps_completed_reference_and_other_files(
    db_session,
    tmp_path,
    monkeypatch,
):
    from dataclasses import replace

    from pullbox.core.exceptions import JobCancelledError
    from pullbox.services import import_completed_cleanup as cleanup
    from pullbox.services.import_deferred_recovery_execution import cancel_deferred_preparation

    job, file, library, wrong, metadata, _ = await reference_case(db_session, tmp_path)
    other, other_library = await add_second_reference(db_session, job, file, library, tmp_path)
    first = metadata.get_catalog_issue_summaries_for_series.return_value[0]
    metadata.get_catalog_issue_summaries_for_series.return_value.append(
        replace(
            first,
            provider_id="7002",
            issue_number=105,
            issue_number_text="105",
        )
    )
    original = cleanup._apply_mixed_folder_resolutions
    calls = 0

    async def cancel_on_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise JobCancelledError("cancelled")
        return await original(*args, **kwargs)

    monkeypatch.setattr(cleanup, "_apply_mixed_folder_resolutions", cancel_on_second)
    with pytest.raises(JobCancelledError):
        await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)
    # The worker rolls back the interrupted unit, not earlier committed repairs.
    await db_session.rollback()
    for row in (job, file, library, wrong, other, other_library):
        await db_session.refresh(row)
    job.status = ImportJobStatus.CANCELLING
    assert await cancel_deferred_preparation(db_session, job)
    assert library.issue_id != wrong.id
    assert other_library.issue_id == wrong.id
    assert file.status is other.status is ImportedFileStatus.IMPORTED
    assert job.status is ImportJobStatus.COMPLETED


async def test_recovery_does_not_turn_wrong_provisional_issue_into_wanted_download(
    db_session,
    tmp_path,
):
    job, file, library, wrong, metadata, _ = await reference_case(db_session, tmp_path)
    wrong.comicvine_id = None
    wrong.metadata_source = "provisional_import"
    file.matched_issue_cv_id = None
    series = await db_session.get(Series, wrong.series_id)
    series.monitored = True
    await db_session.commit()
    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)
    assert library.issue_id != wrong.id
    assert wrong.status is IssueStatus.SKIPPED


async def test_same_timestamp_manual_change_during_file_check_is_preserved(
    db_session,
    tmp_path,
    monkeypatch,
):
    from sqlalchemy import update

    from pullbox.models.import_job import ImportedFile
    from pullbox.services import import_reference_recovery as recovery

    job, file, library, wrong, metadata, _ = await reference_case(db_session, tmp_path)
    original_to_thread = recovery.asyncio.to_thread

    async def checked(func, *args, **kwargs):
        result = await original_to_thread(func, *args, **kwargs)
        if func is recovery._unchanged_source:
            await db_session.execute(
                update(ImportedFile)
                .where(ImportedFile.id == file.id)
                .values(
                    match_method="manual_issue",
                    updated_at=file.updated_at,
                )
            )
            await db_session.commit()
        return result

    monkeypatch.setattr(recovery.asyncio, "to_thread", checked)
    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)
    assert file.match_method == "manual_issue"
    assert file.matched_issue_id == library.issue_id == wrong.id

"""Completed-import recovery cleanup contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.models.series import Series
from pullbox.models.story_arc import StoryArcResolutionState, StoryArcSourceKind
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.models.user import User
from pullbox.services.import_completed_cleanup import (
    CompletedImportCleanupAction,
    apply_completed_import_cleanup,
    list_completed_import_cleanup_files,
    preview_completed_import_cleanup,
    summarize_completed_import_cleanup_scope,
)
from pullbox.services.import_safety_diagnostics import (
    ImportSafetyCategory,
    build_import_safety_diagnostics,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_job(session: AsyncSession) -> tuple[ImportJob, ImportedSeries]:
    session.add(User(id=42, username="cleanup-operator", password_hash="unused"))
    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.COMPLETED,
    )
    session.add(job)
    await session.flush()
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Long-running library",
        status=ImportSeriesStatus.IMPORTED,
        selected_for_import=False,
    )
    session.add(imported_series)
    await session.flush()
    return job, imported_series


def _blocked_file(
    job: ImportJob,
    imported_series: ImportedSeries,
    *,
    name: str,
    category: ImportSafetyCategory,
    overrideable: bool = False,
) -> ImportedFile:
    block = build_import_safety_diagnostics(
        category.value,
        code=category.value,
        overrideable_hint=overrideable,
    )
    return ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path=f"/comics/{name}",
        file_name=name,
        file_size=1024,
        file_format="cbz",
        status=ImportedFileStatus.SAFETY_BLOCKED,
        diagnostics={"safety_block": block},
        error_message=str(block["reason"]),
    )


@pytest.mark.asyncio
async def test_missing_references_can_be_dismissed_without_deleting_records(
    db_session: AsyncSession,
) -> None:
    job, imported_series = await _seed_job(db_session)
    db_session.add_all(
        [
            _blocked_file(
                job,
                imported_series,
                name=f"missing-{index}.cbz",
                category=ImportSafetyCategory.SOURCE_MISSING,
            )
            for index in range(3)
        ]
    )
    await db_session.commit()

    preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES,
        actor_id=42,
    )
    assert preview.affected_count == 3
    assert preview.preview_token

    result = await apply_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    assert result.affected_count == 3
    assert result.requires_import_retry is False
    assert await db_session.scalar(select(func.count(ImportedFile.id))) == 3
    rows = list((await db_session.scalars(select(ImportedFile))).all())
    assert all(row.status is ImportedFileStatus.SKIPPED for row in rows)
    assert all(row.include_in_import is False for row in rows)
    assert all(row.error_message is None for row in rows)


@pytest.mark.asyncio
async def test_probable_covers_are_separate_from_oversized_files(
    db_session: AsyncSession,
) -> None:
    job, imported_series = await _seed_job(db_session)
    db_session.add_all(
        [
            _blocked_file(
                job,
                imported_series,
                name="cover.cbz",
                category=ImportSafetyCategory.SINGLE_PAGE_COMIC,
            ),
            _blocked_file(
                job,
                imported_series,
                name="large-tpb.cbz",
                category=ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT,
                overrideable=True,
            ),
        ]
    )
    await db_session.commit()

    cover_preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.SKIP_PROBABLE_COVERS,
        actor_id=42,
    )
    assert cover_preview.affected_count == 1

    size_preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.ALLOW_OVERSIZED_FILES,
        actor_id=42,
    )
    assert size_preview.affected_count == 1


@pytest.mark.asyncio
async def test_allow_oversized_files_requeues_only_overrideable_rows(
    db_session: AsyncSession,
) -> None:
    job, imported_series = await _seed_job(db_session)
    approved = _blocked_file(
        job,
        imported_series,
        name="large-tpb.cbz",
        category=ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT,
        overrideable=True,
    )
    not_approved = _blocked_file(
        job,
        imported_series,
        name="unsafe.cbz",
        category=ImportSafetyCategory.DANGEROUS_PATH_OR_PAYLOAD,
        overrideable=True,
    )
    db_session.add_all([approved, not_approved])
    await db_session.commit()

    preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.ALLOW_OVERSIZED_FILES,
        actor_id=42,
    )
    result = await apply_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.ALLOW_OVERSIZED_FILES,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    assert result.requires_import_retry is True
    await db_session.refresh(job)
    await db_session.refresh(imported_series)
    await db_session.refresh(approved)
    await db_session.refresh(not_approved)
    assert job.status is ImportJobStatus.IMPORTING
    assert imported_series.status is ImportSeriesStatus.CONFIRMED
    assert imported_series.selected_for_import is True
    assert approved.status is ImportedFileStatus.CONFIRMED
    assert approved.include_in_import is True
    assert not_approved.status is ImportedFileStatus.SAFETY_BLOCKED


@pytest.mark.asyncio
async def test_accept_recommended_conflicts_requires_one_high_confidence_choice(
    db_session: AsyncSession,
) -> None:
    job, imported_series = await _seed_job(db_session)
    rows = [
        ImportedFile(
            import_job_id=job.id,
            import_series_id=imported_series.id,
            file_path=f"/comics/candidate-{index}.cbz",
            file_name=f"candidate-{index}.cbz",
            file_size=2048 - index,
            file_format="cbz",
            status=ImportedFileStatus.CONFLICT,
            matched_issue_cv_id=1001,
            match_confidence="high" if index == 0 else "medium",
            conflict_group_id=7,
            is_preferred=index == 0,
            diagnostics={"kind": "file_conflict", "preferred_file_id": 1},
        )
        for index in range(2)
    ]
    db_session.add_all(rows)
    await db_session.commit()

    preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS,
        actor_id=42,
    )
    assert preview.affected_count == 1
    assert preview.item_unit == "group"

    result = await apply_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS,
        actor_id=42,
        preview_token=preview.preview_token,
    )
    assert result.affected_count == 1
    assert result.affected_file_count == 2
    assert result.requires_import_retry is True
    assert rows[0].status is ImportedFileStatus.CONFIRMED
    assert rows[0].include_in_import is True
    assert rows[1].status is ImportedFileStatus.SKIPPED
    assert rows[1].include_in_import is False


@pytest.mark.asyncio
async def test_recommended_conflicts_leave_mixed_conflict_series_untouched(
    db_session: AsyncSession,
) -> None:
    job, mixed_series = await _seed_job(db_session)
    safe_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Safe series",
        status=ImportSeriesStatus.IMPORTED,
        selected_for_import=False,
    )
    db_session.add(safe_series)
    await db_session.flush()

    mixed_rows = [
        ImportedFile(
            import_job_id=job.id,
            import_series_id=mixed_series.id,
            file_path=f"/comics/mixed-{index}.cbz",
            file_name=f"mixed-{index}.cbz",
            file_size=100,
            file_format="cbz",
            status=ImportedFileStatus.CONFLICT,
            matched_issue_cv_id=1001,
            match_confidence="high" if index == 0 else "medium",
            conflict_group_id=7,
            is_preferred=index == 0,
        )
        for index in range(2)
    ]
    mixed_rows.extend(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=mixed_series.id,
            file_path=f"/comics/ambiguous-{index}.cbz",
            file_name=f"ambiguous-{index}.cbz",
            file_size=100,
            file_format="cbz",
            status=ImportedFileStatus.CONFLICT,
            matched_issue_cv_id=1002,
            match_confidence="medium",
            conflict_group_id=8,
            is_preferred=False,
        )
        for index in range(2)
    )
    safe_rows = [
        ImportedFile(
            import_job_id=job.id,
            import_series_id=safe_series.id,
            file_path=f"/comics/safe-{index}.cbz",
            file_name=f"safe-{index}.cbz",
            file_size=100,
            file_format="cbz",
            status=ImportedFileStatus.CONFLICT,
            matched_issue_cv_id=2001,
            match_confidence="high" if index == 0 else "medium",
            conflict_group_id=9,
            is_preferred=index == 0,
        )
        for index in range(2)
    ]
    db_session.add_all([*mixed_rows, *safe_rows])
    await db_session.commit()

    preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS,
        actor_id=42,
    )
    result = await apply_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    assert result.affected_count == 1
    assert result.affected_file_count == 2
    assert all(row.status is ImportedFileStatus.CONFLICT for row in mixed_rows)
    assert safe_rows[0].status is ImportedFileStatus.CONFIRMED
    assert safe_rows[1].status is ImportedFileStatus.SKIPPED


@pytest.mark.asyncio
async def test_known_library_issue_conflicts_are_normalized_without_reimport(
    db_session: AsyncSession,
) -> None:
    job, imported_series = await _seed_job(db_session)
    library_root = LibraryRoot(name="Comics", path="/comics", enabled=True)
    series = Series(title="Existing", sort_title="existing", year_start=2020, monitored=True)
    db_session.add_all([library_root, series])
    await db_session.flush()
    issue = Issue(series_id=series.id, issue_number=1, issue_number_text="1")
    db_session.add(issue)
    await db_session.flush()
    db_session.add(
        LibraryFile(
            issue_id=issue.id,
            library_root_id=library_root.id,
            file_path="/comics/existing.cbz",
            file_name="existing.cbz",
            file_size=100,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
        )
    )
    conflict = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path="/imports/existing.cbz",
        file_name="existing.cbz",
        file_size=100,
        file_format="cbz",
        status=ImportedFileStatus.CONFLICT,
        matched_issue_id=issue.id,
        conflict_group_id=10,
        is_preferred=True,
    )
    db_session.add(conflict)
    await db_session.commit()

    preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.NORMALIZE_ALREADY_OWNED,
        actor_id=42,
    )
    result = await apply_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.NORMALIZE_ALREADY_OWNED,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    assert result.affected_count == 1
    assert result.requires_import_retry is False
    await db_session.refresh(conflict)
    assert conflict.status is ImportedFileStatus.ALREADY_OWNED
    assert conflict.include_in_import is False


async def _seed_mixed_folder_candidate(
    session: AsyncSession,
    *,
    target_title: str = "Action Comics",
    target_year: int = 1938,
    source_signal: str = "comicinfo",
    with_library_file: bool = False,
) -> tuple[ImportJob, ImportedSeries, ImportedSeries, ImportedFile, Issue]:
    job, source_series = await _seed_job(session)
    source_series.raw_series_name = "Fritzi Ritz"
    source_series.cv_title = "Fritzi Ritz"

    root = LibraryRoot(name="Comics", path="/comics", enabled=True)
    target = Series(
        title=target_title,
        sort_title=target_title.lower(),
        year_start=target_year,
        monitored=True,
    )
    session.add_all([root, target])
    await session.flush()
    issue = Issue(
        series_id=target.id,
        issue_number=1002,
        issue_number_text="1002",
        comicvine_id=7001002,
    )
    session.add(issue)
    await session.flush()
    target_import_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=target.title,
        raw_year=target.year_start,
        cv_title=target.title,
        cv_year=target.year_start,
        status=ImportSeriesStatus.IMPORTED,
        selected_for_import=False,
        series_id=target.id,
    )
    session.add(target_import_series)
    await session.flush()

    mixed_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=source_series.id,
        file_path="/comics/Fritzi Ritz (1953)/Action Comics 1002.cbz",
        file_name="Action Comics 1002.cbz",
        file_size=2048,
        file_format="cbz",
        parsed_series="Fritzi Ritz",
        parsed_issue_number=1002,
        issue_number_raw="1002",
        has_comicinfo=source_signal == "comicinfo",
        status=ImportedFileStatus.NO_MATCH,
        diagnostics={
            "metadata_signals": {
                "series_name": source_signal,
                "issue_number": source_signal,
            },
            "source_metadata": {
                "comicinfo": {
                    "series": target.title,
                    "number": "1002",
                    "year": 2026,
                }
            },
        },
        error_message="No issue match found.",
    )
    session.add(mixed_file)
    if with_library_file:
        session.add(
            LibraryFile(
                issue_id=issue.id,
                library_root_id=root.id,
                file_path="/comics/Action Comics (1938)/Action Comics 1002.cbz",
                file_name="Action Comics 1002.cbz",
                file_size=1024,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                match_confidence=MatchConfidence.HIGH,
            )
        )
    await session.commit()
    return job, source_series, target_import_series, mixed_file, issue


async def _seed_story_arc_entry_for_mixed_file(
    session: AsyncSession,
    *,
    job: ImportJob,
    mixed_file: ImportedFile,
) -> ImportedStoryArcEntry:
    arc = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key=f"mixed-folder-{mixed_file.id}",
        source_ordinal=1,
        name="Recovered Arc",
    )
    session.add(arc)
    await session.flush()
    entry = ImportedStoryArcEntry(
        imported_story_arc_id=arc.id,
        import_file_id=mixed_file.id,
        source_ordinal=1,
        resolution_state=StoryArcResolutionState.AMBIGUOUS,
        source_kind=StoryArcSourceKind.MYLAR3,
    )
    session.add(entry)
    await session.flush()
    return entry


@pytest.mark.asyncio
async def test_mixed_folder_comicinfo_reassigns_file_and_retries_correct_series(
    db_session: AsyncSession,
) -> None:
    (
        job,
        source_series,
        target_import_series,
        mixed_file,
        issue,
    ) = await _seed_mixed_folder_candidate(db_session)

    preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES,
        actor_id=42,
    )
    assert preview.affected_count == 1
    assert preview.examples == ("Action Comics 1002.cbz",)

    result = await apply_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    await db_session.refresh(job)
    await db_session.refresh(source_series)
    await db_session.refresh(target_import_series)
    await db_session.refresh(mixed_file)
    assert result.requires_import_retry is True
    assert job.status is ImportJobStatus.IMPORTING
    assert mixed_file.import_series_id == target_import_series.id
    assert mixed_file.matched_issue_id == issue.id
    assert mixed_file.matched_issue_cv_id == issue.comicvine_id
    assert mixed_file.status is ImportedFileStatus.CONFIRMED
    assert mixed_file.include_in_import is True
    assert mixed_file.match_confidence == "high"
    assert mixed_file.match_method == "completed_import_metadata_reassignment"
    assert mixed_file.file_path == "/comics/Fritzi Ritz (1953)/Action Comics 1002.cbz"
    assert mixed_file.diagnostics["completed_import_cleanup"]["source_preserved"] is True
    assert source_series.files_no_match == 0
    assert target_import_series.status is ImportSeriesStatus.CONFIRMED
    assert target_import_series.selected_for_import is True


@pytest.mark.asyncio
async def test_mixed_folder_summary_returns_count_and_examples_together(
    db_session: AsyncSession,
) -> None:
    job, _source, _target, _mixed_file, _issue = await _seed_mixed_folder_candidate(db_session)

    summary = await summarize_completed_import_cleanup_scope(
        db_session,
        job.id,
        CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES,
    )

    assert summary.affected_count == 1
    assert summary.affected_file_count == 1
    assert summary.examples == ("Action Comics 1002.cbz",)


@pytest.mark.asyncio
async def test_mixed_folder_target_already_owned_is_resolved_without_retry(
    db_session: AsyncSession,
) -> None:
    (
        job,
        _source_series,
        target_import_series,
        mixed_file,
        issue,
    ) = await _seed_mixed_folder_candidate(db_session, with_library_file=True)
    story_arc_entry = await _seed_story_arc_entry_for_mixed_file(
        db_session,
        job=job,
        mixed_file=mixed_file,
    )
    await db_session.commit()
    target_library_file = (
        await db_session.scalars(select(LibraryFile).where(LibraryFile.issue_id == issue.id))
    ).one()
    preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES,
        actor_id=42,
    )

    result = await apply_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    await db_session.refresh(job)
    await db_session.refresh(mixed_file)
    await db_session.refresh(story_arc_entry)
    assert result.requires_import_retry is False
    assert job.status is ImportJobStatus.COMPLETED
    assert mixed_file.import_series_id == target_import_series.id
    assert mixed_file.matched_issue_id == issue.id
    assert mixed_file.status is ImportedFileStatus.ALREADY_OWNED
    assert mixed_file.include_in_import is False
    assert mixed_file.library_file_id == target_library_file.id
    assert story_arc_entry.matched_issue_id == issue.id
    assert story_arc_entry.resolution_state is StoryArcResolutionState.RESOLVED


@pytest.mark.asyncio
async def test_mixed_folder_cleanup_refuses_ambiguous_existing_target_ownership(
    db_session: AsyncSession,
) -> None:
    (
        job,
        _source_series,
        _target_import_series,
        mixed_file,
        issue,
    ) = await _seed_mixed_folder_candidate(db_session, with_library_file=True)
    existing = (
        await db_session.scalars(select(LibraryFile).where(LibraryFile.issue_id == issue.id))
    ).one()
    db_session.add(
        LibraryFile(
            issue_id=issue.id,
            library_root_id=existing.library_root_id,
            file_path="/comics/Action Comics (1938)/Action Comics 1002 variant.cbz",
            file_name="Action Comics 1002 variant.cbz",
            file_size=2048,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
        )
    )
    await db_session.commit()

    with pytest.raises(ValidationError, match="No files are eligible"):
        await preview_completed_import_cleanup(
            db_session,
            job.id,
            CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES,
            actor_id=42,
        )
    await db_session.refresh(mixed_file)
    assert mixed_file.status is ImportedFileStatus.NO_MATCH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("add_other_owner", "expected_previous_status"),
    ((False, IssueStatus.WANTED), (True, IssueStatus.OWNED)),
    ids=("sole-owner", "other-owner-remains"),
)
async def test_mixed_folder_cleanup_corrects_wrong_referenced_issue_ownership(
    db_session: AsyncSession,
    add_other_owner: bool,
    expected_previous_status: IssueStatus,
) -> None:
    job, source_import_series = await _seed_job(db_session)
    root = LibraryRoot(name="Legacy Mylar", path="/comics", allow_managed_writes=False)
    wrong_series = Series(
        title="Fritzi Ritz",
        sort_title="fritzi ritz",
        year_start=1953,
        monitored=True,
    )
    target_series = Series(
        title="Action Comics",
        sort_title="action comics",
        year_start=1938,
        monitored=True,
    )
    db_session.add_all([root, wrong_series, target_series])
    await db_session.flush()
    source_import_series.series_id = wrong_series.id
    wrong_issue = Issue(
        series_id=wrong_series.id,
        issue_number=1002,
        issue_number_text="1002",
        status=IssueStatus.OWNED,
    )
    target_issue = Issue(
        series_id=target_series.id,
        issue_number=1002,
        issue_number_text="1002",
        status=IssueStatus.WANTED,
    )
    db_session.add_all([wrong_issue, target_issue])
    await db_session.flush()
    target_import_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=target_series.title,
        status=ImportSeriesStatus.IMPORTED,
        series_id=target_series.id,
    )
    library_file = LibraryFile(
        issue_id=wrong_issue.id,
        library_root_id=root.id,
        file_path="/comics/Fritzi Ritz/Action Comics 1002.cbz",
        file_name="Action Comics 1002.cbz",
        file_size=2048,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        storage_mode=LibraryFileStorageMode.REFERENCED,
    )
    db_session.add_all([target_import_series, library_file])
    await db_session.flush()
    if add_other_owner:
        db_session.add(
            LibraryFile(
                issue_id=wrong_issue.id,
                library_root_id=root.id,
                file_path="/comics/Fritzi Ritz/Fritzi Ritz 1002.cbz",
                file_name="Fritzi Ritz 1002.cbz",
                file_size=1024,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                match_confidence=MatchConfidence.HIGH,
                storage_mode=LibraryFileStorageMode.REFERENCED,
            )
        )
        await db_session.flush()
    mixed_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=source_import_series.id,
        file_path=library_file.file_path,
        file_name=library_file.file_name,
        file_size=library_file.file_size,
        file_format="cbz",
        status=ImportedFileStatus.IMPORTED,
        matched_issue_id=wrong_issue.id,
        library_file_id=library_file.id,
        diagnostics={
            "metadata_signals": {
                "series_name": "comicinfo",
                "issue_number": "comicinfo",
            },
            "source_metadata": {"comicinfo": {"series": target_series.title, "number": "1002"}},
        },
    )
    db_session.add(mixed_file)
    await db_session.flush()
    story_arc_entry = await _seed_story_arc_entry_for_mixed_file(
        db_session,
        job=job,
        mixed_file=mixed_file,
    )
    await db_session.commit()

    preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES,
        actor_id=42,
    )
    result = await apply_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    await db_session.refresh(mixed_file)
    await db_session.refresh(library_file)
    await db_session.refresh(wrong_issue)
    await db_session.refresh(target_issue)
    await db_session.refresh(story_arc_entry)
    assert result.requires_import_retry is False
    assert mixed_file.import_series_id == target_import_series.id
    assert mixed_file.matched_issue_id == target_issue.id
    assert mixed_file.library_file_id == library_file.id
    assert mixed_file.status is ImportedFileStatus.IMPORTED
    assert library_file.issue_id == target_issue.id
    assert target_issue.status is IssueStatus.OWNED
    assert wrong_issue.status is expected_previous_status
    assert mixed_file.file_path == "/comics/Fritzi Ritz/Action Comics 1002.cbz"
    assert story_arc_entry.matched_issue_id == target_issue.id
    assert story_arc_entry.resolution_state is StoryArcResolutionState.RESOLVED


@pytest.mark.asyncio
async def test_mixed_folder_cleanup_leaves_ambiguous_series_and_files_untouched(
    db_session: AsyncSession,
) -> None:
    (
        job,
        _source_series,
        _target_import_series,
        mixed_file,
        _issue,
    ) = await _seed_mixed_folder_candidate(db_session)
    duplicate_title = Series(
        title="Action Comics",
        sort_title="action comics",
        year_start=2011,
        monitored=True,
    )
    db_session.add(duplicate_title)
    await db_session.commit()

    with pytest.raises(ValidationError, match="No files are eligible"):
        await preview_completed_import_cleanup(
            db_session,
            job.id,
            CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES,
            actor_id=42,
        )
    await db_session.refresh(mixed_file)
    assert mixed_file.status is ImportedFileStatus.NO_MATCH


@pytest.mark.asyncio
async def test_mixed_folder_cleanup_does_not_trust_filename_only_identity(
    db_session: AsyncSession,
) -> None:
    (
        job,
        _source_series,
        _target_import_series,
        mixed_file,
        _issue,
    ) = await _seed_mixed_folder_candidate(db_session, source_signal="release_title")

    with pytest.raises(ValidationError, match="No files are eligible"):
        await preview_completed_import_cleanup(
            db_session,
            job.id,
            CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES,
            actor_id=42,
        )
    await db_session.refresh(mixed_file)
    assert mixed_file.status is ImportedFileStatus.NO_MATCH


@pytest.mark.asyncio
async def test_cleanup_rejects_non_completed_job(db_session: AsyncSession) -> None:
    job, _imported_series = await _seed_job(db_session)
    job.status = ImportJobStatus.REVIEW
    await db_session.commit()

    with pytest.raises(ValidationError, match="COMPLETED"):
        await preview_completed_import_cleanup(
            db_session,
            job.id,
            CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES,
            actor_id=42,
        )


@pytest.mark.asyncio
async def test_cleanup_file_review_is_bounded_and_paginated(
    db_session: AsyncSession,
) -> None:
    job, imported_series = await _seed_job(db_session)
    db_session.add_all(
        [
            _blocked_file(
                job,
                imported_series,
                name=f"missing-{index:03d}.cbz",
                category=ImportSafetyCategory.SOURCE_MISSING,
            )
            for index in range(60)
        ]
    )
    await db_session.commit()

    result = await list_completed_import_cleanup_files(
        db_session,
        job.id,
        CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES,
        page=3,
        page_size=25,
    )

    assert result.total == 60
    assert result.page == 3
    assert result.total_pages == 3
    assert len(result.items) == 10
    assert result.items[0].file_name == "missing-050.cbz"


@pytest.mark.asyncio
async def test_retry_source_inspection_prepares_existing_retry_pipeline(
    db_session: AsyncSession,
) -> None:
    job, imported_series = await _seed_job(db_session)
    blocked = _blocked_file(
        job,
        imported_series,
        name="temporarily-unreadable.cbz",
        category=ImportSafetyCategory.ARCHIVE_INSPECTION_FAILED,
    )
    db_session.add(blocked)
    await db_session.commit()

    preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION,
        actor_id=42,
    )
    result = await apply_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    assert result.requires_import_retry is True
    await db_session.refresh(job)
    await db_session.refresh(blocked)
    assert job.status is ImportJobStatus.COMPLETED
    assert blocked.status is ImportedFileStatus.FAILED
    assert "safety_block" not in blocked.diagnostics
    assert blocked.diagnostics["source_revalidation"]["retryable"] is True
    assert result.retry_file_ids == (blocked.id,)


@pytest.mark.asyncio
async def test_retry_source_inspection_includes_failed_completed_rechecks(
    db_session: AsyncSession,
) -> None:
    job, imported_series = await _seed_job(db_session)
    failed = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path="/comics/recheck-failed.cbr",
        file_name="recheck-failed.cbr",
        file_size=1024,
        file_format="cbr",
        status=ImportedFileStatus.FAILED,
        diagnostics={
            "source_revalidation": {
                "kind": "source_revalidation",
                "category": ImportSafetyCategory.ARCHIVE_INSPECTION_FAILED.value,
                "code": ImportSafetyCategory.ARCHIVE_INSPECTION_FAILED.value,
                "reason": "Pullbox could not inspect this archive.",
                "retryable": True,
                "overrideable": False,
            }
        },
        error_message="Pullbox could not inspect this archive.",
    )
    db_session.add(failed)
    await db_session.commit()

    preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION,
        actor_id=42,
    )
    result = await apply_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    assert preview.affected_file_count == 1
    assert result.retry_file_ids == (failed.id,)
    await db_session.refresh(failed)
    assert failed.status is ImportedFileStatus.FAILED
    assert failed.diagnostics["source_revalidation"]["retryable"] is True


@pytest.mark.asyncio
async def test_cleanup_preview_cannot_be_reused_after_scope_changes(
    db_session: AsyncSession,
) -> None:
    job, imported_series = await _seed_job(db_session)
    blocked = _blocked_file(
        job,
        imported_series,
        name="missing.cbz",
        category=ImportSafetyCategory.SOURCE_MISSING,
    )
    db_session.add(blocked)
    await db_session.commit()
    preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES,
        actor_id=42,
    )
    blocked.status = ImportedFileStatus.SKIPPED
    await db_session.commit()

    with pytest.raises(ValidationError, match="scope changed"):
        await apply_completed_import_cleanup(
            db_session,
            job.id,
            CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES,
            actor_id=42,
            preview_token=preview.preview_token,
        )


@pytest.mark.asyncio
async def test_cleanup_preview_is_bound_to_the_operator(
    db_session: AsyncSession,
) -> None:
    job, imported_series = await _seed_job(db_session)
    db_session.add(
        _blocked_file(
            job,
            imported_series,
            name="missing.cbz",
            category=ImportSafetyCategory.SOURCE_MISSING,
        )
    )
    await db_session.commit()
    preview = await preview_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES,
        actor_id=42,
    )

    with pytest.raises(ValidationError, match="does not match"):
        await apply_completed_import_cleanup(
            db_session,
            job.id,
            CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES,
            actor_id=7,
            preview_token=preview.preview_token,
        )

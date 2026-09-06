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
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.models.user import User
from pullbox.services.import_completed_cleanup import (
    CompletedImportCleanupAction,
    apply_completed_import_cleanup,
    list_completed_import_cleanup_files,
    preview_completed_import_cleanup,
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

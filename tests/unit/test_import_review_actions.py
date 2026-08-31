"""Tests for import review mutation helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

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
from pullbox.models.series import Series
from pullbox.services.import_review_actions import (
    allow_safety_blocked_file_once,
    bulk_update_file_selection,
    bulk_update_series_selection,
    reset_conflicts,
    resolve_conflict,
    resolve_conflicts,
    skip_safety_blocked_file,
    unmatch_series_match,
    update_file_selection,
    update_series_selection,
)
from pullbox.services.import_service import ImportService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_job_row(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    session.add(job)
    await session.flush()
    return job


async def _create_library_series(session: AsyncSession) -> Series:
    series = Series(title="Absolute Wonder Woman", sort_title="absolute wonder woman")
    session.add(series)
    await session.flush()
    return series


async def _create_imported_series(
    session: AsyncSession,
    job: ImportJob,
    *,
    status: ImportSeriesStatus = ImportSeriesStatus.MATCHED,
    series_id: int | None = None,
    diagnostics: dict[str, object] | None = None,
    selected_for_import: bool = False,
) -> ImportedSeries:
    imported = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Absolute Wonder Woman",
        status=status,
        series_id=series_id,
        file_count=0,
        diagnostics=diagnostics or {},
        selected_for_import=selected_for_import,
    )
    session.add(imported)
    await session.flush()
    return imported


def _make_file(
    job: ImportJob,
    imported: ImportedSeries,
    *,
    name: str,
    status: ImportedFileStatus = ImportedFileStatus.MATCHED,
    conflict_group_id: int | None = None,
    include_in_import: bool = True,
) -> ImportedFile:
    return ImportedFile(
        import_job_id=job.id,
        import_series_id=imported.id,
        file_path=f"/tmp/comics/{name}",
        file_name=name,
        file_size=1024,
        file_format="cbz",
        status=status,
        conflict_group_id=conflict_group_id,
        include_in_import=include_in_import,
    )


async def test_resolve_conflict_selects_chosen_file_and_recomputes(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    imported = await _create_imported_series(db_session, job)
    imported.files_matched = 1
    await db_session.flush()
    chosen = _make_file(
        job,
        imported,
        name="chosen.cbz",
        status=ImportedFileStatus.CONFLICT,
        conflict_group_id=42,
    )
    skipped = _make_file(
        job,
        imported,
        name="skipped.cbz",
        status=ImportedFileStatus.CONFLICT,
        conflict_group_id=42,
    )
    db_session.add_all([chosen, skipped])
    await db_session.flush()
    recompute = AsyncMock()

    files = await resolve_conflict(
        db_session,
        job.id,
        42,
        chosen.id,
        recompute_file_counters=recompute,
    )

    assert {file.id for file in files} == {chosen.id, skipped.id}
    assert chosen.status == ImportedFileStatus.CONFIRMED
    assert chosen.is_preferred is True
    assert chosen.include_in_import is True
    assert skipped.status == ImportedFileStatus.SKIPPED
    assert skipped.is_preferred is False
    assert skipped.include_in_import is False
    recompute.assert_awaited_once_with(db_session, job, [imported.id])


async def test_resolve_conflict_rejects_file_outside_group(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    imported = await _create_imported_series(db_session, job)
    group_file = _make_file(
        job,
        imported,
        name="group.cbz",
        status=ImportedFileStatus.CONFLICT,
        conflict_group_id=42,
    )
    outside_file = _make_file(job, imported, name="outside.cbz")
    db_session.add_all([group_file, outside_file])
    await db_session.flush()

    with pytest.raises(ValidationError):
        await resolve_conflict(
            db_session,
            job.id,
            42,
            outside_file.id,
            recompute_file_counters=AsyncMock(),
        )


async def test_resolve_conflicts_batches_groups_and_recomputes_once(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    first_series = await _create_imported_series(db_session, job)
    second_series = await _create_imported_series(db_session, job)
    first_chosen = _make_file(
        job,
        first_series,
        name="first-chosen.cbz",
        status=ImportedFileStatus.CONFLICT,
        conflict_group_id=41,
    )
    first_skipped = _make_file(
        job,
        first_series,
        name="first-skipped.cbz",
        status=ImportedFileStatus.CONFLICT,
        conflict_group_id=41,
    )
    second_chosen = _make_file(
        job,
        second_series,
        name="second-chosen.cbz",
        status=ImportedFileStatus.CONFLICT,
        conflict_group_id=42,
    )
    second_skipped = _make_file(
        job,
        second_series,
        name="second-skipped.cbz",
        status=ImportedFileStatus.CONFLICT,
        conflict_group_id=42,
    )
    db_session.add_all([first_chosen, first_skipped, second_chosen, second_skipped])
    await db_session.flush()
    recompute = AsyncMock()

    resolved_group_ids, resolved_series_ids, files_by_group = await resolve_conflicts(
        db_session,
        job.id,
        [(41, first_chosen.id), (42, second_chosen.id)],
        recompute_file_counters=recompute,
    )

    assert resolved_group_ids == [41, 42]
    assert resolved_series_ids == [first_series.id, second_series.id]
    assert {file.id for file in files_by_group[41]} == {first_chosen.id, first_skipped.id}
    assert {file.id for file in files_by_group[42]} == {second_chosen.id, second_skipped.id}
    assert first_chosen.status == ImportedFileStatus.CONFIRMED
    assert second_chosen.status == ImportedFileStatus.CONFIRMED
    assert first_skipped.status == ImportedFileStatus.SKIPPED
    assert second_skipped.status == ImportedFileStatus.SKIPPED
    assert first_series.selected_for_import is True
    assert second_series.selected_for_import is True
    recompute.assert_awaited_once_with(
        db_session,
        job,
        [first_series.id, second_series.id],
    )


async def test_reset_conflicts_restores_review_state_and_counters(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    imported = await _create_imported_series(db_session, job)
    chosen = _make_file(
        job,
        imported,
        name="chosen.cbz",
        status=ImportedFileStatus.CONFIRMED,
        conflict_group_id=42,
        include_in_import=True,
    )
    chosen.is_preferred = True
    skipped = _make_file(
        job,
        imported,
        name="skipped.cbz",
        status=ImportedFileStatus.SKIPPED,
        conflict_group_id=42,
        include_in_import=False,
    )
    db_session.add_all([chosen, skipped])
    await db_session.flush()
    chosen.diagnostics = {"preferred_file_id": chosen.id}
    skipped.diagnostics = {"preferred_file_id": chosen.id}
    recompute = AsyncMock()

    reset_group_ids, reset_series_ids = await reset_conflicts(
        db_session,
        job.id,
        [42],
        recompute_file_counters=recompute,
    )

    assert reset_group_ids == [42]
    assert reset_series_ids == [imported.id]
    assert chosen.status == ImportedFileStatus.CONFLICT
    assert skipped.status == ImportedFileStatus.CONFLICT
    assert chosen.include_in_import is False
    assert skipped.include_in_import is False
    assert chosen.is_preferred is True
    assert skipped.is_preferred is False
    assert imported.selected_for_import is False
    recompute.assert_awaited_once_with(db_session, job, [imported.id])


async def test_update_series_selection_toggles_matched_series(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    imported = await _create_imported_series(db_session, job)
    imported.files_matched = 1
    await db_session.flush()

    updated = await update_series_selection(
        db_session,
        job.id,
        imported.id,
        include_in_import=True,
    )

    assert updated.selected_for_import is True


async def test_unmatch_series_match_returns_matched_row_to_series_review(
    db_session: AsyncSession,
) -> None:
    """Users can reject an incorrect auto-match before importing."""
    job = await _create_job_row(db_session)
    imported = await _create_imported_series(db_session, job, selected_for_import=True)
    imported.cv_id = 143970
    imported.cv_title = "Savage Tales One-Shot"
    imported.cv_year = 2022
    imported.cv_publisher = "Dynamite Entertainment"
    imported.cv_issue_count = 1
    imported.cv_url = "https://comicvine.gamespot.com/savage-tales-one-shot/4050-143970/"
    imported.cv_match_score = 0.84
    imported.cv_match_method = "alternate_release_candidate"
    imported.files_total = 1
    imported.files_matched = 1
    imported.diagnostics = {"kind": "series_match", "reason": "alternate_release_candidate"}
    imp_file = _make_file(job, imported, name="Giant-Sized Savage Tales.cbz")
    imp_file.matched_issue_cv_id = 935006
    imp_file.match_confidence = "medium"
    imp_file.match_method = "issue_number"
    imp_file.conflict_group_id = 42
    imp_file.duplicate_group_id = 7
    imp_file.is_preferred = True
    imp_file.diagnostics = {"target_issue_summary": {"provider_id": "935006"}}
    db_session.add(imp_file)
    await db_session.flush()
    recompute_files = AsyncMock()
    recompute_series = AsyncMock()

    updated = await unmatch_series_match(
        db_session,
        job.id,
        imported.id,
        recompute_file_counters=recompute_files,
        recompute_series_counters=recompute_series,
    )

    assert updated.id == imported.id
    assert imported.status == ImportSeriesStatus.NO_MATCH
    assert imported.cv_id is None
    assert imported.cv_title is None
    assert imported.cv_year is None
    assert imported.cv_match_score is None
    assert imported.cv_match_method is None
    assert imported.user_selected_cv_id is None
    assert imported.selected_for_import is False
    assert imported.diagnostics["kind"] == "series_no_match"
    assert imported.diagnostics["reason"] == "series_unmatched_by_user"
    assert imported.diagnostics["previous_match"]["cv_id"] == 143970
    assert imp_file.status == ImportedFileStatus.NO_MATCH
    assert imp_file.include_in_import is False
    assert imp_file.matched_issue_cv_id is None
    assert imp_file.match_confidence is None
    assert imp_file.match_method is None
    assert imp_file.conflict_group_id is None
    assert imp_file.duplicate_group_id is None
    assert imp_file.is_preferred is False
    assert imp_file.diagnostics["kind"] == "file_no_match"
    assert imp_file.diagnostics["target_state"] == "needs_series_match"
    recompute_files.assert_awaited_once_with(db_session, job, [imported.id])
    recompute_series.assert_awaited_once_with(db_session, job)


async def test_bulk_update_series_selection_selects_visible_matched_rows(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    first = await _create_imported_series(db_session, job)
    second = await _create_imported_series(db_session, job)
    conflicted = await _create_imported_series(db_session, job)
    first.files_matched = 1
    second.files_matched = 1
    conflicted.files_conflict = 1
    await db_session.flush()

    updated_count = await bulk_update_series_selection(
        db_session,
        job.id,
        include_in_import=True,
        imported_series_ids=[first.id, second.id],
    )

    assert updated_count == 2
    assert first.selected_for_import is True
    assert second.selected_for_import is True
    assert conflicted.selected_for_import is False


async def test_bulk_update_series_selection_selects_all_importable_matched_rows(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    first = await _create_imported_series(db_session, job)
    second = await _create_imported_series(db_session, job)
    conflicted = await _create_imported_series(db_session, job)
    skipped = await _create_imported_series(db_session, job, status=ImportSeriesStatus.SKIPPED)
    first.files_matched = 1
    second.files_matched = 1
    conflicted.files_conflict = 1
    await db_session.flush()

    updated_count = await bulk_update_series_selection(
        db_session,
        job.id,
        include_in_import=True,
        imported_series_ids=[],
    )

    assert updated_count == 2
    assert first.selected_for_import is True
    assert second.selected_for_import is True
    assert conflicted.selected_for_import is False
    assert skipped.selected_for_import is False


async def test_bulk_update_series_selection_clears_all_selected_rows(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    first = await _create_imported_series(db_session, job, selected_for_import=True)
    second = await _create_imported_series(db_session, job, selected_for_import=True)

    updated_count = await bulk_update_series_selection(
        db_session,
        job.id,
        include_in_import=False,
        imported_series_ids=[],
    )

    assert updated_count == 2
    assert first.selected_for_import is False
    assert second.selected_for_import is False


async def test_update_file_selection_toggles_duplicate_matched_file(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    library_series = await _create_library_series(db_session)
    imported = await _create_imported_series(
        db_session,
        job,
        status=ImportSeriesStatus.DUPLICATE,
        series_id=library_series.id,
        diagnostics={"actionable_duplicate_merge": True},
    )
    imp_file = _make_file(job, imported, name="issue.cbz", include_in_import=True)
    db_session.add(imp_file)
    await db_session.flush()

    updated = await update_file_selection(
        db_session,
        job.id,
        imp_file.id,
        include_in_import=False,
    )

    assert updated.include_in_import is False


async def test_update_file_selection_allows_duplicate_with_stale_non_actionable_diagnostics(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    library_series = await _create_library_series(db_session)
    imported = await _create_imported_series(
        db_session,
        job,
        status=ImportSeriesStatus.DUPLICATE,
        series_id=library_series.id,
        diagnostics={
            "actionable_duplicate_merge": False,
            "fully_owned_series": True,
        },
    )
    imported.files_matched = 1
    imp_file = _make_file(job, imported, name="issue-020.cbz", include_in_import=False)
    db_session.add(imp_file)
    await db_session.flush()

    updated = await update_file_selection(
        db_session,
        job.id,
        imp_file.id,
        include_in_import=True,
    )

    assert updated.include_in_import is True


async def test_bulk_update_file_selection_limits_to_actionable_duplicate_series(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    library_series = await _create_library_series(db_session)
    imported = await _create_imported_series(
        db_session,
        job,
        status=ImportSeriesStatus.DUPLICATE,
        series_id=library_series.id,
        diagnostics={"actionable_duplicate_merge": True},
    )
    included = _make_file(job, imported, name="included.cbz", include_in_import=False)
    no_match = _make_file(
        job,
        imported,
        name="no-match.cbz",
        status=ImportedFileStatus.NO_MATCH,
        include_in_import=False,
    )
    db_session.add_all([included, no_match])
    await db_session.flush()

    updated_count = await bulk_update_file_selection(
        db_session,
        job.id,
        include_in_import=True,
        imported_series_id=imported.id,
    )

    assert updated_count == 1
    assert included.include_in_import is True
    assert no_match.include_in_import is False


async def test_bulk_clear_file_selection_clears_stale_duplicate_selections(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    library_series = await _create_library_series(db_session)
    imported = await _create_imported_series(
        db_session,
        job,
        status=ImportSeriesStatus.DUPLICATE,
        series_id=library_series.id,
        diagnostics={"actionable_duplicate_merge": False},
    )
    confirmed = _make_file(
        job,
        imported,
        name="confirmed.cbz",
        status=ImportedFileStatus.CONFIRMED,
        include_in_import=True,
    )
    matched = _make_file(job, imported, name="matched.cbz", include_in_import=True)
    no_match = _make_file(
        job,
        imported,
        name="no-match.cbz",
        status=ImportedFileStatus.NO_MATCH,
        include_in_import=True,
    )
    db_session.add_all([confirmed, matched, no_match])
    await db_session.flush()

    updated_count = await bulk_update_file_selection(
        db_session,
        job.id,
        include_in_import=False,
    )

    assert updated_count == 3
    assert confirmed.include_in_import is False
    assert matched.include_in_import is False
    assert no_match.include_in_import is False


async def test_import_service_review_action_shims_remain_available(
    db_session: AsyncSession,
) -> None:
    service = ImportService(
        series_service=AsyncMock(),
        metadata_service=AsyncMock(),
        event_bus=AsyncMock(),
    )
    job = await _create_job_row(db_session)
    library_series = await _create_library_series(db_session)
    imported = await _create_imported_series(
        db_session,
        job,
        status=ImportSeriesStatus.DUPLICATE,
        series_id=library_series.id,
        diagnostics={"actionable_duplicate_merge": True},
    )
    imp_file = _make_file(job, imported, name="issue.cbz", include_in_import=True)
    db_session.add(imp_file)
    await db_session.flush()

    selected = await service.update_file_selection(
        db_session,
        job.id,
        imp_file.id,
        include_in_import=False,
    )
    assert selected.include_in_import is False

    updated_count = await service.bulk_update_file_selection(
        db_session,
        job.id,
        include_in_import=True,
        imported_series_id=imported.id,
    )

    assert updated_count == 1
    assert selected.include_in_import is True

    matched = await _create_imported_series(db_session, job)
    matched.files_matched = 1
    await db_session.flush()
    selected_series = await service.update_series_selection(
        db_session,
        job.id,
        matched.id,
        include_in_import=True,
    )
    assert selected_series.selected_for_import is True

    cleared_count = await service.bulk_update_series_selection(
        db_session,
        job.id,
        include_in_import=False,
        imported_series_ids=[],
    )
    assert cleared_count >= 1
    assert matched.selected_for_import is False


async def test_allow_safety_blocked_file_once_keeps_file_in_safety_review_until_rematched(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    imported = await _create_imported_series(db_session, job, selected_for_import=True)
    imp_file = _make_file(
        job,
        imported,
        name="oversized.cbz",
        status=ImportedFileStatus.SAFETY_BLOCKED,
        include_in_import=True,
    )
    imp_file.error_message = "Archive decompressed size exceeds limit"
    imp_file.diagnostics = {
        "safety_block": {
            "kind": "file_safety_blocked",
            "reason": "Archive decompressed size exceeds limit",
            "details": ["/tmp/comics/oversized.cbz"],
        }
    }
    db_session.add(imp_file)
    await db_session.flush()
    recompute_files = AsyncMock()
    recompute_series = AsyncMock()

    updated_file = await allow_safety_blocked_file_once(
        db_session,
        job.id,
        imp_file.id,
        recompute_file_counters=recompute_files,
        recompute_series_counters=recompute_series,
    )

    assert updated_file.status == ImportedFileStatus.SAFETY_APPROVED
    assert updated_file.include_in_import is False
    assert updated_file.error_message is None
    assert imported.selected_for_import is False
    assert updated_file.diagnostics["safety_exception"]["allowed_once"] is True
    previous_block = updated_file.diagnostics["safety_exception"]["previous_block"]
    assert previous_block["category"] == "decompression_size_limit"
    assert previous_block["code"] == "archive_decompressed_size_limit"
    assert previous_block["reason"] == (
        "The archive exceeds Pullbox's configured decompressed-size limit."
    )
    assert "/tmp/comics" not in str(previous_block)
    recompute_files.assert_awaited_once_with(db_session, job, [imported.id])
    recompute_series.assert_awaited_once_with(db_session, job)


async def test_allow_safety_blocked_file_once_rejects_non_overrideable_blocks(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    imported = await _create_imported_series(db_session, job, selected_for_import=True)
    imp_file = _make_file(
        job,
        imported,
        name="corrupt.cbz",
        status=ImportedFileStatus.SAFETY_BLOCKED,
        include_in_import=True,
    )
    imp_file.error_message = "Archive could not be inspected"
    imp_file.diagnostics = {
        "safety_block": {
            "kind": "file_safety_blocked",
            "reason": "Archive could not be inspected",
            "details": ["/tmp/comics/corrupt.cbz"],
            "overrideable": False,
        }
    }
    db_session.add(imp_file)
    await db_session.flush()
    recompute_files = AsyncMock()
    recompute_series = AsyncMock()

    with pytest.raises(ValidationError, match="cannot be overridden"):
        await allow_safety_blocked_file_once(
            db_session,
            job.id,
            imp_file.id,
            recompute_file_counters=recompute_files,
            recompute_series_counters=recompute_series,
        )

    assert imp_file.status == ImportedFileStatus.SAFETY_BLOCKED
    assert imp_file.include_in_import is True
    assert "safety_exception" not in imp_file.diagnostics
    recompute_files.assert_not_awaited()
    recompute_series.assert_not_awaited()


async def test_allow_safety_blocked_file_once_rejects_dangerous_legacy_override_hint(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    imported = await _create_imported_series(db_session, job, selected_for_import=True)
    imp_file = _make_file(
        job,
        imported,
        name="unsafe.cbz",
        status=ImportedFileStatus.SAFETY_BLOCKED,
        include_in_import=True,
    )
    imp_file.diagnostics = {
        "safety_block": {
            "kind": "file_safety_blocked",
            "reason": "Archive contains path traversal entries",
            "details": ["../../private"],
            "overrideable": True,
        }
    }
    db_session.add(imp_file)
    await db_session.flush()
    recompute_files = AsyncMock()
    recompute_series = AsyncMock()

    with pytest.raises(ValidationError, match="cannot be overridden"):
        await allow_safety_blocked_file_once(
            db_session,
            job.id,
            imp_file.id,
            recompute_file_counters=recompute_files,
            recompute_series_counters=recompute_series,
        )

    assert imp_file.status == ImportedFileStatus.SAFETY_BLOCKED
    assert "safety_exception" not in imp_file.diagnostics
    recompute_files.assert_not_awaited()
    recompute_series.assert_not_awaited()


async def test_allow_safety_blocked_file_once_retry_requeues_terminal_import(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    job.status = ImportJobStatus.COMPLETED
    job.error_message = "previous import failed"
    imported = await _create_imported_series(
        db_session,
        job,
        status=ImportSeriesStatus.IMPORTED,
        selected_for_import=False,
    )
    imported.error_message = "greenlet_spawn has not been called"
    imp_file = _make_file(
        job,
        imported,
        name="oversized.cbz",
        status=ImportedFileStatus.SAFETY_BLOCKED,
        include_in_import=False,
    )
    imp_file.error_message = "Archive decompressed size exceeds limit"
    imp_file.diagnostics = {
        "safety_block": {
            "kind": "archive_decompressed_size",
            "reason": "Archive decompressed size exceeds limit",
            "details": ["/tmp/comics/oversized.cbz"],
            "overrideable": True,
        }
    }
    db_session.add(imp_file)
    await db_session.flush()
    recompute_files = AsyncMock()
    recompute_series = AsyncMock()

    updated_file = await allow_safety_blocked_file_once(
        db_session,
        job.id,
        imp_file.id,
        recompute_file_counters=recompute_files,
        recompute_series_counters=recompute_series,
        retry_import=True,
    )

    assert job.status == ImportJobStatus.IMPORTING
    assert job.error_message is None
    assert imported.status == ImportSeriesStatus.CONFIRMED
    assert imported.selected_for_import is True
    assert imported.error_message is None
    assert updated_file.status == ImportedFileStatus.CONFIRMED
    assert updated_file.include_in_import is True
    assert updated_file.error_message is None
    assert updated_file.diagnostics["safety_exception"]["allowed_once"] is True
    assert "safety_block" not in updated_file.diagnostics
    recompute_files.assert_awaited_once_with(db_session, job, [imported.id])
    recompute_series.assert_awaited_once_with(db_session, job)


async def test_skip_safety_blocked_file_marks_file_skipped_and_unselects_series(
    db_session: AsyncSession,
) -> None:
    job = await _create_job_row(db_session)
    imported = await _create_imported_series(db_session, job, selected_for_import=True)
    imp_file = _make_file(
        job,
        imported,
        name="oversized.cbz",
        status=ImportedFileStatus.SAFETY_BLOCKED,
    )
    db_session.add(imp_file)
    await db_session.flush()
    recompute_files = AsyncMock()
    recompute_series = AsyncMock()

    updated_file = await skip_safety_blocked_file(
        db_session,
        job.id,
        imp_file.id,
        recompute_file_counters=recompute_files,
        recompute_series_counters=recompute_series,
    )

    assert updated_file.status == ImportedFileStatus.SKIPPED
    assert updated_file.include_in_import is False
    assert imported.selected_for_import is False
    assert updated_file.diagnostics["resolution"] == "skipped"

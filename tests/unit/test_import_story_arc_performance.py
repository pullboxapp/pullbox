"""Query-growth guards for large story-arc import phases."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, func, select

import pullbox.services.import_story_arc_materialization as materialization_module
from pullbox.core.mylar3_reader import (
    Mylar3ArcSettingsSnapshot,
    Mylar3CollectionSnapshot,
    Mylar3StoryArcEntrySnapshot,
    Mylar3StoryArcSnapshot,
)
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArc,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services.import_job_actions import record_action
from pullbox.services.import_rollback_state import restore_review_state_after_rollback
from pullbox.services.import_story_arc_materialization import (
    materialize_confirmed_story_arcs,
)
from pullbox.services.import_story_arc_review import confirm_import_story_arcs
from pullbox.services.import_story_arc_staging import stage_mylar_story_arcs

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _job(session: AsyncSession, *, status: ImportJobStatus) -> ImportJob:
    job = ImportJob(
        source_path="/synthetic/scale",
        source_type=ImportSourceType.MYLAR3,
        status=status,
    )
    session.add(job)
    await session.flush()
    return job


async def _staged_arcs(
    session: AsyncSession,
    *,
    job: ImportJob,
    count: int,
    status: ImportedStoryArcStatus,
) -> None:
    for ordinal in range(1, count + 1):
        arc = ImportedStoryArc(
            import_job_id=job.id,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key=f"mylar3:scale:{ordinal}",
            source_arc_id=f"scale-{ordinal}",
            source_ordinal=ordinal,
            name=f"Scale Arc {ordinal}",
            status=status,
            selected_for_import=True,
        )
        session.add(arc)
        session.add(
            ImportedStoryArcEntry(
                imported_story_arc=arc,
                source_ordinal=1,
                reading_order=ordinal,
                resolution_state=StoryArcResolutionState.MISSING,
                source_kind=StoryArcSourceKind.MYLAR3,
                source_entry_id=f"scale-entry-{ordinal}",
                source_arc_id=arc.source_arc_id,
                source_issue_number_text="1AU",
                selected_for_import=True,
            )
        )
    await session.flush()


async def _select_count(
    engine: AsyncEngine,
    operation: Callable[[], Awaitable[object]],
) -> int:
    statements: list[str] = []

    def record_statement(*args: object) -> None:
        statement = str(args[2])
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        await operation()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_statement)
    return len(statements)


def _mylar_scale_snapshot(*, arc_count: int) -> Mylar3CollectionSnapshot:
    arcs: list[Mylar3StoryArcSnapshot] = []
    for ordinal in range(1, arc_count + 1):
        arc_id = f"scale-{ordinal}"
        arcs.append(
            Mylar3StoryArcSnapshot(
                story_arc_id=arc_id,
                cv_arc_id=None,
                name=f"Scale Arc {ordinal}",
                entries=(
                    Mylar3StoryArcEntrySnapshot(
                        ordinal=1,
                        reading_order=ordinal,
                        reading_order_raw=str(ordinal),
                        story_arc_id=arc_id,
                        story_arc_name=f"Scale Arc {ordinal}",
                        cv_arc_id=None,
                        issue_arc_id=f"entry-{ordinal}",
                        issue_id=None,
                        comic_id=None,
                        issue_number="1AU",
                        comic_name="Scale Series",
                        series_year=None,
                        issue_year=None,
                        status="Wanted",
                        location=None,
                        release_date=None,
                        issue_date=None,
                        publisher=None,
                        issue_publisher=None,
                        issue_name=None,
                        manual=None,
                        date_added=None,
                        digital_date=None,
                        issue_type=None,
                        aliases=None,
                        total_issues=None,
                        in_cache_dir=None,
                        int_issue_number=None,
                        dynamic_comic_name=None,
                        volume=None,
                        arc_image=None,
                    ),
                ),
            )
        )
    return Mylar3CollectionSnapshot(
        series=(),
        story_arcs=tuple(arcs),
        storyarcs_present=True,
        readlist_present=False,
        readlist_count=0,
        arc_settings=Mylar3ArcSettingsSnapshot(
            present=False,
            parse_warnings=(),
            values=(),
        ),
    )


@pytest.mark.asyncio
async def test_record_action_allocates_many_sequences_with_one_select(
    async_engine: AsyncEngine,
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session, status=ImportJobStatus.IMPORTING)
    db_session.add(
        ImportJobAction(
            import_job_id=job.id,
            sequence_no=87,
            phase="existing",
            action_type="existing_action",
            status=ImportJobActionStatus.COMPLETED,
            payload={},
        )
    )
    await db_session.flush()
    allocated_sequences: list[int] = []

    async def write_actions() -> object:
        for ordinal in range(1, 51):
            action = await record_action(
                db_session,
                job,
                phase="scale",
                action_type="scale_action",
                payload={"ordinal": ordinal},
            )
            allocated_sequences.append(action.sequence_no)
        return None

    selects = await _select_count(async_engine, write_actions)

    assert selects <= 1, f"50 journal actions issued {selects} SELECT statements"
    assert allocated_sequences == list(range(88, 138))


@pytest.mark.asyncio
async def test_mylar_staging_flushes_only_at_batch_boundaries(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session, status=ImportJobStatus.SCANNING)
    flush_count = 0

    def record_flush(*_args: object) -> None:
        nonlocal flush_count
        flush_count += 1

    event.listen(db_session.sync_session, "before_flush", record_flush)
    try:
        result = await stage_mylar_story_arcs(
            db_session,
            import_job_id=job.id,
            snapshot=_mylar_scale_snapshot(arc_count=40),
            batch_size=20,
        )
    finally:
        event.remove(db_session.sync_session, "before_flush", record_flush)

    assert result.arcs_staged == 40
    assert flush_count <= 2, f"40 arcs across two pages triggered {flush_count} flushes"


@pytest.mark.asyncio
async def test_confirming_many_arcs_uses_bounded_keyset_pages(
    async_engine: AsyncEngine,
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session, status=ImportJobStatus.REVIEW)
    await _staged_arcs(
        db_session,
        job=job,
        count=1_001,
        status=ImportedStoryArcStatus.READY,
    )

    async def confirm() -> object:
        return await confirm_import_story_arcs(
            db_session,
            job.id,
            story_arc_ids=(),
            decisions=(),
            batch_size=100,
        )

    selects = await _select_count(async_engine, confirm)

    expected_pages = (1_001 + 100 - 1) // 100
    assert expected_pages * 2 <= selects <= (expected_pages * 2) + 2


@pytest.mark.asyncio
async def test_materializing_many_arcs_has_batched_query_count(
    async_engine: AsyncEngine,
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session, status=ImportJobStatus.IMPORTING)
    await _staged_arcs(
        db_session,
        job=job,
        count=40,
        status=ImportedStoryArcStatus.CONFIRMED,
    )

    async def materialize() -> object:
        return await materialize_confirmed_story_arcs(
            db_session,
            import_job_id=job.id,
            batch_size=20,
        )

    selects = await _select_count(async_engine, materialize)

    assert selects <= 16, f"materializing 40 staged arcs issued {selects} SELECT statements"


@pytest.mark.asyncio
async def test_restart_recovery_lookup_work_is_linear_not_arc_squared(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = await _job(db_session, status=ImportJobStatus.IMPORTING)
    arc_count = 300
    canonical_arcs: list[StoryArc] = []
    staged_arcs: list[ImportedStoryArc] = []
    for ordinal in range(1, arc_count + 1):
        source_key = f"recovery-scale:{ordinal}"
        canonical_arcs.append(
            StoryArc(
                name=f"Recovery Arc {ordinal}",
                source_kind=StoryArcSourceKind.MYLAR3,
                source_import_job_id=job.id,
                diagnostics={
                    "schema_version": 1,
                    "import_identity": {
                        "import_job_id": job.id,
                        "source_key": source_key,
                    },
                },
            )
        )
        staged_arcs.append(
            ImportedStoryArc(
                import_job_id=job.id,
                source_kind=StoryArcSourceKind.MYLAR3,
                source_key=source_key,
                source_arc_id=f"source-{ordinal}",
                source_ordinal=ordinal,
                name=f"Recovery Arc {ordinal}",
                status=ImportedStoryArcStatus.CONFIRMED,
                selected_for_import=True,
            )
        )
    db_session.add_all([*canonical_arcs, *staged_arcs])
    await db_session.flush()

    identity_comparisons = 0
    original_match = materialization_module._arc_matches_import_identity

    def counted_match(*args: object, **kwargs: object) -> bool:
        nonlocal identity_comparisons
        identity_comparisons += 1
        return original_match(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        materialization_module,
        "_arc_matches_import_identity",
        counted_match,
    )

    result = await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        batch_size=100,
    )

    assert result.arcs_reused == arc_count
    assert identity_comparisons <= arc_count


@pytest.mark.asyncio
async def test_materialization_checks_cancellation_between_bounded_batches(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session, status=ImportJobStatus.IMPORTING)
    await _staged_arcs(
        db_session,
        job=job,
        count=45,
        status=ImportedStoryArcStatus.CONFIRMED,
    )
    checkpoint_calls = 0

    async def cancel_before_second_batch() -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        if checkpoint_calls == 5:
            raise RuntimeError("synthetic cancellation")

    with pytest.raises(RuntimeError, match="synthetic cancellation"):
        await materialize_confirmed_story_arcs(
            db_session,
            import_job_id=job.id,
            batch_size=20,
            cancellation_check=cancel_before_second_batch,
        )

    imported_count = await db_session.scalar(
        select(func.count())
        .select_from(ImportedStoryArc)
        .where(ImportedStoryArc.status == ImportedStoryArcStatus.IMPORTED)
    )
    assert checkpoint_calls == 5
    assert imported_count == 20


@pytest.mark.asyncio
async def test_single_large_arc_checks_cancellation_between_entry_chunks(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session, status=ImportJobStatus.IMPORTING)
    staged_arc = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key="large-single-arc",
        source_arc_id="large-single-arc",
        source_ordinal=1,
        name="Large Single Arc",
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
    )
    db_session.add(staged_arc)
    await db_session.flush()
    db_session.add_all(
        [
            ImportedStoryArcEntry(
                imported_story_arc_id=staged_arc.id,
                source_ordinal=ordinal,
                reading_order=ordinal,
                resolution_state=StoryArcResolutionState.MISSING,
                source_kind=StoryArcSourceKind.MYLAR3,
                source_entry_id=f"large-entry-{ordinal}",
                source_arc_id=staged_arc.source_arc_id,
                source_issue_number_text="1AU",
                selected_for_import=True,
            )
            for ordinal in range(1, 601)
        ]
    )
    await db_session.flush()
    checkpoint_calls = 0

    async def cancel_at_first_entry_chunk_boundary() -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        if checkpoint_calls == 6:
            raise RuntimeError("synthetic entry cancellation")

    with pytest.raises(RuntimeError, match="synthetic entry cancellation"):
        await materialize_confirmed_story_arcs(
            db_session,
            import_job_id=job.id,
            batch_size=1,
            entry_checkpoint_size=250,
            cancellation_check=cancel_at_first_entry_chunk_boundary,
        )

    membership_count = await db_session.scalar(select(func.count()).select_from(IssueStoryArc))
    assert checkpoint_calls == 6
    assert membership_count == 250


@pytest.mark.asyncio
async def test_single_large_arc_loads_entry_orm_rows_in_bounded_pages(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = await _job(db_session, status=ImportJobStatus.IMPORTING)
    staged_arc = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key="large-page-proof",
        source_arc_id="large-page-proof",
        source_ordinal=1,
        name="Large Page Proof",
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
    )
    db_session.add(staged_arc)
    await db_session.flush()
    db_session.add_all(
        [
            ImportedStoryArcEntry(
                imported_story_arc_id=staged_arc.id,
                source_ordinal=ordinal,
                reading_order=ordinal,
                resolution_state=StoryArcResolutionState.MISSING,
                source_kind=StoryArcSourceKind.MYLAR3,
                source_entry_id=f"page-entry-{ordinal}",
                source_arc_id=staged_arc.source_arc_id,
                source_issue_number_text="1AU",
                selected_for_import=True,
            )
            for ordinal in range(1, 602)
        ]
    )
    await db_session.flush()
    observed_page_sizes: list[int] = []
    original_prepare = materialization_module._prepare_entry_page_lookups

    async def observed_prepare(*args: object, **kwargs: object) -> object:
        entries = kwargs["entries"]
        assert isinstance(entries, list)
        observed_page_sizes.append(len(entries))
        return await original_prepare(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        materialization_module,
        "_prepare_entry_page_lookups",
        observed_prepare,
    )

    result = await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        batch_size=1,
        entry_checkpoint_size=250,
    )

    assert result.memberships_created == 601
    assert observed_page_sizes == [250, 250, 101]


@pytest.mark.asyncio
async def test_rollback_review_state_uses_keyset_bounded_pages(
    async_engine: AsyncEngine,
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session, status=ImportJobStatus.ROLLING_BACK)
    imported_series = [
        ImportedSeries(
            import_job_id=job.id,
            raw_series_name=f"Scale Series {ordinal}",
            file_count=1,
            status=ImportSeriesStatus.IMPORTED,
            files_imported=1,
        )
        for ordinal in range(1, 121)
    ]
    db_session.add_all(imported_series)
    await db_session.flush()
    imported_files = [
        ImportedFile(
            import_job_id=job.id,
            import_series_id=series.id,
            file_path=f"/synthetic/{ordinal}.cbz",
            file_name=f"{ordinal}.cbz",
            file_size=0,
            file_format="cbz",
            status=ImportedFileStatus.IMPORTED,
            include_in_import=True,
        )
        for ordinal, series in enumerate(imported_series, start=1)
    ]
    staged_arcs = [
        ImportedStoryArc(
            import_job_id=job.id,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key=f"rollback:{ordinal}",
            source_ordinal=ordinal,
            name=f"Rollback Arc {ordinal}",
            status=ImportedStoryArcStatus.IMPORTED,
            selected_for_import=True,
        )
        for ordinal in range(1, 121)
    ]
    db_session.add_all([*imported_files, *staged_arcs])
    await db_session.flush()
    db_session.add_all(
        [
            ImportedStoryArcEntry(
                imported_story_arc_id=arc.id,
                source_ordinal=1,
                reading_order=ordinal,
                resolution_state=StoryArcResolutionState.MISSING,
                source_kind=StoryArcSourceKind.MYLAR3,
                selected_for_import=True,
                diagnostics={"materialization": {"status": "imported"}},
            )
            for ordinal, arc in enumerate(staged_arcs, start=1)
        ]
    )
    await db_session.flush()

    async def restore() -> object:
        return await restore_review_state_after_rollback(
            db_session,
            job.id,
            batch_size=25,
        )

    selects = await _select_count(async_engine, restore)

    assert selects <= 24, f"480 rollback rows issued {selects} SELECT statements"
    assert imported_series[0].status == ImportSeriesStatus.MATCHED
    assert imported_series[-1].status == ImportSeriesStatus.MATCHED
    assert imported_files[0].status == ImportedFileStatus.NO_MATCH
    assert imported_files[-1].status == ImportedFileStatus.NO_MATCH
    assert staged_arcs[0].status == ImportedStoryArcStatus.CONFIRMED
    assert staged_arcs[-1].status == ImportedStoryArcStatus.CONFIRMED

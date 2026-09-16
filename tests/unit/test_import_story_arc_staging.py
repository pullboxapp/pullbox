"""Story-arc import staging stays local, deterministic, and review-only."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, func, select

from pullbox.core.mylar3_reader import (
    Mylar3ArcSettingsSnapshot,
    Mylar3ArcSettingValue,
    Mylar3CollectionSnapshot,
    Mylar3StoryArcEntrySnapshot,
    Mylar3StoryArcSnapshot,
)
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArc,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services.import_story_arc_staging import (
    stage_folder_story_arcs,
    stage_mylar_story_arcs,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _mylar_entry(**overrides: object) -> Mylar3StoryArcEntrySnapshot:
    values: dict[str, object] = {
        "ordinal": 1,
        "reading_order": 2,
        "reading_order_raw": "002",
        "story_arc_id": "arc-local-1",
        "story_arc_name": "Synthetic Crossover",
        "cv_arc_id": "4045-12",
        "issue_arc_id": "arc-entry-1",
        "issue_id": "340001",
        "comic_id": "42721",
        "issue_number": "1000000",
        "comic_name": "Alpha Series",
        "series_year": "2024",
        "issue_year": "2024",
        "status": "Downloaded",
        "location": "/private/library/Alpha Series/Alpha 1000000.cbz",
        "release_date": "2024-02-03",
        "issue_date": "2024-02-01",
        "publisher": "Example Comics",
        "issue_publisher": "Example Comics",
        "issue_name": "An Exact Million",
        "manual": "added",
        "date_added": "2026-08-29",
        "digital_date": "2024-02-03",
        "issue_type": "Comic",
        "aliases": None,
        "total_issues": "3",
        "in_cache_dir": "False",
        "int_issue_number": "1000000",
        "dynamic_comic_name": None,
        "volume": None,
        "arc_image": "/private/cache/arc.jpg",
    }
    values.update(overrides)
    return Mylar3StoryArcEntrySnapshot(**values)  # type: ignore[arg-type]


def _mylar_snapshot(
    *,
    arcs: tuple[Mylar3StoryArcSnapshot, ...],
    readlist_count: int = 4,
) -> Mylar3CollectionSnapshot:
    return Mylar3CollectionSnapshot(
        series=(),
        story_arcs=arcs,
        storyarcs_present=True,
        readlist_present=True,
        readlist_count=readlist_count,
        arc_settings=Mylar3ArcSettingsSnapshot(
            present=True,
            parse_warnings=("unknown_value:ARC_FILEOPS",),
            values=(
                Mylar3ArcSettingValue(
                    key="STORYARCDIR",
                    section="StoryArc",
                    value=True,
                    raw_value="true",
                    used_default=False,
                ),
                Mylar3ArcSettingValue(
                    key="STORYARC_LOCATION",
                    section="StoryArc",
                    value="/private/story-arcs",
                    raw_value="/private/story-arcs",
                    used_default=False,
                ),
                Mylar3ArcSettingValue(
                    key="ARC_FILEOPS",
                    section="StoryArc",
                    value="teleport",
                    raw_value="teleport",
                    used_default=False,
                ),
            ),
        ),
    )


async def _add_job(
    session: AsyncSession,
    *,
    source_type: ImportSourceType,
) -> ImportJob:
    job = ImportJob(
        source_path="/private/source",
        source_type=source_type,
        status=ImportJobStatus.REVIEW,
    )
    session.add(job)
    await session.flush()
    return job


@pytest.mark.asyncio
async def test_mylar_staging_preserves_exact_gapped_duplicate_and_missing_entries(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session, source_type=ImportSourceType.MYLAR3)
    arc = Mylar3StoryArcSnapshot(
        story_arc_id="arc-local-1",
        cv_arc_id="4045-12",
        name="Synthetic Crossover",
        entries=(
            _mylar_entry(),
            _mylar_entry(
                ordinal=2,
                reading_order=7,
                reading_order_raw="007",
                issue_arc_id="arc-entry-2",
                issue_id="340007",
                comic_id="42722",
                issue_number="1AU",
                comic_name="Beta Series",
                location="/private/library/Beta Series/Beta 1AU.cbz",
            ),
            _mylar_entry(
                ordinal=3,
                reading_order=7,
                reading_order_raw="7",
                issue_arc_id="arc-entry-missing",
                issue_id=None,
                comic_id=None,
                issue_number="0.5",
                comic_name="Missing Series",
                status="Wanted",
                location=None,
            ),
        ),
    )

    result = await stage_mylar_story_arcs(
        db_session,
        import_job_id=job.id,
        snapshot=_mylar_snapshot(arcs=(arc,)),
        batch_size=1,
    )

    staged_arc = (
        await db_session.execute(select(ImportedStoryArc).order_by(ImportedStoryArc.id))
    ).scalar_one()
    entries = list(
        (
            await db_session.execute(
                select(ImportedStoryArcEntry).order_by(ImportedStoryArcEntry.source_ordinal)
            )
        )
        .scalars()
        .all()
    )
    assert result.arcs_staged == 1
    assert result.entries_staged == 3
    assert result.readlist_count == 4
    assert staged_arc.source_kind == StoryArcSourceKind.MYLAR3
    assert staged_arc.status == ImportedStoryArcStatus.NEEDS_REVIEW
    assert staged_arc.name == "Synthetic Crossover"
    assert len(staged_arc.source_key) <= 255
    assert staged_arc.source_key.startswith("mylar3:")
    assert staged_arc.source_settings_snapshot["readlist"] == {
        "present": True,
        "count": 4,
        "import_state": "deferred_v1.5.0",
    }
    assert staged_arc.source_settings_snapshot["values"]["STORYARC_LOCATION"]["value"] == (
        "/private/story-arcs"
    )
    assert staged_arc.proposed_policy_snapshot["activation"] == "requires_confirmation"
    assert staged_arc.proposed_policy_snapshot["placement_policy"] == {
        "schema_version": 1,
        "mode": "reference_only",
        "target_library_root_id": None,
        "destination_root": "/private/story-arcs",
        "folder_template": "{StoryArc}",
        "file_template": "{Series} {IssueNumber}{IssueTitleOptional}",
        "symlink_style": None,
        "synchronize": False,
    }
    assert "unknown_value:ARC_FILEOPS" in staged_arc.proposed_policy_snapshot["review_warnings"]
    assert staged_arc.proposed_policy_snapshot["confirmation"]["ready_for_activation"] is False
    assert staged_arc.diagnostics["duplicate_reading_order"] is True
    assert staged_arc.diagnostics["external_identities"] == [
        {
            "source": "comicvine",
            "namespace": "story_arc",
            "external_id": "4045-12",
        }
    ]
    assert [entry.reading_order for entry in entries] == [2, 7, 7]
    assert [entry.reading_order_raw for entry in entries] == ["002", "007", "7"]
    assert [entry.source_issue_number_text for entry in entries] == [
        "1000000",
        "1AU",
        "0.5",
    ]
    assert entries[2].resolution_state == StoryArcResolutionState.MISSING
    assert entries[2].source_issue_id is None
    assert entries[2].source_series_name == "Missing Series"
    assert entries[0].source_location == "/private/library/Alpha Series/Alpha 1000000.cbz"
    assert "/private/" not in json.dumps(staged_arc.diagnostics)
    assert all("/private/" not in json.dumps(entry.diagnostics) for entry in entries)

    assert await db_session.scalar(select(func.count()).select_from(StoryArc)) == 0
    assert await db_session.scalar(select(func.count()).select_from(IssueStoryArc)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Issue)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Series)) == 0


@pytest.mark.asyncio
async def test_mylar_staging_is_idempotent_and_keeps_stable_row_ids(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session, source_type=ImportSourceType.MYLAR3)
    snapshot = _mylar_snapshot(
        arcs=(
            Mylar3StoryArcSnapshot(
                story_arc_id="arc-local-1",
                cv_arc_id=None,
                name="Synthetic Crossover",
                entries=(_mylar_entry(), _mylar_entry(ordinal=2, issue_arc_id="entry-2")),
            ),
        )
    )

    first = await stage_mylar_story_arcs(
        db_session,
        import_job_id=job.id,
        snapshot=snapshot,
    )
    first_arc_id = await db_session.scalar(select(ImportedStoryArc.id))
    first_entry_ids = list(
        (
            await db_session.execute(
                select(ImportedStoryArcEntry.id).order_by(ImportedStoryArcEntry.source_ordinal)
            )
        )
        .scalars()
        .all()
    )
    staged_arc = (await db_session.execute(select(ImportedStoryArc))).scalar_one()
    first_entry = (
        await db_session.execute(
            select(ImportedStoryArcEntry).where(ImportedStoryArcEntry.id == first_entry_ids[0])
        )
    ).scalar_one()
    staged_arc.status = ImportedStoryArcStatus.CONFIRMED
    staged_arc.selected_for_import = True
    first_entry.selected_for_import = True
    await db_session.flush()

    second = await stage_mylar_story_arcs(
        db_session,
        import_job_id=job.id,
        snapshot=snapshot,
    )
    changed_snapshot = _mylar_snapshot(
        arcs=(
            Mylar3StoryArcSnapshot(
                story_arc_id="arc-local-1",
                cv_arc_id=None,
                name="Synthetic Crossover",
                entries=(
                    _mylar_entry(issue_number="1000000"),
                    _mylar_entry(
                        ordinal=2,
                        issue_arc_id="entry-2",
                        issue_number="1AU",
                    ),
                ),
            ),
        )
    )
    await stage_mylar_story_arcs(
        db_session,
        import_job_id=job.id,
        snapshot=changed_snapshot,
    )

    assert first == second
    assert await db_session.scalar(select(func.count()).select_from(ImportedStoryArc)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ImportedStoryArcEntry)) == 2
    assert await db_session.scalar(select(ImportedStoryArc.id)) == first_arc_id
    refreshed_arc = (await db_session.execute(select(ImportedStoryArc))).scalar_one()
    refreshed_entry = (
        await db_session.execute(
            select(ImportedStoryArcEntry).where(ImportedStoryArcEntry.id == first_entry_ids[0])
        )
    ).scalar_one()
    assert refreshed_arc.status == ImportedStoryArcStatus.CONFIRMED
    assert refreshed_arc.selected_for_import is True
    assert refreshed_entry.selected_for_import is True
    assert (
        list(
            (
                await db_session.execute(
                    select(ImportedStoryArcEntry.id).order_by(ImportedStoryArcEntry.source_ordinal)
                )
            )
            .scalars()
            .all()
        )
        == first_entry_ids
    )


@pytest.mark.asyncio
async def test_readlist_only_snapshot_reports_deferral_without_creating_fake_arc(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session, source_type=ImportSourceType.MYLAR3)

    result = await stage_mylar_story_arcs(
        db_session,
        import_job_id=job.id,
        snapshot=_mylar_snapshot(arcs=(), readlist_count=9),
    )

    assert result.arcs_staged == 0
    assert result.entries_staged == 0
    assert result.readlist_present is True
    assert result.readlist_count == 9
    assert await db_session.scalar(select(func.count()).select_from(ImportedStoryArc)) == 0


def _comicinfo_diagnostics(
    *,
    series: str | None,
    issue_number: str | None,
    arc: str | None,
    order: str | None,
    safety_blocked: bool = False,
) -> dict[str, object]:
    comicinfo: dict[str, object] = {}
    if series is not None:
        comicinfo["series"] = series
    if issue_number is not None:
        comicinfo["number"] = issue_number
    if arc is not None:
        comicinfo["story_arc"] = arc
    if order is not None:
        comicinfo["story_arc_number"] = order
    diagnostics: dict[str, object] = {
        "source_metadata": {
            "archive_member_evidence": {
                "member_index_scanned": True,
                "comicinfo": comicinfo,
            }
        }
    }
    if safety_blocked:
        diagnostics["safety_block"] = {
            "code": "archive_inspection_failed",
            "reason": "Could not inspect /private/library/secret.cbz",
        }
    return diagnostics


async def _add_folder_file(
    session: AsyncSession,
    *,
    job: ImportJob,
    imported_series: ImportedSeries,
    file_name: str,
    cohort_key: str,
    source_ordinal: int,
    series: str | None,
    issue_number: str | None,
    arc: str | None = None,
    order: str | None = None,
    safety_blocked: bool = False,
) -> ImportedFile:
    item = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path=f"/private/library/{cohort_key}/{file_name}",
        file_name=file_name,
        file_size=123,
        file_format="cbz",
        parsed_series=series,
        issue_number_raw=issue_number,
        source_folder_cohort_key=cohort_key,
        source_ordinal=source_ordinal,
        status=(
            ImportedFileStatus.SAFETY_BLOCKED if safety_blocked else ImportedFileStatus.PENDING
        ),
        diagnostics=_comicinfo_diagnostics(
            series=series,
            issue_number=issue_number,
            arc=arc,
            order=order,
            safety_blocked=safety_blocked,
        ),
    )
    session.add(item)
    await session.flush()
    return item


@pytest.mark.asyncio
async def test_folder_staging_streams_complete_split_cohort_and_keeps_safety_review(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session, source_type=ImportSourceType.FILESYSTEM)
    alpha = ImportedSeries(import_job_id=job.id, raw_series_name="Alpha", file_count=2)
    beta = ImportedSeries(import_job_id=job.id, raw_series_name="Beta", file_count=1)
    db_session.add_all([alpha, beta])
    await db_session.flush()
    first = await _add_folder_file(
        db_session,
        job=job,
        imported_series=alpha,
        file_name="Alpha 001.cbz",
        cohort_key="Publisher/Synthetic Crossover",
        source_ordinal=2,
        series="Alpha",
        issue_number="1000000",
        arc="Synthetic Crossover",
        order="002",
    )
    second = await _add_folder_file(
        db_session,
        job=job,
        imported_series=beta,
        file_name="Beta 001.cbz",
        cohort_key="Publisher/Synthetic Crossover",
        source_ordinal=7,
        series="Beta",
        issue_number="1AU",
        arc="Synthetic Crossover",
        order="007",
    )
    blocked = await _add_folder_file(
        db_session,
        job=job,
        imported_series=alpha,
        file_name="Gamma 001.cbz",
        cohort_key="Publisher/Synthetic Crossover",
        source_ordinal=8,
        series=None,
        issue_number="0.5",
        arc=None,
        order="007",
        safety_blocked=True,
    )

    checkpoints: list[int] = []

    async def cancellation_checkpoint() -> None:
        checkpoints.append(len(checkpoints) + 1)

    result = await stage_folder_story_arcs(
        db_session,
        import_job_id=job.id,
        cohort_batch_size=1,
        cancellation_check=cancellation_checkpoint,
    )

    staged_arc = (await db_session.execute(select(ImportedStoryArc))).scalar_one()
    entries = list(
        (
            await db_session.execute(
                select(ImportedStoryArcEntry).order_by(ImportedStoryArcEntry.source_ordinal)
            )
        )
        .scalars()
        .all()
    )
    assert result.arcs_staged == 1
    assert result.entries_staged == 3
    assert result.cohorts_examined == 1
    assert len(checkpoints) >= 2
    assert staged_arc.source_kind == StoryArcSourceKind.FOLDER
    assert staged_arc.status == ImportedStoryArcStatus.NEEDS_REVIEW
    assert staged_arc.name == "Synthetic Crossover"
    assert staged_arc.source_key.startswith("folder:")
    assert "Publisher/Synthetic Crossover" not in staged_arc.source_key
    assert [entry.import_file_id for entry in entries] == [first.id, second.id, blocked.id]
    assert [entry.reading_order for entry in entries] == [2, 7, 7]
    assert [entry.reading_order_raw for entry in entries] == ["002", "007", "007"]
    assert [entry.source_issue_number_text for entry in entries] == [
        "1000000",
        "1AU",
        "0.5",
    ]
    assert entries[2].source_series_name is None
    assert entries[2].resolution_state == StoryArcResolutionState.AMBIGUOUS
    assert entries[2].diagnostics["review_reason"] == "safety_incomplete"
    assert entries[2].source_location == blocked.file_path
    assert "/private/" not in json.dumps(staged_arc.diagnostics)
    assert all("/private/" not in json.dumps(entry.diagnostics) for entry in entries)
    assert await db_session.scalar(select(func.count()).select_from(StoryArc)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Series)) == 0


@pytest.mark.asyncio
async def test_folder_staging_uses_prefix_evidence_and_skips_ordinary_mixed_folder(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session, source_type=ImportSourceType.FILESYSTEM)
    series = ImportedSeries(import_job_id=job.id, raw_series_name="Split", file_count=4)
    db_session.add(series)
    await db_session.flush()
    await _add_folder_file(
        db_session,
        job=job,
        imported_series=series,
        file_name="001 - Alpha 001.cbz",
        cohort_key="Ordered Candidate",
        source_ordinal=1,
        series="Alpha",
        issue_number="1",
    )
    await _add_folder_file(
        db_session,
        job=job,
        imported_series=series,
        file_name="010 - Beta 001.cbz",
        cohort_key="Ordered Candidate",
        source_ordinal=2,
        series="Beta",
        issue_number="1",
    )
    await _add_folder_file(
        db_session,
        job=job,
        imported_series=series,
        file_name="Alpha 002.cbz",
        cohort_key="Ordinary Mixed",
        source_ordinal=3,
        series="Alpha",
        issue_number="2",
    )
    await _add_folder_file(
        db_session,
        job=job,
        imported_series=series,
        file_name="Beta 002.cbz",
        cohort_key="Ordinary Mixed",
        source_ordinal=4,
        series="Beta",
        issue_number="2",
    )

    result = await stage_folder_story_arcs(
        db_session,
        import_job_id=job.id,
        cohort_batch_size=1,
    )

    arcs = list(
        (await db_session.execute(select(ImportedStoryArc).order_by(ImportedStoryArc.id)))
        .scalars()
        .all()
    )
    entries = list(
        (
            await db_session.execute(
                select(ImportedStoryArcEntry).order_by(ImportedStoryArcEntry.source_ordinal)
            )
        )
        .scalars()
        .all()
    )
    assert result.cohorts_examined == 2
    assert result.arcs_staged == 1
    assert result.cohorts_skipped == 1
    assert len(arcs) == 1
    assert arcs[0].name == "Ordered Candidate"
    assert arcs[0].status == ImportedStoryArcStatus.NEEDS_REVIEW
    assert [entry.reading_order_raw for entry in entries] == ["001", "010"]


@pytest.mark.asyncio
async def test_staging_propagates_cancellation_between_bounded_cohorts(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session, source_type=ImportSourceType.FILESYSTEM)
    series = ImportedSeries(import_job_id=job.id, raw_series_name="Split", file_count=2)
    db_session.add(series)
    await db_session.flush()
    for source_ordinal, cohort_key in enumerate(("Arc A", "Arc B"), start=1):
        await _add_folder_file(
            db_session,
            job=job,
            imported_series=series,
            file_name=f"{source_ordinal:03d} - Alpha 001.cbz",
            cohort_key=cohort_key,
            source_ordinal=source_ordinal,
            series="Alpha",
            issue_number="1",
            arc=cohort_key,
            order=str(source_ordinal),
        )

    calls = 0

    async def cancel_after_first_cohort() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("synthetic cancellation")

    with pytest.raises(RuntimeError, match="synthetic cancellation"):
        await stage_folder_story_arcs(
            db_session,
            import_job_id=job.id,
            cohort_batch_size=1,
            cancellation_check=cancel_after_first_cohort,
        )

    assert calls == 3


@pytest.mark.asyncio
async def test_folder_staging_batches_cohort_queries_at_large_library_scale(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session, source_type=ImportSourceType.FILESYSTEM)
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Scale",
        file_count=205,
    )
    db_session.add(imported_series)
    await db_session.flush()
    for ordinal in range(1, 206):
        await _add_folder_file(
            db_session,
            job=job,
            imported_series=imported_series,
            file_name=f"Scale {ordinal:03d}.cbz",
            cohort_key=f"Publisher/Series {ordinal:03d}",
            source_ordinal=ordinal,
            series="Scale",
            issue_number=str(ordinal),
        )

    statements: list[str] = []

    def record_select(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = db_session.bind
    assert engine is not None
    event.listen(engine.sync_engine, "before_cursor_execute", record_select)
    try:
        result = await stage_folder_story_arcs(
            db_session,
            import_job_id=job.id,
            cohort_batch_size=100,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_select)

    assert result.cohorts_examined == 205
    assert result.cohorts_skipped == 205
    assert result.arcs_staged == 0
    cohort_pages = (205 + 100 - 1) // 100
    assert len(statements) <= (cohort_pages * 3) + 1


@pytest.mark.asyncio
async def test_mylar_staging_batches_arc_queries_at_large_library_scale(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session, source_type=ImportSourceType.MYLAR3)
    snapshot = _mylar_snapshot(
        arcs=tuple(
            Mylar3StoryArcSnapshot(
                story_arc_id=f"arc-{ordinal}",
                cv_arc_id=None,
                name=f"Synthetic Arc {ordinal}",
                entries=(
                    _mylar_entry(
                        story_arc_id=f"arc-{ordinal}",
                        issue_arc_id=f"entry-{ordinal}",
                        issue_id=str(340_000 + ordinal),
                    ),
                ),
            )
            for ordinal in range(1, 206)
        )
    )

    statements: list[str] = []

    def record_select(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = db_session.bind
    assert engine is not None
    event.listen(engine.sync_engine, "before_cursor_execute", record_select)
    try:
        result = await stage_mylar_story_arcs(
            db_session,
            import_job_id=job.id,
            snapshot=snapshot,
            batch_size=100,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_select)

    assert result.arcs_staged == 205
    assert result.entries_staged == 205
    arc_pages = (205 + 100 - 1) // 100
    assert len(statements) <= (arc_pages * 2) + 1

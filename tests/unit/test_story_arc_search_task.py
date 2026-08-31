"""Arc acquisition uses canonical issue targets without widening to parent series."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.pending_match import PendingMatch
from pullbox.models.search_log import SearchLog
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcResolutionState

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed(session: AsyncSession) -> tuple[StoryArc, list[Issue]]:
    series = [
        Series(title=f"Arc parent {n}", sort_title=f"arc parent {n}", monitored=False)
        for n in range(2)
    ]
    arc = StoryArc(name="Scoped arc", monitored=False)
    root = LibraryRoot(name="Protected root", path="/comics")
    session.add_all([*series, arc, root])
    await session.flush()
    issues = [
        Issue(series_id=series[n % 2].id, issue_number=n + 1, status=IssueStatus.SKIPPED)
        for n in range(10)
    ]
    session.add_all(issues)
    await session.flush()
    session.add_all(
        [
            IssueStoryArc(
                story_arc_id=arc.id,
                issue_id=issue.id,
                sequence_number=n + 1,
                source_ordinal=n + 1,
                resolution_state=StoryArcResolutionState.RESOLVED,
            )
            for n, issue in enumerate(issues[:9])
        ]
    )
    issues[1].manual_skip = True
    issues[2].status = IssueStatus.OWNED
    session.add(
        LibraryFile(
            issue_id=issues[3].id,
            library_root_id=root.id,
            file_path="/comics/protected.cbz",
            file_name="protected.cbz",
            file_size=10,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
        )
    )
    session.add(
        PendingMatch(
            issue_id=issues[4].id,
            release_title="Review",
            download_url="https://example.test/review",
            confidence="medium",
        )
    )
    session.add(
        DownloadHistory(
            issue_id=issues[5].id,
            title="Active",
            download_url="https://example.test/active",
            download_client=DownloadClientType.SABNZBD,
            protocol=AcquisitionProtocol.USENET,
            state=DownloadState.COMPLETED,
        )
    )
    issues[6].release_date = date.today() + timedelta(days=30)
    issues[7].issue_number = 1000000
    issues[7].issue_number_text = "1000000"
    await session.flush()
    unresolved = await session.scalar(
        select(IssueStoryArc).where(IssueStoryArc.issue_id == issues[8].id)
    )
    assert unresolved is not None
    unresolved.resolution_state = StoryArcResolutionState.PENDING
    await session.commit()
    return arc, issues


async def test_arc_targets_keep_manual_scope_without_mutating_monitoring(
    db_session: AsyncSession,
) -> None:
    from pullbox.services.story_arc_search_targets import load_story_arc_missing_search_targets

    arc, issues = await _seed(db_session)
    targets = await load_story_arc_missing_search_targets(db_session, arc.id)
    assert [target.issue_id for target in targets] == [issues[0].id, issues[7].id]
    assert targets[1].effective_issue_number_text == "1000000"
    assert arc.monitored is False
    assert all(not series.monitored for series in (await db_session.scalars(select(Series))).all())
    assert issues[0].status is IssueStatus.SKIPPED

    arc.include_upcoming = True
    await db_session.flush()
    first = await load_story_arc_missing_search_targets(db_session, arc.id, limit=1)
    rest = await load_story_arc_missing_search_targets(
        db_session, arc.id, after_issue_id=first[0].issue_id, limit=1
    )
    assert rest[0].issue_id == issues[6].id
    scoped = await load_story_arc_missing_search_targets(
        db_session, arc.id, issue_ids=[issues[7].id, issues[9].id], series_id=issues[7].series_id
    )
    assert [target.issue_id for target in scoped] == [issues[7].id]


async def test_arc_batch_search_reuses_series_runner_with_exact_members(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pullbox.tasks import story_arc_search_task

    arc, issues = await _seed(db_session)
    runner = AsyncMock(return_value={"wanted": 1, "sent": 0, "queued": 1})
    monkeypatch.setattr(
        story_arc_search_task,
        "get_session_factory",
        lambda: async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    monkeypatch.setattr(story_arc_search_task, "search_series_issues", runner)
    result = await story_arc_search_task.search_story_arc_issues(arc.id)
    assert result == {"wanted": 2, "sent": 0, "queued": 2}
    assert runner.await_count == 2
    searched = [
        issue_id for call in runner.await_args_list for issue_id in call.kwargs["issue_ids"]
    ]
    assert sorted(searched) == [issues[0].id, issues[7].id]
    assert all(call.kwargs["story_arc_id"] == arc.id for call in runner.await_args_list)


async def test_issues_released_today_are_missing_not_upcoming(db_session: AsyncSession) -> None:
    from pullbox.services.story_arc_search_targets import load_story_arc_missing_search_targets

    arc, issues = await _seed(db_session)
    issues[0].release_date = date.today()
    issues[7].store_date = date.today()
    await db_session.flush()
    targets = await load_story_arc_missing_search_targets(db_session, arc.id)
    assert [target.issue_id for target in targets] == [issues[0].id, issues[7].id]


async def test_store_date_takes_precedence_over_a_future_cover_date(
    db_session: AsyncSession,
) -> None:
    from pullbox.services.story_arc_search_targets import load_story_arc_missing_search_targets

    arc, issues = await _seed(db_session)
    issues[0].release_date = date.today() + timedelta(days=60)
    issues[0].store_date = date.today() - timedelta(days=1)
    await db_session.flush()
    targets = await load_story_arc_missing_search_targets(db_session, arc.id)
    assert [target.issue_id for target in targets] == [issues[0].id, issues[7].id]


@pytest.mark.parametrize("skip_during_search", [False, True])
async def test_arc_scoped_runner_never_uses_unrestricted_series_loader(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, skip_during_search: bool
) -> None:
    from pullbox.services.search_service import IssueSearchOutcome, SearchRuntime
    from pullbox.tasks import search_task

    arc, issues = await _seed(db_session)
    monkeypatch.setattr(
        search_task,
        "get_session_factory",
        lambda: async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    unrestricted = AsyncMock(side_effect=AssertionError("must not search parent catalog"))
    monkeypatch.setattr(search_task, "load_series_wanted_search_targets", unrestricted)
    runtime = SearchRuntime(
        registry=MagicMock(),
        indexer_configs={},
        source_priority=None,
        eval_kwargs={},
        validator_kwargs={},
        type_thresholds=search_task.DEFAULT_TYPE_THRESHOLDS.copy(),
        failure_threshold=3,
    )
    monkeypatch.setattr(search_task, "_build_task_search_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr(search_task, "_is_mocked_search_service", lambda _service: True)
    monkeypatch.setattr(search_task, "_load_mocked_two_pass_enabled", AsyncMock(return_value=False))

    async def outcome(_session, _service, target, _runtime):
        if skip_during_search:
            issue = await _session.get(Issue, target.issue_id)
            issue.manual_skip = True
            await _session.commit()
        return IssueSearchOutcome(
            target=target,
            mode="fast",
            query_count=1,
            raw_results=[],
            filtered_results=[],
            matched=[],
            rejected=[],
            best_release=None,
            best_validation=None,
            search_details={},
            elapsed_ms=0,
        )

    mocked_outcome = AsyncMock(side_effect=outcome)
    monkeypatch.setattr(search_task, "_build_mocked_issue_outcome", mocked_outcome)
    persist = AsyncMock(return_value=(0, 0, 0))
    monkeypatch.setattr(search_task, "_persist_series_search_outcome", persist)
    result = await search_task.search_series_issues(
        issues[7].series_id, story_arc_id=arc.id, issue_ids=[issues[7].id, issues[9].id]
    )
    assert result["wanted"] == 1
    unrestricted.assert_not_awaited()
    assert mocked_outcome.await_args.args[2].issue_id == issues[7].id
    if skip_during_search:
        persist.assert_not_awaited()
    else:
        assert persist.await_args.kwargs["primary_outcome"].target.issue_id == issues[7].id
    logs = (await db_session.scalars(select(SearchLog))).all()
    assert len(logs) == 1
    assert logs[0].details["story_arc_id"] == arc.id
    assert logs[0].details["search_scope"] == "story_arc"
    if skip_during_search:
        assert logs[0].details["action_status"] == "no_longer_eligible"


async def test_arc_search_pages_are_bounded_and_do_not_chase_new_issues(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pullbox.tasks import story_arc_search_task

    arc, original_issues = await _seed(db_session)
    series_id = original_issues[0].series_id
    members = [Issue(series_id=series_id, issue_number=n + 20) for n in range(123)]
    db_session.add_all(members)
    await db_session.flush()
    db_session.add_all(
        IssueStoryArc(
            story_arc_id=arc.id,
            issue_id=issue.id,
            sequence_number=n + 20,
            source_ordinal=n + 20,
            resolution_state=StoryArcResolutionState.RESOLVED,
        )
        for n, issue in enumerate(members)
    )
    await db_session.commit()
    searched: list[int] = []
    page_sizes: list[int] = []

    async def run(_series_id: int, *, story_arc_id: int, issue_ids: list[int]):
        assert story_arc_id == arc.id
        page_sizes.append(len(issue_ids))
        if not searched:
            late = Issue(series_id=series_id, issue_number=999)
            db_session.add(late)
            await db_session.flush()
            db_session.add(
                IssueStoryArc(
                    story_arc_id=arc.id,
                    issue_id=late.id,
                    sequence_number=999,
                    source_ordinal=999,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                )
            )
            await db_session.commit()
        searched.extend(issue_ids)
        return {"wanted": len(issue_ids), "sent": 0, "queued": 0}

    monkeypatch.setattr(
        story_arc_search_task,
        "get_session_factory",
        lambda: async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    monkeypatch.setattr(story_arc_search_task, "search_series_issues", run)
    result = await story_arc_search_task.search_story_arc_issues(arc.id)
    expected = [original_issues[0].id, original_issues[7].id, *(issue.id for issue in members)]
    assert sorted(searched) == expected
    assert result == {"wanted": 125, "sent": 0, "queued": 0}
    assert max(page_sizes) <= 50


async def test_arc_eligibility_is_rechecked_after_airdcpp_wait(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pullbox.services.search_service import IssueSearchOutcome, SearchRuntime
    from pullbox.services.story_arc_search_targets import load_story_arc_missing_search_targets
    from pullbox.tasks import search_task

    arc, issues = await _seed(db_session)
    target = (await load_story_arc_missing_search_targets(db_session, arc.id))[0]
    log_ids = await search_task._ensure_pending_series_search_logs(
        db_session,
        [target],
        series_id=target.series_id,
        existing_log_ids_by_issue={},
        story_arc_id=arc.id,
    )
    outcome = IssueSearchOutcome(
        target=target,
        mode="fast",
        query_count=1,
        raw_results=[],
        filtered_results=[],
        matched=[],
        rejected=[],
        best_release=None,
        best_validation=None,
        search_details={},
        elapsed_ms=0,
    )
    runtime = SearchRuntime(
        registry=MagicMock(),
        indexer_configs={},
        source_priority=None,
        eval_kwargs={},
        validator_kwargs={},
        type_thresholds=search_task.DEFAULT_TYPE_THRESHOLDS.copy(),
        failure_threshold=3,
    )

    async def dc_wait(session, current, **_kwargs):
        issues[0].manual_skip = True
        await session.commit()
        return current

    route = AsyncMock()
    monkeypatch.setattr(search_task, "attach_automatic_airdcpp_search", dc_wait)
    monkeypatch.setattr(search_task, "route_search_acquisition", route)
    result = await search_task._persist_series_search_outcome(
        db_session,
        log=MagicMock(),
        primary_outcome=outcome,
        fallback_outcome=None,
        pending_log_id=log_ids[target.issue_id],
        runtime=runtime,
        download_svc=MagicMock(),
        intervention_svc=MagicMock(),
        story_arc_id=arc.id,
    )
    assert result == (0, 0, 0)
    route.assert_not_awaited()
    search_log = await db_session.get(SearchLog, log_ids[target.issue_id])
    assert search_log.details["action_status"] == "no_longer_eligible"


async def test_repeated_arc_search_launch_is_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    from pullbox.tasks import story_arc_search_task

    gate = asyncio.Event()

    async def wait(_arc_id: int) -> dict[str, int]:
        await gate.wait()
        return {"wanted": 0, "sent": 0, "queued": 0}

    monkeypatch.setattr(story_arc_search_task, "search_story_arc_issues", wait)
    assert story_arc_search_task.schedule_story_arc_search(1234) is True
    assert story_arc_search_task.schedule_story_arc_search(1234) is False
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert story_arc_search_task.schedule_story_arc_search(1234) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)

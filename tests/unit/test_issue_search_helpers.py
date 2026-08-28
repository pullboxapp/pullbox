"""Focused coverage for shared issue-search helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import joinedload

from pullbox.api.v1 import issues as issues_api
from pullbox.core.exceptions import ConfigurationError, NotFoundError, ProviderError
from pullbox.models import Base
from pullbox.models.download import DownloadState
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.base import ProviderRegistry, ReleaseResult
from pullbox.schemas.issue import IssueUpdate, ManualFileImportRequest
from pullbox.schemas.search import GrabReleaseRequest, MatchDetails, SearchResultItem
from pullbox.services.issue_import_service import ManualIssueImportError
from pullbox.services.search_service import IssueSearchOutcome, IssueSearchTarget, SearchRuntime

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _make_release(title: str = "Absolute Superman 009") -> ReleaseResult:
    return ReleaseResult(
        title=title,
        indexer_name="NZBgeek",
        download_url=f"https://example.com/{title.replace(' ', '_')}",
        size_bytes=100_000_000,
        age_days=3,
        seeders=None,
        leechers=None,
        grabs=25,
        is_torrent=False,
        category="7030",
        published_at=None,
    )


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _create_issue(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        series = Series(
            comicvine_id=801,
            title="Absolute Superman",
            sort_title="absolute superman",
            year_start=2025,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        session.add(series)
        await session.flush()
        issue = Issue(
            series_id=series.id,
            comicvine_id=9001,
            issue_number=9.0,
            title="Issue #9",
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        session.add(issue)
        await session.commit()
        return issue.id


@pytest.mark.asyncio
async def test_run_issue_search_handles_not_found_and_no_runtime(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_factory() as session:
        with pytest.raises(issues_api.NotFoundError):
            await issues_api._run_issue_search(session, 99999, include_download_clients=False)

    issue_id = await _create_issue(db_factory)
    monkeypatch.setattr(issues_api, "build_search_runtime", AsyncMock(return_value=None))

    async with db_factory() as session:
        bundle = await issues_api._run_issue_search(
            session,
            issue_id,
            include_download_clients=False,
        )

    assert bundle.runtime is None
    assert bundle.outcome is None
    assert bundle.matched_items == []
    assert bundle.rejected_items == []


@pytest.mark.asyncio
async def test_run_issue_search_returns_shared_bundle_and_log(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue_id = await _create_issue(db_factory)
    target = IssueSearchTarget(
        issue_id=issue_id,
        series_id=51,
        series_title="Absolute Superman",
        issue_number=9.0,
        issue_type=IssueType.ISSUE,
        issue_title="Issue #9",
        series_year=2025,
    )
    release = _make_release()
    validation = SimpleNamespace(release=release, confidence=MatchConfidence.HIGH)
    outcome = IssueSearchOutcome(
        target=target,
        mode="deep",
        query_count=2,
        raw_results=[release],
        filtered_results=[release],
        matched=[validation],
        rejected=[],
        best_release=release,
        best_validation=validation,
        search_details={"results_count": 1, "query_count": 2},
        elapsed_ms=12,
        used_fallback=True,
    )
    runtime = SearchRuntime(
        registry=ProviderRegistry(),
        indexer_configs={},
        source_priority=["NZBgeek"],
        eval_kwargs={},
        validator_kwargs={},
        type_thresholds={"issue": "high"},
        failure_threshold=3,
    )
    matched_items = [SimpleNamespace(title=release.title)]
    rejected_items = [SimpleNamespace(title="Rejected release")]

    monkeypatch.setattr(issues_api, "load_issue_search_target", AsyncMock(return_value=target))
    monkeypatch.setattr(issues_api, "build_search_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr(
        issues_api.SearchService,
        "search_issue_target",
        AsyncMock(return_value=outcome),
    )
    monkeypatch.setattr(
        issues_api,
        "build_interactive_results",
        lambda matched, rejected, eval_kwargs, **kwargs: (
            matched_items,
            rejected_items,
        ),
    )

    async with db_factory() as session:
        bundle = await issues_api._run_issue_search(
            session,
            issue_id,
            include_download_clients=True,
        )

    assert bundle.runtime is runtime
    assert bundle.outcome is outcome
    assert bundle.matched_items == matched_items
    assert bundle.rejected_items == rejected_items

    search_log = issues_api._build_issue_search_log(bundle)
    assert search_log.search_type == SearchType.MANUAL
    assert search_log.results_found == 1
    assert search_log.results_rejected == 1
    assert search_log.best_confidence == MatchConfidence.HIGH.value

    empty_log = issues_api._build_issue_search_log(
        issues_api._IssueSearchBundle(
            target=target,
            issue=bundle.issue,
            runtime=None,
            outcome=None,
            matched_items=[],
            rejected_items=[],
            search_time_ms=0,
        )
    )
    assert empty_log.results_found == 0
    assert empty_log.details["validated_count"] == 0


@pytest.mark.asyncio
async def test_run_issue_search_releases_transactions_between_manual_search_passes(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue_id = await _create_issue(db_factory)
    target = IssueSearchTarget(
        issue_id=issue_id,
        series_id=51,
        series_title="Absolute Superman",
        issue_number=9.0,
        issue_type=IssueType.ISSUE,
        issue_title="Issue #9",
        series_year=2025,
    )
    release = _make_release()
    validation = SimpleNamespace(release=release, confidence=MatchConfidence.HIGH)
    fast_outcome = IssueSearchOutcome(
        target=target,
        mode="fast",
        query_count=1,
        raw_results=[],
        filtered_results=[],
        matched=[],
        rejected=[],
        best_release=None,
        best_validation=None,
        search_details={"search_mode": "fast"},
        elapsed_ms=10,
    )
    runtime = SearchRuntime(
        registry=ProviderRegistry(),
        indexer_configs={},
        source_priority=None,
        eval_kwargs={},
        validator_kwargs={},
        type_thresholds={"issue": "high"},
        failure_threshold=3,
    )
    transaction_open_when_search_started: list[bool] = []

    async def search_issue_target(
        _self,
        session: AsyncSession,
        _target: IssueSearchTarget,
        *,
        mode: str,
        **_kwargs,
    ) -> IssueSearchOutcome:
        transaction_open_when_search_started.append(session.in_transaction())
        await session.execute(select(Issue.id).limit(1))
        if mode == "fast":
            return fast_outcome
        return IssueSearchOutcome(
            target=target,
            mode="deep",
            query_count=1,
            raw_results=[release],
            filtered_results=[release],
            matched=[validation],
            rejected=[],
            best_release=release,
            best_validation=validation,
            search_details={"search_mode": "deep"},
            elapsed_ms=12,
        )

    monkeypatch.setattr(issues_api, "load_issue_search_target", AsyncMock(return_value=target))
    monkeypatch.setattr(issues_api, "build_search_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr(
        issues_api.SearchService,
        "search_issue_target",
        search_issue_target,
    )
    monkeypatch.setattr(
        issues_api,
        "build_interactive_results",
        lambda matched, rejected, eval_kwargs, **kwargs: (
            [SimpleNamespace(title=release.title)],
            [],
        ),
    )

    async with db_factory() as session:
        await issues_api._run_issue_search(
            session,
            issue_id,
            include_download_clients=False,
        )

    assert transaction_open_when_search_started == [False, False]


@pytest.mark.asyncio
async def test_run_issue_search_uses_fast_manual_search_when_it_finds_matches(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue_id = await _create_issue(db_factory)
    target = IssueSearchTarget(
        issue_id=issue_id,
        series_id=51,
        series_title="Absolute Superman",
        issue_number=9.0,
        issue_type=IssueType.ISSUE,
        issue_title="Issue #9",
        series_year=2025,
    )
    release = _make_release()
    validation = SimpleNamespace(release=release, confidence=MatchConfidence.HIGH)
    fast_outcome = IssueSearchOutcome(
        target=target,
        mode="fast",
        query_count=5,
        raw_results=[release],
        filtered_results=[release],
        matched=[validation],
        rejected=[],
        best_release=release,
        best_validation=validation,
        search_details={"search_mode": "fast"},
        elapsed_ms=8,
    )
    runtime = SearchRuntime(
        registry=ProviderRegistry(),
        indexer_configs={},
        source_priority=None,
        eval_kwargs={},
        validator_kwargs={},
        type_thresholds={"issue": "high"},
        failure_threshold=3,
    )
    search_mock = AsyncMock(return_value=fast_outcome)

    monkeypatch.setattr(issues_api, "load_issue_search_target", AsyncMock(return_value=target))
    monkeypatch.setattr(issues_api, "build_search_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr(issues_api.SearchService, "search_issue_target", search_mock)
    monkeypatch.setattr(
        issues_api,
        "build_interactive_results",
        lambda matched, rejected, eval_kwargs, **kwargs: (
            [SimpleNamespace(title=release.title)],
            [],
        ),
    )

    async with db_factory() as session:
        bundle = await issues_api._run_issue_search(
            session,
            issue_id,
            include_download_clients=False,
        )

    assert bundle.outcome is fast_outcome
    assert search_mock.await_count == 1
    assert search_mock.await_args.kwargs["mode"] == "fast"
    assert bundle.outcome.search_details["manual_search_strategy"] == "quick_first"


@pytest.mark.asyncio
async def test_run_issue_search_falls_back_to_deep_when_fast_has_no_matches(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue_id = await _create_issue(db_factory)
    target = IssueSearchTarget(
        issue_id=issue_id,
        series_id=51,
        series_title="Absolute Superman",
        issue_number=9.0,
        issue_type=IssueType.ISSUE,
        issue_title="Issue #9",
        series_year=2025,
    )
    release = _make_release()
    validation = SimpleNamespace(release=release, confidence=MatchConfidence.HIGH)
    fast_outcome = IssueSearchOutcome(
        target=target,
        mode="fast",
        query_count=5,
        raw_results=[],
        filtered_results=[],
        matched=[],
        rejected=[],
        best_release=None,
        best_validation=None,
        search_details={"search_mode": "fast", "query_count": 5},
        elapsed_ms=7,
    )
    deep_outcome = IssueSearchOutcome(
        target=target,
        mode="deep",
        query_count=6,
        raw_results=[release],
        filtered_results=[release],
        matched=[validation],
        rejected=[],
        best_release=release,
        best_validation=validation,
        search_details={"search_mode": "deep", "query_count": 6},
        elapsed_ms=22,
    )
    runtime = SearchRuntime(
        registry=ProviderRegistry(),
        indexer_configs={},
        source_priority=None,
        eval_kwargs={},
        validator_kwargs={},
        type_thresholds={"issue": "high"},
        failure_threshold=3,
    )
    search_mock = AsyncMock(side_effect=[fast_outcome, deep_outcome])

    monkeypatch.setattr(issues_api, "load_issue_search_target", AsyncMock(return_value=target))
    monkeypatch.setattr(issues_api, "build_search_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr(issues_api.SearchService, "search_issue_target", search_mock)
    monkeypatch.setattr(
        issues_api,
        "build_interactive_results",
        lambda matched, rejected, eval_kwargs, **kwargs: (
            [SimpleNamespace(title=release.title)],
            [],
        ),
    )

    async with db_factory() as session:
        bundle = await issues_api._run_issue_search(
            session,
            issue_id,
            include_download_clients=False,
        )

    assert bundle.outcome is deep_outcome
    assert search_mock.await_count == 2
    assert [call.kwargs["mode"] for call in search_mock.await_args_list] == ["fast", "deep"]
    assert bundle.outcome.search_details["manual_search_strategy"] == "quick_first_deep_fallback"
    assert bundle.outcome.search_details["fast_search"] == {
        "query_count": 5,
        "results_count": 0,
        "matched_count": 0,
        "rejected_count": 0,
        "elapsed_ms": 7,
    }


def _search_result_item(title: str = "Absolute Superman 009") -> SearchResultItem:
    return SearchResultItem(
        title=title,
        indexer_name="NZBgeek",
        download_url="https://example.com/release.nzb",
        info_url=None,
        size_bytes=100_000_000,
        age_days=1,
        seeders=None,
        leechers=None,
        is_torrent=False,
        category="7030",
        confidence="high",
        quality_score=98.0,
        auto_grabbable=True,
        match_details=MatchDetails(
            parsed_series="Absolute Superman",
            parsed_issue=9.0,
            parsed_year=2025,
            series_similarity=1.0,
            match_type="exact",
        ),
    )


def _runtime() -> SearchRuntime:
    return SearchRuntime(
        registry=ProviderRegistry(),
        indexer_configs={},
        source_priority=None,
        eval_kwargs={},
        validator_kwargs={},
        type_thresholds={"issue": "high"},
        failure_threshold=3,
    )


def _bundle(
    issue_id: int,
    *,
    runtime: SearchRuntime | None,
    outcome: IssueSearchOutcome | None,
    matched_items: list[SearchResultItem] | None = None,
) -> issues_api._IssueSearchBundle:
    target = IssueSearchTarget(
        issue_id=issue_id,
        series_id=51,
        series_title="Absolute Superman",
        issue_number=9.0,
        issue_type=IssueType.ISSUE,
        issue_title="Issue #9",
        series_year=2025,
    )
    return issues_api._IssueSearchBundle(
        target=target,
        issue=issues_api._build_issue_context(target),
        runtime=runtime,
        outcome=outcome,
        matched_items=matched_items or [],
        rejected_items=[],
        search_time_ms=12,
    )


def _outcome(
    target: IssueSearchTarget,
    *,
    release: ReleaseResult | None = None,
    confidence: MatchConfidence = MatchConfidence.HIGH,
) -> IssueSearchOutcome:
    best_release = release or _make_release()
    validation = SimpleNamespace(release=best_release, confidence=confidence)
    return IssueSearchOutcome(
        target=target,
        mode="fast",
        query_count=1,
        raw_results=[best_release],
        filtered_results=[best_release],
        matched=[validation],
        rejected=[],
        best_release=best_release,
        best_validation=validation,
        search_details={"results_count": 1},
        elapsed_ms=12,
    )


@pytest.mark.asyncio
async def test_issue_response_and_search_log_grab_helpers(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        root = LibraryRoot(name="Root", path="/library", enabled=True)
        series = Series(
            comicvine_id=801,
            title="Absolute Superman",
            sort_title="absolute superman",
            year_start=2025,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
        )
        session.add_all([root, series])
        await session.flush()
        issue = Issue(
            series_id=series.id,
            comicvine_id=9001,
            issue_number=9.0,
            title="Issue #9",
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        session.add(issue)
        await session.flush()
        library_file = LibraryFile(
            issue_id=issue.id,
            library_root_id=root.id,
            file_path="/library/Absolute Superman 009.cbz",
            file_name="Absolute Superman 009.cbz",
            file_size=10,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
        )
        session.add(library_file)
        await session.flush()

        response = await issues_api._load_issue_response(session, issue.id)
        assert response.series_title == "Absolute Superman"
        assert response.has_file is True
        assert (await issues_api.get_issue(issue.id, object(), session)).id == issue.id
        updated = await issues_api.update_issue(
            issue.id,
            IssueUpdate(status=IssueStatus.SKIPPED),
            object(),
            session,
        )
        assert updated.status == IssueStatus.SKIPPED
        with pytest.raises(NotFoundError):
            await issues_api._load_issue_response(session, 99999)

        await issues_api._increment_search_log_grabbed(
            session,
            issue_id=issue.id,
            search_log_id=None,
        )
        await issues_api._increment_search_log_grabbed(
            session,
            issue_id=issue.id,
            search_log_id=99999,
        )
        other_issue = Issue(
            series_id=series.id,
            issue_number=10.0,
            title="Issue #10",
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        session.add(other_issue)
        await session.flush()
        mismatched_log = SearchLog(
            issue_id=other_issue.id,
            series_title="Other",
            issue_number=1,
            search_type=SearchType.MANUAL,
        )
        search_log = SearchLog(
            issue_id=issue.id,
            series_title=series.title,
            issue_number=issue.issue_number,
            search_type=SearchType.MANUAL,
            results_grabbed=2,
        )
        session.add_all([mismatched_log, search_log])
        await session.flush()

        await issues_api._increment_search_log_grabbed(
            session,
            issue_id=issue.id,
            search_log_id=mismatched_log.id,
        )
        await issues_api._increment_search_log_grabbed(
            session,
            issue_id=issue.id,
            search_log_id=search_log.id,
        )
        assert search_log.results_grabbed == 3


@pytest.mark.asyncio
async def test_issue_search_routes_log_no_runtime_and_runtime_results(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue_id = await _create_issue(db_factory)
    release = _make_release()
    target = IssueSearchTarget(
        issue_id=issue_id,
        series_id=51,
        series_title="Absolute Superman",
        issue_number=9.0,
        issue_type=IssueType.ISSUE,
        issue_title="Issue #9",
        series_year=2025,
    )
    outcome = _outcome(target, release=release)
    no_runtime = _bundle(issue_id, runtime=None, outcome=None)
    runtime_bundle = _bundle(
        issue_id,
        runtime=_runtime(),
        outcome=outcome,
        matched_items=[_search_result_item(release.title)],
    )
    search_mock = AsyncMock(side_effect=[no_runtime, no_runtime, runtime_bundle, runtime_bundle])
    monkeypatch.setattr(issues_api, "_run_issue_search", search_mock)

    async with db_factory() as session:
        no_indexers = await issues_api.search_issue(issue_id, object(), session)
        assert no_indexers == {
            "issue_id": issue_id,
            "results": [],
            "error": "no indexers configured",
        }

        interactive_empty = await issues_api.get_search_results(issue_id, object(), session)
        assert interactive_empty.search_log_id is None
        assert interactive_empty.matched == []

        manual = await issues_api.search_issue(issue_id, object(), session)
        assert manual["results"][0]["title"] == release.title

        interactive = await issues_api.get_search_results(issue_id, object(), session)
        assert interactive.search_log_id is not None
        assert interactive.matched[0].title == release.title


@pytest.mark.asyncio
async def test_grab_release_direct_error_and_success_branches(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue_id = await _create_issue(db_factory)
    body = GrabReleaseRequest(
        download_url="https://example.com/release.nzb",
        title="Absolute Superman 009",
        indexer_name="NZBgeek",
        file_size=100,
    )

    async with db_factory() as session:
        with pytest.raises(NotFoundError):
            await issues_api.grab_release(99999, body, object(), session)

    monkeypatch.setattr(
        "pullbox.composition.services.build_domain_download_service",
        AsyncMock(return_value=None),
    )
    async with db_factory() as session:
        with pytest.raises(ProviderError, match="No download clients"):
            await issues_api.grab_release(issue_id, body, object(), session)

    failed_download = SimpleNamespace(
        id=7,
        state=DownloadState.FAILED,
        error_message="already in client",
    )
    failed_service = SimpleNamespace(grab_release=AsyncMock(return_value=failed_download))
    monkeypatch.setattr(
        "pullbox.composition.services.build_domain_download_service",
        AsyncMock(return_value=(failed_service, {})),
    )
    async with db_factory() as session:
        with pytest.raises(HTTPException) as exc_info:
            await issues_api.grab_release(issue_id, body, object(), session)
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail == "already in client"

    success_download = SimpleNamespace(id=8, state=DownloadState.DOWNLOADING)
    success_service = SimpleNamespace(grab_release=AsyncMock(return_value=success_download))
    monkeypatch.setattr(
        "pullbox.composition.services.build_domain_download_service",
        AsyncMock(return_value=(success_service, {})),
    )
    async with db_factory() as session:
        search_log = SearchLog(
            issue_id=issue_id,
            series_title="Absolute Superman",
            issue_number=9,
            search_type=SearchType.MANUAL,
            results_grabbed=0,
        )
        session.add(search_log)
        await session.flush()
        body.search_log_id = search_log.id
        response = await issues_api.grab_release(issue_id, body, object(), session)
        assert response.download_id == 8
        assert response.status == DownloadState.DOWNLOADING.value
        assert search_log.results_grabbed == 1


@pytest.mark.asyncio
async def test_download_issue_direct_status_branches(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue_id = await _create_issue(db_factory)
    target = IssueSearchTarget(
        issue_id=issue_id,
        series_id=51,
        series_title="Absolute Superman",
        issue_number=9.0,
        issue_type=IssueType.ISSUE,
        issue_title="Issue #9",
        series_year=2025,
    )
    release = _make_release()
    runtime = _runtime()
    no_runtime = _bundle(issue_id, runtime=None, outcome=None)
    no_results = _bundle(issue_id, runtime=runtime, outcome=None)
    matched = _bundle(
        issue_id,
        runtime=runtime,
        outcome=_outcome(target, release=release),
        matched_items=[_search_result_item(release.title)],
    )

    monkeypatch.setattr(
        "pullbox.composition.services.build_download_service",
        lambda _registry: SimpleNamespace(
            send_to_client=AsyncMock(return_value=SimpleNamespace(id=22))
        ),
    )

    search_mock = AsyncMock(side_effect=[no_runtime, no_results, matched, matched, matched])
    monkeypatch.setattr(issues_api, "_run_issue_search", search_mock)

    async with db_factory() as session:
        with pytest.raises(NotFoundError):
            await issues_api.download_issue(99999, object(), session)

        no_clients = await issues_api.download_issue(issue_id, object(), session)
        assert no_clients["status"] == "no_clients"

        no_result = await issues_api.download_issue(issue_id, object(), session)
        assert no_result["status"] == "no_results"

        monkeypatch.setattr(
            "pullbox.services.search_acquisition_router.should_auto_grab",
            lambda *_args: True,
        )
        downloading = await issues_api.download_issue(issue_id, object(), session)
        assert downloading["status"] == "downloading"
        assert downloading["download_id"] == 22

        monkeypatch.setattr(
            "pullbox.services.search_acquisition_router.should_auto_grab",
            lambda *_args: False,
        )

        class FakeInterventionService:
            pending = True

            def __init__(self, download_service: object) -> None:
                self.download_service = download_service

            async def has_pending_for_issue(self, _session: AsyncSession, _issue_id: int) -> bool:
                return self.pending

            async def create_pending_match(
                self,
                _session: AsyncSession,
                _issue_id: int,
                _best: ReleaseResult,
                _validation: object,
            ) -> None:
                self.pending = True

        monkeypatch.setattr(
            "pullbox.services.intervention_service.InterventionService",
            FakeInterventionService,
        )
        queued_existing = await issues_api.download_issue(issue_id, object(), session)
        assert queued_existing["message"] == "Already queued for review"

        FakeInterventionService.pending = False
        queued = await issues_api.download_issue(issue_id, object(), session)
        assert queued["status"] == "queued"
        assert queued["confidence"] == MatchConfidence.HIGH.value


@pytest.mark.asyncio
async def test_import_file_and_progress_route_branches(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    issue_id = await _create_issue(db_factory)
    request = ManualFileImportRequest(file_path=str(tmp_path / "issue.cbz"))

    async with db_factory() as session:
        monkeypatch.setattr(
            issues_api,
            "prepare_manual_issue_import",
            AsyncMock(
                side_effect=ManualIssueImportError(status_code=422, detail="bad manual import")
            ),
        )
        with pytest.raises(HTTPException) as manual_error:
            await issues_api.import_file_for_issue(issue_id, request, object(), session)
        assert manual_error.value.status_code == 422

        monkeypatch.setattr(
            issues_api,
            "prepare_manual_issue_import",
            AsyncMock(side_effect=FileNotFoundError("missing source")),
        )
        with pytest.raises(HTTPException) as missing_error:
            await issues_api.import_file_for_issue(issue_id, request, object(), session)
        assert missing_error.value.status_code == 400

        monkeypatch.setattr(
            issues_api,
            "prepare_manual_issue_import",
            AsyncMock(side_effect=ConfigurationError("missing root")),
        )
        with pytest.raises(HTTPException) as config_error:
            await issues_api.import_file_for_issue(issue_id, request, object(), session)
        assert config_error.value.status_code == 400

        issue = (
            await session.execute(
                select(Issue).options(joinedload(Issue.series)).where(Issue.id == issue_id)
            )
        ).scalar_one()
        issue.series.library_root_id = 1
        prepared = SimpleNamespace(
            source_path=tmp_path / "issue.cbz",
            issue=issue,
            ingest_policy=SimpleNamespace(post_processing_method="copy"),
        )
        library_file = LibraryFile(
            id=55,
            issue_id=issue_id,
            library_root_id=1,
            file_path="/library/issue.cbz",
            file_name="issue.cbz",
            file_size=123,
            file_format=FileFormat.CBZ,
            match_confidence=MatchConfidence.MANUAL,
        )
        monkeypatch.setattr(
            issues_api,
            "prepare_manual_issue_import",
            AsyncMock(return_value=prepared),
        )
        monkeypatch.setattr(
            issues_api,
            "register_library_file",
            AsyncMock(return_value=library_file),
        )
        imported = await issues_api.import_file_for_issue(issue_id, request, object(), session)
        assert imported.library_file_id == 55
        assert imported.match_confidence == MatchConfidence.MANUAL.value

    async with db_factory() as session:
        fallback_progress = await issues_api.get_import_file_for_issue_progress(
            issue_id,
            object(),
            session,
        )
        assert fallback_progress.issue_id == issue_id
        assert fallback_progress.state == "idle"


@pytest.mark.asyncio
async def test_download_issue_file_error_and_success_branches(
    db_factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    issue_id = await _create_issue(db_factory)
    file_path = tmp_path / "issue.cbz"
    file_path.write_text("comic")

    async with db_factory() as session:
        with pytest.raises(NotFoundError):
            await issues_api.download_issue_file(99999, object(), session)

        with pytest.raises(HTTPException) as no_file:
            await issues_api.download_issue_file(issue_id, object(), session)
        assert no_file.value.status_code == 404

        root = LibraryRoot(name="Root", path=str(tmp_path), enabled=True)
        session.add(root)
        await session.flush()
        library_file = LibraryFile(
            issue_id=issue_id,
            library_root_id=root.id,
            file_path=str(tmp_path / "missing.cbz"),
            file_name="missing.cbz",
            file_size=1,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
        )
        session.add(library_file)
        await session.flush()
        session.expire_all()
        with pytest.raises(HTTPException) as missing_disk:
            await issues_api.download_issue_file(issue_id, object(), session)
        assert missing_disk.value.detail == "File no longer exists on disk"

        library_file.file_path = str(file_path)
        library_file.file_name = file_path.name
        response = await issues_api.download_issue_file(issue_id, object(), session)
        assert response.path == file_path
        assert response.filename == file_path.name

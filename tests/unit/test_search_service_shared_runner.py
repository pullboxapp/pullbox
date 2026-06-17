"""Focused coverage for the shared issue-search runner."""

from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.exceptions import NotFoundError
from pullbox.models import Base
from pullbox.models.config import SystemConfig
from pullbox.models.indexer import IndexerConfig, IndexerType
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import MatchConfidence
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.base import ProviderRegistry, ReleaseResult
from pullbox.services import search_service
from pullbox.services.search_service import (
    IssueSearchOutcome,
    IssueSearchTarget,
    SearchQuery,
    SearchService,
    _dedupe_release_results,
    _select_best_validation,
    build_search_runtime,
    load_issue_search_target,
    load_series_wanted_search_targets,
    load_wanted_issue_search_targets,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator


def _make_release(
    title: str,
    *,
    indexer_name: str = "NZBgeek",
    download_url: str | None = None,
    grabs: int | None = 50,
    seeders: int | None = None,
    is_torrent: bool = False,
) -> ReleaseResult:
    return ReleaseResult(
        title=title,
        indexer_name=indexer_name,
        download_url=download_url or f"https://example.com/{title.replace(' ', '_')}",
        size_bytes=100_000_000,
        age_days=5,
        seeders=seeders,
        leechers=2 if is_torrent else None,
        grabs=grabs if not is_torrent else None,
        is_torrent=is_torrent,
        category="7030",
        published_at=None,
    )


def _make_target(
    *,
    issue_id: int = 11,
    series_id: int = 7,
    series_title: str = "Absolute Superman",
    issue_number: float = 1.0,
    issue_type: IssueType = IssueType.ISSUE,
    issue_title: str | None = "Issue #1",
    series_year: int | None = 2025,
    alternate_names: list[str] | None = None,
) -> IssueSearchTarget:
    return IssueSearchTarget(
        issue_id=issue_id,
        series_id=series_id,
        series_title=series_title,
        issue_number=issue_number,
        issue_type=issue_type,
        issue_title=issue_title,
        series_year=series_year,
        alternate_names=alternate_names,
    )


def _make_outcome(
    target: IssueSearchTarget,
    release: ReleaseResult | None = None,
    *,
    mode: str = "fast",
) -> IssueSearchOutcome:
    validation = None
    if release is not None:
        validation = SimpleNamespace(release=release, confidence=MatchConfidence.HIGH)
    return IssueSearchOutcome(
        target=target,
        mode=mode,
        query_count=1,
        raw_results=[release] if release is not None else [],
        filtered_results=[release] if release is not None else [],
        matched=[validation] if validation is not None else [],
        rejected=[],
        best_release=release,
        best_validation=validation,
        search_details={"results_count": 1 if release is not None else 0},
        elapsed_ms=0,
        used_fallback=False,
    )


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def clear_shared_search_query_cache() -> Generator[None]:
    search_service._SHARED_QUERY_CACHE.clear()
    search_service._SHARED_QUERY_INFLIGHT.clear()
    yield
    search_service._SHARED_QUERY_CACHE.clear()
    search_service._SHARED_QUERY_INFLIGHT.clear()


async def _seed_search_rows(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    async with factory() as session:
        monitored = Series(
            comicvine_id=101,
            title="Absolute Superman",
            sort_title="absolute superman",
            year_start=2025,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=2,
            alternate_names=["AS"],
        )
        unmonitored = Series(
            comicvine_id=102,
            title="Ignored Series",
            sort_title="ignored series",
            year_start=2024,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=False,
            issue_count=1,
        )
        session.add_all([monitored, unmonitored])
        await session.flush()

        wanted = Issue(
            series_id=monitored.id,
            comicvine_id=201,
            issue_number=9.0,
            title="Vol. 1: All Weather Turns To Storm",
            status=IssueStatus.WANTED,
            issue_type=IssueType.TPB,
        )
        skipped = Issue(
            series_id=monitored.id,
            comicvine_id=202,
            issue_number=10.0,
            title="Issue #10",
            status=IssueStatus.SKIPPED,
            issue_type=IssueType.ISSUE,
        )
        stale = Issue(
            series_id=unmonitored.id,
            comicvine_id=203,
            issue_number=1.0,
            title="Issue #1",
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        session.add_all([wanted, skipped, stale])
        await session.commit()
        return {
            "monitored_series_id": monitored.id,
            "wanted_issue_id": wanted.id,
            "skipped_issue_id": skipped.id,
            "stale_issue_id": stale.id,
        }


@pytest.mark.asyncio
async def test_load_issue_and_wanted_targets_filter_and_shape(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed_search_rows(db_factory)

    async with db_factory() as session:
        target = await load_issue_search_target(session, ids["wanted_issue_id"])
        assert target is not None
        assert target.series_title == "Absolute Superman"
        assert target.issue_type == IssueType.TPB
        assert target.alternate_names == ["AS"]

        assert await load_issue_search_target(session, 99999) is None

        series_targets = await load_series_wanted_search_targets(
            session,
            ids["monitored_series_id"],
        )
        assert [item.issue_id for item in series_targets] == [ids["wanted_issue_id"]]

        wanted_targets = await load_wanted_issue_search_targets(session, limit=10)
        assert [item.issue_id for item in wanted_targets] == [ids["wanted_issue_id"]]


def test_issue_query_builders_cover_fast_deep_and_fallback_variants() -> None:
    service = SearchService(ProviderRegistry())
    standard_target = _make_target(
        series_title="Absolute Flash",
        issue_number=1.0,
        issue_type=IssueType.ISSUE,
    )
    collection_target = _make_target(
        issue_type=IssueType.TPB,
        issue_title="Vol. 1: All Weather Turns To Storm",
        issue_number=1.0,
    )

    standard_fast_queries = service._build_issue_queries(standard_target, mode="fast")
    assert [(query.series_title, query.issue_number) for query in standard_fast_queries] == [
        ("Absolute Flash 1", None),
        ("Absolute Flash 01", None),
        ("Absolute Flash #01", None),
        ("Absolute Flash 001", None),
        ("Absolute Flash #001", None),
    ]

    fast_queries = service._build_issue_queries(collection_target, mode="fast")
    assert [(query.series_title, query.issue_number) for query in fast_queries] == [
        ("Absolute Superman", 1.0)
    ]

    deep_queries = service._build_issue_queries(collection_target, mode="deep")
    deep_titles = {query.series_title for query in deep_queries}
    assert "Absolute Superman TPB 1" in deep_titles
    assert "Absolute Superman Vol 1" in deep_titles
    assert "Absolute Superman" in deep_titles
    assert "Absolute Superman 2025" in deep_titles
    assert "Absolute Superman All Weather Turns To Storm" in deep_titles
    assert all(query.issue_number is None for query in deep_queries)

    generic_queries = service._build_issue_queries(
        collection_target,
        mode="deep",
        force_generic=True,
    )
    assert [query.series_title for query in generic_queries] == [
        "Absolute Superman 1",
        "Absolute Superman 01",
        "Absolute Superman #01",
        "Absolute Superman 001",
        "Absolute Superman #001",
    ]

    collection_fallback = service._build_auto_fallback_queries(collection_target)
    assert [query.series_title for query in collection_fallback] == ["Absolute Superman"]

    standard_fallback = service._build_auto_fallback_queries(_make_target())
    assert [query.series_title for query in standard_fallback] == ["Absolute Superman"]


def test_fast_wanted_queries_keep_typed_and_collection_searches_bounded() -> None:
    service = SearchService(ProviderRegistry())

    standard_queries = service._build_issue_queries(
        _make_target(
            series_title="Absolute Flash",
            issue_number=1.0,
            issue_type=IssueType.ISSUE,
        ),
        mode="fast",
    )
    annual_queries = service._build_issue_queries(
        _make_target(
            series_title="Batman Annual",
            issue_number=1.0,
            issue_type=IssueType.ANNUAL,
        ),
        mode="fast",
    )
    tpb_queries = service._build_issue_queries(
        _make_target(
            series_title="Absolute Superman",
            issue_number=1.0,
            issue_type=IssueType.TPB,
            issue_title="Vol. 1: All Weather Turns To Storm",
        ),
        mode="fast",
    )

    assert [query.series_title for query in standard_queries] == [
        "Absolute Flash 1",
        "Absolute Flash 01",
        "Absolute Flash #01",
        "Absolute Flash 001",
        "Absolute Flash #001",
    ]
    assert [(query.series_title, query.issue_number) for query in annual_queries] == [
        ("Batman Annual", 1.0)
    ]
    assert [(query.series_title, query.issue_number) for query in tpb_queries] == [
        ("Absolute Superman", 1.0)
    ]


def test_deduping_and_min_score_filtering_cover_edge_cases() -> None:
    original = _make_release(
        "Absolute Superman 009",
        download_url="https://example.com/original",
        grabs=5,
    )
    better = _make_release(
        " Absolute Superman 009 ",
        download_url="https://example.com/better",
        grabs=25,
    )
    duplicate_url = _make_release(
        "Absolute Superman 009",
        download_url="https://example.com/better",
        grabs=100,
    )

    deduped = _dedupe_release_results([original, better, duplicate_url])
    assert deduped == [better]

    validation = SimpleNamespace(release=better, confidence=MatchConfidence.LOW)
    assert _select_best_validation([validation], min_score=10_000) is None


@pytest.mark.asyncio
async def test_run_query_batch_and_search_targets_cover_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SearchService(ProviderRegistry())
    query_a = SearchQuery(series_title="Absolute Superman", issue_number=1.0)
    query_b = SearchQuery(series_title="Absolute Superman TPB", issue_number=None)
    overlapping = _make_release("Absolute Superman 001", download_url="https://example.com/a")
    better = _make_release(
        "Absolute Superman 001",
        download_url="https://example.com/b",
        grabs=99,
    )

    async def fake_search_indexers(
        query: SearchQuery,
        *,
        indexer_configs: dict[int, object] | None = None,
    ) -> list[ReleaseResult]:
        if query.series_title == "Absolute Superman":
            return [overlapping]
        return [better]

    monkeypatch.setattr(service, "_search_indexers", fake_search_indexers)
    combined = await service._run_query_batch([query_a, query_b])
    assert combined == [better]

    empty = await service.search_targets(
        object(),
        [],
        mode="fast",
        concurrency=3,
    )
    assert empty == []

    in_flight = 0
    max_in_flight = 0

    async def fake_search_issue_target(  # type: ignore[no-untyped-def]
        self,
        session,
        target,
        **kwargs,
    ) -> IssueSearchOutcome:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return _make_outcome(target)

    monkeypatch.setattr(
        service,
        "search_issue_target",
        MethodType(fake_search_issue_target, service),
    )
    targets = [_make_target(issue_id=issue_id) for issue_id in (1, 2, 3)]
    outcomes = await service.search_targets(
        object(),
        targets,
        mode="fast",
        concurrency=2,
    )
    assert [outcome.target.issue_id for outcome in outcomes] == [1, 2, 3]
    assert max_in_flight == 2


@pytest.mark.asyncio
async def test_search_indexers_coalesces_identical_inflight_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SearchService(ProviderRegistry())
    query = SearchQuery(series_title="Absolute Flash #001", issue_number=None, year=2025)
    release = _make_release("Absolute Flash #001 (2025).cbz")
    call_count = 0

    async def fake_search_indexers(*args: object, **kwargs: object) -> list[ReleaseResult]:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0)
        return [release]

    monkeypatch.setattr(search_service._search_indexers, "search_indexers", fake_search_indexers)

    first, second = await asyncio.gather(service.search(query), service.search(query))
    third = await service.search(query)

    assert first == [release]
    assert second == [release]
    assert third == [release]
    assert call_count == 1


@pytest.mark.asyncio
async def test_search_indexers_reuses_short_ttl_cache_across_configured_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_a = SearchService(ProviderRegistry())
    service_b = SearchService(ProviderRegistry())
    query = SearchQuery(series_title="Absolute Flash #001", issue_number=None, year=2025)
    release = _make_release("Absolute Flash #001 (2025).cbz")
    call_count = 0
    config = IndexerConfig(
        name="Local Prowlarr",
        indexer_type=IndexerType.PROWLARR,
        url="http://prowlarr:9696",
        api_key="test",
        enabled=True,
        priority=1,
        categories="7030",
    )
    config.disabled_until = None
    indexer_configs = cast("dict[int, IndexerConfig]", {1: config})

    async def fake_search_indexers(*args: object, **kwargs: object) -> list[ReleaseResult]:
        nonlocal call_count
        call_count += 1
        return [release]

    monkeypatch.setattr(search_service._search_indexers, "search_indexers", fake_search_indexers)

    first = await service_a.search(query, indexer_configs=indexer_configs)
    second = await service_b.search(query, indexer_configs=indexer_configs)

    assert first == [release]
    assert second == [release]
    assert call_count == 1


@pytest.mark.asyncio
async def test_search_indexers_coalesces_configured_inflight_across_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_a = SearchService(ProviderRegistry())
    service_b = SearchService(ProviderRegistry())
    query = SearchQuery(series_title="Absolute Batman #001", issue_number=None, year=2024)
    release = _make_release("Absolute Batman #001 (2024).cbz")
    call_count = 0
    config = IndexerConfig(
        name="Local Prowlarr",
        indexer_type=IndexerType.PROWLARR,
        url="http://prowlarr:9696",
        api_key="test",
        enabled=True,
        priority=1,
        categories="7030",
    )
    config.disabled_until = None
    indexer_configs = cast("dict[int, IndexerConfig]", {1: config})

    async def fake_search_indexers(*args: object, **kwargs: object) -> list[ReleaseResult]:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0)
        return [release]

    monkeypatch.setattr(search_service._search_indexers, "search_indexers", fake_search_indexers)

    first, second = await asyncio.gather(
        service_a.search(query, indexer_configs=indexer_configs),
        service_b.search(query, indexer_configs=indexer_configs),
    )

    assert first == [release]
    assert second == [release]
    assert call_count == 1


@pytest.mark.asyncio
async def test_search_indexers_keeps_unconfigured_registries_cache_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_a = SearchService(ProviderRegistry())
    service_b = SearchService(ProviderRegistry())
    query = SearchQuery(series_title="Absolute Flash #001", issue_number=None, year=2025)
    release = _make_release("Absolute Flash #001 (2025).cbz")
    call_count = 0

    async def fake_search_indexers(*args: object, **kwargs: object) -> list[ReleaseResult]:
        nonlocal call_count
        call_count += 1
        return [release]

    monkeypatch.setattr(search_service._search_indexers, "search_indexers", fake_search_indexers)

    first = await service_a.search(query)
    second = await service_b.search(query)

    assert first == [release]
    assert second == [release]
    assert call_count == 2


@pytest.mark.asyncio
async def test_search_issue_target_uses_fallback_priority_and_best_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SearchService(ProviderRegistry())
    target = _make_target(
        issue_type=IssueType.ISSUE,
        issue_title="Issue #9",
        issue_number=9.0,
    )
    other = _make_release(
        "Absolute Superman 009",
        indexer_name="Other",
        download_url="https://example.com/other",
        grabs=10,
    )
    preferred = _make_release(
        "Absolute Superman 009",
        indexer_name="Preferred",
        download_url="https://example.com/preferred",
        grabs=10,
    )
    validations = [
        SimpleNamespace(release=other, confidence=MatchConfidence.LOW),
        SimpleNamespace(release=preferred, confidence=MatchConfidence.HIGH),
    ]

    monkeypatch.setattr(
        service,
        "_run_query_batch_with_provenance",
        AsyncMock(side_effect=[([], {}, []), ([other, preferred], {}, [])]),
    )
    monkeypatch.setattr(
        search_service,
        "build_search_details",
        lambda *args, **kwargs: {"details": "ok"},
    )
    monkeypatch.setattr(search_service, "log_type_detection", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        search_service.ReleaseValidator,
        "validate_all_results",
        lambda self, results, **kwargs: (validations, []),
    )
    blocklist_service = __import__(
        "pullbox.services.blocklist_service",
        fromlist=["BlocklistService"],
    ).BlocklistService
    monkeypatch.setattr(
        blocklist_service,
        "filter_results",
        AsyncMock(side_effect=lambda session, results: results),
    )

    outcome = await service.search_issue_target(
        MagicMock(),
        target,
        mode="deep",
        source_priority=["Preferred", "Other"],
        auto_fallback=True,
    )

    assert outcome.used_fallback is True
    assert outcome.query_count == 7
    assert {release.indexer_name for release in outcome.raw_results} == {"Preferred", "Other"}
    assert outcome.best_validation is validations[1]
    assert outcome.best_release is preferred


@pytest.mark.asyncio
async def test_search_issue_target_logs_search_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SearchService(ProviderRegistry())
    target = _make_target(
        series_title="Absolute Flash",
        issue_number=1.0,
        issue_type=IssueType.ISSUE,
        series_year=2025,
    )
    matched_release = _make_release(
        "Absolute Flash #001 (2025) (Digital) (Zone-Empire).cbz",
        indexer_name="MyAnonamouse",
        is_torrent=True,
        seeders=42,
    )
    rejected_release = _make_release(
        "Absolute Batman 007 (2025) (Digital).cbz",
        indexer_name="NZBgeek",
    )
    fake_log = MagicMock()

    monkeypatch.setattr(
        service,
        "_run_query_batch_with_provenance",
        AsyncMock(return_value=([matched_release, rejected_release], {}, [])),
    )
    monkeypatch.setattr(search_service, "log_type_detection", lambda *args, **kwargs: None)
    monkeypatch.setattr(search_service, "logger", fake_log)
    blocklist_service = __import__(
        "pullbox.services.blocklist_service",
        fromlist=["BlocklistService"],
    ).BlocklistService
    monkeypatch.setattr(
        blocklist_service,
        "filter_results",
        AsyncMock(side_effect=lambda session, results: results),
    )

    outcome = await service.search_issue_target(MagicMock(), target, mode="deep")

    assert outcome.matched
    assert outcome.rejected
    complete_log = next(
        call for call in fake_log.info.call_args_list if call.args[0] == "issue_search_complete"
    )
    assert complete_log.kwargs["best_match"]["title"] == matched_release.title
    assert complete_log.kwargs["best_match"]["reason_summary"] == "high confidence exact match"
    assert complete_log.kwargs["top_rejected"][0]["title"] == rejected_release.title
    assert complete_log.kwargs["top_rejected"][0]["reason"]


@pytest.mark.asyncio
async def test_search_issue_target_records_query_provenance_in_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SearchService(ProviderRegistry())
    target = _make_target(
        series_title="Absolute Flash",
        issue_number=1.0,
        issue_type=IssueType.ISSUE,
        series_year=2025,
    )
    release = _make_release(
        "Absolute Flash #001 (2025) (Digital) (Zone-Empire).cbz",
        indexer_name="MyAnonamouse",
        is_torrent=True,
        seeders=42,
    )

    async def fake_search_indexers(query: SearchQuery, **kwargs: object) -> list[ReleaseResult]:
        if query.series_title == "Absolute Flash #001":
            return [release]
        return []

    monkeypatch.setattr(service, "_search_indexers", fake_search_indexers)
    monkeypatch.setattr(search_service, "log_type_detection", lambda *args, **kwargs: None)
    blocklist_service = __import__(
        "pullbox.services.blocklist_service",
        fromlist=["BlocklistService"],
    ).BlocklistService
    monkeypatch.setattr(
        blocklist_service,
        "filter_results",
        AsyncMock(side_effect=lambda session, results: results),
    )

    outcome = await service.search_issue_target(MagicMock(), target, mode="deep")

    assert outcome.matched
    assert outcome.search_details["matched"][0]["query"] == "Absolute Flash #001"
    assert outcome.search_details["best_match"]["query"] == "Absolute Flash #001"


@pytest.mark.asyncio
async def test_search_issue_target_records_query_timing_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SearchService(ProviderRegistry())
    target = _make_target(
        series_title="Absolute Flash",
        issue_number=1.0,
        issue_type=IssueType.ISSUE,
        series_year=2025,
    )
    release = _make_release(
        "Absolute Flash #001 (2025) (Digital) (Zone-Empire).cbz",
        indexer_name="MyAnonamouse",
        is_torrent=True,
        seeders=42,
    )

    async def fake_search_indexers(query: SearchQuery, **kwargs: object) -> list[ReleaseResult]:
        timing_collector = kwargs.get("timing_collector")
        if isinstance(timing_collector, list):
            timing_collector.append(
                {
                    "query": query.series_title,
                    "indexer": "MyAnonamouse",
                    "status": "completed",
                    "elapsed_ms": 2,
                    "raw_count": 1,
                    "result_count": 1,
                    "filtered_count": 0,
                }
            )
        if query.series_title == "Absolute Flash #001":
            return [release]
        return []

    monkeypatch.setattr(service, "_search_indexers", fake_search_indexers)
    monkeypatch.setattr(search_service, "log_type_detection", lambda *args, **kwargs: None)
    blocklist_service = __import__(
        "pullbox.services.blocklist_service",
        fromlist=["BlocklistService"],
    ).BlocklistService
    monkeypatch.setattr(
        blocklist_service,
        "filter_results",
        AsyncMock(side_effect=lambda session, results: results),
    )

    outcome = await service.search_issue_target(MagicMock(), target, mode="deep")

    diagnostics = outcome.search_details["query_diagnostics"]
    assert diagnostics[4]["query"] == "Absolute Flash #001"
    assert diagnostics[4]["result_count"] == 1
    assert diagnostics[4]["indexers"][0]["indexer"] == "MyAnonamouse"


@pytest.mark.asyncio
async def test_search_for_issue_and_search_wanted_delegate_to_shared_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SearchService(ProviderRegistry())
    target = _make_target(issue_id=51)
    release = _make_release("Absolute Superman 051")
    outcome = _make_outcome(target, release, mode="deep")

    monkeypatch.setattr(search_service, "load_issue_search_target", AsyncMock(return_value=None))
    with pytest.raises(NotFoundError):
        await service.search_for_issue(MagicMock(), 999)

    monkeypatch.setattr(search_service, "load_issue_search_target", AsyncMock(return_value=target))
    monkeypatch.setattr(service, "search_issue_target", AsyncMock(return_value=outcome))
    results = await service.search_for_issue(
        MagicMock(),
        target.issue_id,
        source_priority=["Preferred"],
        auto_fallback=True,
    )
    assert results == [release]
    service.search_issue_target.assert_awaited_once()

    monkeypatch.setattr(
        search_service,
        "load_wanted_issue_search_targets",
        AsyncMock(return_value=[target]),
    )
    monkeypatch.setattr(service, "search_targets_quick_first", AsyncMock(return_value=[outcome]))
    results_map = await service.search_wanted(
        MagicMock(),
        limit=5,
        indexer_configs={1: MagicMock()},
    )
    assert results_map == {target.issue_id: [release]}
    service.search_targets_quick_first.assert_awaited_once()
    assert service.search_targets_quick_first.await_args.kwargs["enable_deep_fallback"] is True


@pytest.mark.asyncio
async def test_build_search_runtime_parses_config_and_respects_registry_flag(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProviderRegistry()
    build_registry_mock = AsyncMock(return_value=(registry, {7: MagicMock()}))
    monkeypatch.setattr(
        __import__("pullbox.composition.providers", fromlist=["build_registry"]),
        "build_registry",
        build_registry_mock,
    )

    async with db_factory() as session:
        session.add_all(
            [
                SystemConfig(key="source_priority", value="not-json"),
                SystemConfig(key="search_type_thresholds", value='{"issue": "medium"}'),
                SystemConfig(key="search_size_warn_issue_mb", value="625"),
                SystemConfig(key="search_size_warn_collection_mb", value="80"),
                SystemConfig(key="indexer_failure_threshold", value="5"),
            ]
        )
        await session.commit()

        runtime = await build_search_runtime(session, include_download_clients=False)

    assert runtime is not None
    assert runtime.registry is registry
    assert runtime.indexer_configs.keys() == {7}
    assert runtime.source_priority is None
    assert runtime.type_thresholds["issue"] == "medium"
    assert runtime.eval_kwargs["warn_issue_mb"] == 625
    assert runtime.eval_kwargs["warn_collection_mb"] == 80
    assert runtime.failure_threshold == 5
    assert build_registry_mock.await_args.kwargs["include_download_clients"] is False

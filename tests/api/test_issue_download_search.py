"""Focused tests for the issue download-from-search endpoint."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.api.v1 import issues as issues_api
from pullbox.core.exceptions import ProviderError
from pullbox.models import Base
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import MatchConfidence
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import APIKey, User
from pullbox.providers.base import ProviderRegistry, ReleaseResult
from pullbox.services.auth_service import AuthService
from pullbox.services.direct_search_coordinator import DirectSearchOutcome
from pullbox.services.release_validator import ReleaseValidator
from pullbox.services.search_acquisition_router import SearchAcquisitionRoutingResult
from pullbox.services.search_service import IssueSearchOutcome, IssueSearchTarget, SearchRuntime

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _make_release(title: str = "Absolute Superman 009") -> ReleaseResult:
    return ReleaseResult(
        title=title,
        indexer_name="NZBgeek",
        download_url=f"https://example.com/{title.replace(' ', '_')}",
        size_bytes=100_000_000,
        age_days=2,
        seeders=None,
        leechers=None,
        grabs=30,
        is_torrent=False,
        category="7030",
        published_at=None,
    )


@pytest.fixture
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    raw_key = "pb_k1_" + "d" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="issuedownloaduser",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="issue-download-test"))
        await session.commit()

    app = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with _db_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_dep] = _override_db
    reset_setup_cache()

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-Api-Key": raw_key},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


async def _create_issue(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        series = Series(
            comicvine_id=701,
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
            comicvine_id=9901,
            issue_number=9.0,
            title="Issue #9",
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        session.add(issue)
        await session.commit()
        return issue.id


def _bundle(
    issue_id: int,
    *,
    runtime: SearchRuntime | None,
    outcome: IssueSearchOutcome | None,
) -> issues_api._IssueSearchBundle:
    target = IssueSearchTarget(
        issue_id=issue_id,
        series_id=1,
        series_title="Absolute Superman",
        issue_number=9.0,
        issue_type=IssueType.ISSUE,
        issue_title="Issue #9",
        series_year=2025,
    )
    issue_ctx = issues_api._build_issue_context(target)
    return issues_api._IssueSearchBundle(
        target=target,
        issue=issue_ctx,
        runtime=runtime,
        outcome=outcome,
        matched_items=[],
        rejected_items=[],
        search_time_ms=1,
    )


@pytest.mark.asyncio
async def test_issue_download_returns_no_clients_when_runtime_missing(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    issue_id = await _create_issue(_db_factory)
    with patch(
        "pullbox.api.v1.issues._run_issue_search",
        new_callable=AsyncMock,
        return_value=_bundle(issue_id, runtime=None, outcome=None),
    ):
        resp = await client.post(f"/api/v1/issues/{issue_id}/download")

    assert resp.status_code == 200
    assert resp.json()["status"] == "no_clients"


@pytest.mark.asyncio
async def test_issue_download_returns_no_results_when_best_match_missing(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    issue_id = await _create_issue(_db_factory)
    runtime = SearchRuntime(
        registry=ProviderRegistry(),
        indexer_configs={},
        source_priority=None,
        eval_kwargs={},
        validator_kwargs={},
        type_thresholds={"issue": "high"},
        failure_threshold=3,
    )
    target = IssueSearchTarget(
        issue_id=issue_id,
        series_id=1,
        series_title="Absolute Superman",
        issue_number=9.0,
        issue_type=IssueType.ISSUE,
        issue_title="Issue #9",
        series_year=2025,
    )
    empty_outcome = IssueSearchOutcome(
        target=target,
        mode="deep",
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
    with patch(
        "pullbox.api.v1.issues._run_issue_search",
        new_callable=AsyncMock,
        return_value=_bundle(issue_id, runtime=runtime, outcome=empty_outcome),
    ):
        resp = await client.post(f"/api/v1/issues/{issue_id}/download")

    assert resp.status_code == 200
    assert resp.json()["status"] == "no_results"

    async with _db_factory() as session:
        logs = list(
            (
                await session.execute(
                    select(SearchLog).where(SearchLog.issue_id == issue_id).order_by(SearchLog.id)
                )
            )
            .scalars()
            .all()
        )

    assert len(logs) == 1
    assert logs[0].search_type == SearchType.AUTOMATED
    assert logs[0].results_found == 0
    assert (logs[0].details or {}).get("action_status") == "no_results"


@pytest.mark.asyncio
async def test_issue_download_auto_grabs_high_confidence_match(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    issue_id = await _create_issue(_db_factory)
    release = _make_release()
    validation = SimpleNamespace(release=release, confidence=MatchConfidence.HIGH)
    runtime = SearchRuntime(
        registry=ProviderRegistry(),
        indexer_configs={},
        source_priority=None,
        eval_kwargs={},
        validator_kwargs={},
        type_thresholds={"issue": "high"},
        failure_threshold=3,
    )
    target = IssueSearchTarget(
        issue_id=issue_id,
        series_id=1,
        series_title="Absolute Superman",
        issue_number=9.0,
        issue_type=IssueType.ISSUE,
        issue_title="Issue #9",
        series_year=2025,
    )
    outcome = IssueSearchOutcome(
        target=target,
        mode="deep",
        query_count=1,
        raw_results=[release],
        filtered_results=[release],
        matched=[validation],
        rejected=[],
        best_release=release,
        best_validation=validation,
        search_details={},
        elapsed_ms=0,
    )

    with (
        patch(
            "pullbox.api.v1.issues._run_issue_search",
            new_callable=AsyncMock,
            return_value=_bundle(issue_id, runtime=runtime, outcome=outcome),
        ),
        patch("pullbox.services.download_service.DownloadService") as download_cls,
    ):
        download_cls.return_value.send_to_client = AsyncMock(return_value=SimpleNamespace(id=123))
        resp = await client.post(f"/api/v1/issues/{issue_id}/download")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "downloading"
    assert data["download_id"] == 123
    assert data["release_title"] == release.title

    async with _db_factory() as session:
        logs = list(
            (
                await session.execute(
                    select(SearchLog).where(SearchLog.issue_id == issue_id).order_by(SearchLog.id)
                )
            )
            .scalars()
            .all()
        )

    assert len(logs) == 1
    assert logs[0].search_type == SearchType.AUTOMATED
    assert logs[0].results_found == 1
    assert logs[0].results_grabbed == 1
    assert logs[0].results_queued == 0
    assert (logs[0].details or {}).get("action_status") == "downloading"


@pytest.mark.asyncio
async def test_issue_download_routes_direct_only_match_through_shared_acquisition_router(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One-click download honors a direct-only winner and source priority."""
    issue_id = await _create_issue(_db_factory)
    release = _make_release("Absolute Superman 009 Direct")
    direct_match = SimpleNamespace(
        release=release,
        validation=SimpleNamespace(confidence=MatchConfidence.HIGH),
    )
    runtime = SearchRuntime(
        registry=ProviderRegistry(),
        indexer_configs={},
        source_priority=["direct", "usenet", "torrent"],
        eval_kwargs={},
        validator_kwargs={},
        type_thresholds={"issue": "high"},
        failure_threshold=3,
        direct_providers=(SimpleNamespace(),),
    )
    target = IssueSearchTarget(
        issue_id=issue_id,
        series_id=1,
        series_title="Absolute Superman",
        issue_number=9.0,
        issue_type=IssueType.ISSUE,
        issue_title="Issue #9",
        series_year=2025,
    )
    outcome = IssueSearchOutcome(
        target=target,
        mode="deep",
        query_count=1,
        raw_results=[],
        filtered_results=[],
        matched=[],
        rejected=[],
        best_release=None,
        best_validation=None,
        search_details={},
        elapsed_ms=0,
        direct_outcome=DirectSearchOutcome((direct_match,), (), (), 1, 0),  # type: ignore[arg-type]
    )
    routed = SearchAcquisitionRoutingResult(
        grabbed=1,
        queued=0,
        action_status="downloading",
        best_confidence="high",
        source_kind="direct",
    )

    with (
        patch(
            "pullbox.api.v1.issues._run_issue_search",
            new_callable=AsyncMock,
            return_value=_bundle(issue_id, runtime=runtime, outcome=outcome),
        ),
        patch(
            "pullbox.api.v1.issues.route_search_acquisition",
            new_callable=AsyncMock,
            return_value=routed,
        ) as route,
        patch(
            "pullbox.api.v1.issues.select_search_source",
            return_value=direct_match,
        ),
        patch(
            "pullbox.api.v1.issues.get_direct_acquisition_runner",
            return_value=SimpleNamespace(),
        ),
    ):
        resp = await client.post(f"/api/v1/issues/{issue_id}/download")

    assert resp.status_code == 200
    assert resp.json()["status"] == "downloading"
    assert resp.json()["release_title"] == release.title
    assert resp.json()["source_kind"] == "direct"
    assert route.await_args is not None
    assert route.await_args.kwargs["outcome"] is outcome
    assert route.await_args.kwargs["source_priority"] == ["direct", "usenet", "torrent"]


@pytest.mark.asyncio
async def test_issue_download_returns_the_source_metadata_selected_after_fallback(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    issue_id = await _create_issue(_db_factory)
    initial_release = _make_release("Initial Direct Candidate")
    runtime = SearchRuntime(
        registry=ProviderRegistry(),
        indexer_configs={},
        source_priority=["direct", "usenet", "torrent"],
        eval_kwargs={},
        validator_kwargs={},
        type_thresholds={"issue": "high"},
        failure_threshold=3,
        direct_providers=(SimpleNamespace(),),
    )
    target = IssueSearchTarget(
        issue_id=issue_id,
        series_id=1,
        series_title="Absolute Superman",
        issue_number=9.0,
        issue_type=IssueType.ISSUE,
        issue_title="Issue #9",
        series_year=2025,
    )
    outcome = IssueSearchOutcome(
        target=target,
        mode="deep",
        query_count=1,
        raw_results=[initial_release],
        filtered_results=[initial_release],
        matched=[],
        rejected=[],
        best_release=None,
        best_validation=None,
        search_details={},
        elapsed_ms=0,
    )
    routed = SearchAcquisitionRoutingResult(
        grabbed=1,
        queued=0,
        action_status="downloading",
        best_confidence="high",
        source_kind="indexer",
        release_title="Fallback Indexer Candidate",
    )

    with (
        patch(
            "pullbox.api.v1.issues._run_issue_search",
            new_callable=AsyncMock,
            return_value=_bundle(issue_id, runtime=runtime, outcome=outcome),
        ),
        patch(
            "pullbox.api.v1.issues.route_search_acquisition",
            new_callable=AsyncMock,
            return_value=routed,
        ),
        patch(
            "pullbox.api.v1.issues.select_search_source",
            return_value=SimpleNamespace(
                release=initial_release,
                validation=SimpleNamespace(confidence=MatchConfidence.HIGH),
            ),
        ),
        patch(
            "pullbox.api.v1.issues.get_direct_acquisition_runner",
            return_value=SimpleNamespace(),
        ),
    ):
        response = await client.post(f"/api/v1/issues/{issue_id}/download")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_kind"] == "indexer"
    assert payload["release_title"] == "Fallback Indexer Candidate"


@pytest.mark.parametrize("queue_fails", [True, False])
async def test_issue_download_preserves_matches_and_reports_queue_failure(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    queue_fails: bool,
) -> None:
    issue_id = await _create_issue(_db_factory)
    runtime = SearchRuntime(ProviderRegistry(), {}, None, {}, {}, {"issue": "high"}, 3)
    target = _bundle(issue_id, runtime=runtime, outcome=None).target
    releases = [
        _make_release("Absolute Superman 009 (2025)"),
        _make_release("Absolute Superman 009 (2025) (Digital-Empire)"),
        _make_release("Absolute Superman 010 (2025)"),
    ]
    matched, rejected = ReleaseValidator().validate_all_results(
        releases, target.series_title, target.issue_number, target.series_year
    )
    assert len(matched) == 2 and len(rejected) == 1
    outcome = IssueSearchOutcome(
        target,
        "fast",
        1,
        releases,
        releases,
        matched,
        rejected,
        matched[0].release,
        matched[0],
        {},
        1,
    )
    with (
        patch.object(
            issues_api,
            "_run_issue_search",
            AsyncMock(return_value=_bundle(issue_id, runtime=runtime, outcome=outcome)),
        ),
        patch("pullbox.services.download_service.DownloadService") as download_cls,
    ):
        send = AsyncMock(return_value=SimpleNamespace(id=123))
        if queue_fails:
            send.side_effect = ProviderError("SABnzbd", "Private upstream failure")
        download_cls.return_value.send_to_client = send
        response = await client.post(f"/api/v1/issues/{issue_id}/download")

    assert response.status_code == 200
    data = response.json()
    if queue_fails:
        assert data["status"] == "source_unavailable"
        assert data["message"] == "Matches found, but downloads could not be queued."
        assert data["notices"]
        assert "Private upstream failure" not in response.text
        assert send.await_count == 2
    else:
        assert data["status"] == "downloading"
    async with _db_factory() as session:
        log = await session.scalar(select(SearchLog).where(SearchLog.issue_id == issue_id))
        assert log is not None
        assert log.results_found == 3
        assert log.results_rejected == 1
        assert log.results_grabbed == (0 if queue_fails else 1)


async def test_single_issue_download_attaches_automatic_dc_search_before_selection(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.services import airdcpp_automatic_search

    issue_id = await _create_issue(_db_factory)
    runtime = SearchRuntime(ProviderRegistry(), {}, None, {}, {}, {"issue": "high"}, 3)
    target = _bundle(issue_id, runtime=runtime, outcome=None).target
    original = IssueSearchOutcome(target, "fast", 0, [], [], [], [], None, None, {}, 1)
    attached = replace(original, search_details={"dc_results_count": 0})
    attach = AsyncMock(return_value=attached)
    monkeypatch.setattr(airdcpp_automatic_search, "attach_automatic_airdcpp_search", attach)
    # Patch the boundary in either import style without requiring new production symbols.
    monkeypatch.setattr(issues_api, "attach_automatic_airdcpp_search", attach, raising=False)
    monkeypatch.setattr(
        issues_api,
        "_run_issue_search",
        AsyncMock(return_value=_bundle(issue_id, runtime=runtime, outcome=original)),
    )
    with patch.object(issues_api, "select_search_source", return_value=None) as select_source:
        response = await client.post(f"/api/v1/issues/{issue_id}/download")
    assert response.status_code == 200
    attach.assert_awaited_once()
    assert attach.await_args.args[1] is original
    assert select_source.call_args.args[0] is attached


@pytest.mark.parametrize("automatic_enabled", [True, False])
async def test_automatic_search_can_build_airdcpp_only_runtime(
    _db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    automatic_enabled: bool,
) -> None:
    issue_id = await _create_issue(_db_factory)
    runtime = SearchRuntime(ProviderRegistry(), {}, None, {}, {}, {"issue": "high"}, 3)
    target = _bundle(issue_id, runtime=runtime, outcome=None).target
    outcome = IssueSearchOutcome(target, "fast", 0, [], [], [], [], None, None, {}, 1)
    load_clients = AsyncMock(return_value=[object()] if automatic_enabled else [])
    monkeypatch.setattr(issues_api, "get_airdcpp_supervisor_registry", lambda: object())
    monkeypatch.setattr(issues_api, "load_airdcpp_search_clients", load_clients, raising=False)

    async def build_runtime(session, **kwargs):
        return runtime if kwargs.get("allow_empty_registry") else None

    monkeypatch.setattr(issues_api, "build_search_runtime", build_runtime)
    monkeypatch.setattr(
        issues_api.SearchService, "search_issue_target", AsyncMock(return_value=outcome)
    )
    async with _db_factory() as session:
        bundle = await issues_api._run_issue_search(
            session, issue_id, include_download_clients=True
        )
    assert (bundle.runtime is runtime) is automatic_enabled
    load_clients.assert_awaited_once()
    assert load_clients.call_args.kwargs["automatic"] is True

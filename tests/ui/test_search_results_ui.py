"""Tests for the HTMX interactive search results UI route.

Verifies:
- GET /htmx/issues/{id}/search-results returns 200 with HTML partial
- Confidence badge CSS classes are rendered correctly
- Matched results have grab buttons; rejected results have explicit override grab buttons
- Nonexistent issue returns 404
- No indexers configured returns empty results partial
- Low-confidence results render with correct styling
- Search time is displayed

Run:
    pytest tests/ui/test_search_results_ui.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models import Base
from pullbox.models.airdcpp import AirDcppClientSettings
from pullbox.models.client import DownloadClientConfig
from pullbox.models.direct_acquisition import (
    DirectProviderConfig,
    DirectProviderState,
    DirectProviderTrustLevel,
)
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import APIKey, User
from pullbox.providers.airdcpp.supervisor import AirDcppSupervisorState
from pullbox.providers.base import ReleaseResult
from pullbox.providers.direct.contract import DirectCandidate, DirectParsedCandidate
from pullbox.services.airdcpp_route_tokens import get_airdcpp_route_token_store
from pullbox.services.airdcpp_search_types import (
    DcMetrics,
    DcRoute,
    DcSearchOutcome,
    DcValidatedCandidate,
)
from pullbox.services.auth_service import AuthService
from pullbox.services.direct_search_coordinator import (
    DirectSearchOutcome,
    DirectSearchProvider,
    DirectValidatedCandidate,
)
from pullbox.services.release_validator import ReleaseValidator

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-search-results-ui")


def test_manual_direct_connect_cooldown_ui_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    modal = (root / "src/pullbox/ui/templates/partials/issue_search_modal.html").read_text()
    script = (root / "src/pullbox/ui/static/js/pullbox.js").read_text()

    assert 'data-testid="issue-search-dc-status"' in modal
    assert 'aria-live="polite"' in modal
    assert (
        "Direct Connect search will resume in "
        "{seconds} seconds to respect the 45-second hub cooldown."
    ) in script
    assert "/dc-search-status" in script
    assert "/dc-search-results" in script
    assert "AbortController" in script


def test_manual_direct_connect_result_uses_opaque_queue_route() -> None:
    root = Path(__file__).resolve().parents[2]
    result = (root / "src/pullbox/ui/templates/partials/issue_dc_search_results.html").read_text()
    script = (root / "src/pullbox/ui/static/js/pullbox.js").read_text()

    assert 'data-dc-route-token="{{ row.route_token }}"' in result
    assert '@click="grabRelease($el)"' in result
    assert "AirDC++ queueing is enabled in the next acquisition stage" not in result
    assert 'endpoint = "/api/v1/issues/" + cfg.issueId + "/dc-grab"' in script
    assert "payload = { dc_route_token: dcRouteToken }" in script


@pytest.mark.asyncio
async def test_manual_direct_connect_status_and_stream_are_independent_of_indexers(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    issue_id = await _create_issue(_db_factory)
    coordinator = AsyncMock()
    coordinator.cooldown_status.return_value = {7: 12}
    coordinator.search.return_value = DcSearchOutcome(
        matched=(),
        rejected=(),
        client_summaries=(),
        raw_count=0,
        deduplicated_count=0,
        dropped_count=0,
        elapsed_ms=5,
        partial=False,
    )
    operation = SimpleNamespace(config_id=7)

    with (
        patch(
            "pullbox.ui.series_detail_routes.get_airdcpp_supervisor_registry",
            return_value=object(),
        ),
        patch(
            "pullbox.ui.series_detail_routes.get_airdcpp_search_coordinator",
            return_value=coordinator,
        ),
        patch(
            "pullbox.ui.series_detail_routes.load_airdcpp_search_clients",
            new_callable=AsyncMock,
            return_value=(operation,),
        ),
        patch(
            "pullbox.ui.series_detail_routes.get_settings",
            return_value=SimpleNamespace(airdcpp_enabled=True),
        ),
    ):
        status = await client.get(f"/htmx/issues/{issue_id}/dc-search-status")
        stream = await client.get(
            f"/htmx/issues/{issue_id}/dc-search-results",
            headers={"Accept": "text/event-stream", "X-Api-Key": client.headers["X-Api-Key"]},
        )

    assert status.status_code == 200
    assert status.json() == {
        "available": True,
        "client_count": 1,
        "remaining_seconds": 12,
    }
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    frames = [frame for frame in stream.text.split("\n\n") if frame.startswith("data: ")]
    final = json.loads(frames[-1][6:])
    assert final["kind"] == "results"
    assert "0 Direct Connect results" in final["summary"]
    coordinator.search.assert_awaited_once()


# ── Test Data ──────────────────────────────────────────────────────────


def _make_release(
    title: str,
    indexer_name: str = "NZBgeek",
    *,
    size_bytes: int | None = 100_000_000,
    age_days: int | None = 5,
    is_torrent: bool = False,
    indexer_id: int | None = None,
) -> ReleaseResult:
    return ReleaseResult(
        title=title,
        indexer_name=indexer_name,
        download_url=f"https://indexer.example.com/dl/{title.replace(' ', '_')}",
        size_bytes=size_bytes,
        age_days=age_days,
        seeders=10 if is_torrent else None,
        leechers=2 if is_torrent else None,
        grabs=50 if not is_torrent else None,
        is_torrent=is_torrent,
        category="comics",
        published_at=None,
        indexer_id=indexer_id,
    )


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def _api_key_header(
    _db_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Create a test user + API key, return the raw key string."""
    raw_key = "pb_k1_" + "e" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="uisearchuser",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="ui-search-test"))
        await session.commit()
    return raw_key


@pytest.fixture
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
    _api_key_header: str,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated via API key (bypasses CSRF)."""
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()
    # Keep setup/auth middleware on the same in-memory database as the route deps.
    app.state.db_session_factory = _db_factory

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
        headers={"X-Api-Key": _api_key_header},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


async def _create_issue(
    factory: async_sessionmaker[AsyncSession],
    *,
    series_title: str = "Batman",
    year_start: int = 2016,
    issue_number: float = 1.0,
) -> int:
    """Seed a series + issue, return the issue_id."""
    async with factory() as session:
        series = Series(
            comicvine_id=99900,
            title=series_title,
            sort_title=series_title,
            year_start=year_start,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        session.add(series)
        await session.flush()

        issue = Issue(
            series_id=series.id,
            comicvine_id=50001,
            issue_number=issue_number,
            title=f"Issue #{int(issue_number)}",
            status=IssueStatus.WANTED,
        )
        session.add(issue)
        await session.commit()
        return issue.id


def _dc_candidate(config_id: int) -> DcValidatedCandidate:
    tth = "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y"
    release = ReleaseResult(
        title="Batman 001 (2016).cbz",
        indexer_name="Dedicated Air",
        download_url=f"airdcpp://client/{config_id}/tth/{tth}",
        size_bytes=100_000_000,
        age_days=None,
        seeders=None,
        leechers=None,
        grabs=None,
        is_torrent=False,
        category=None,
        published_at=None,
        protocol=AcquisitionProtocol.DC,
    )
    validation = ReleaseValidator().validate_all_results(
        [release],
        wanted_series="Batman",
        wanted_issue=1,
        wanted_year=2016,
    )[0][0]
    return DcValidatedCandidate(
        release=release,
        validation=validation,
        route=DcRoute(
            client_config_id=config_id,
            client_identity=f"airdcpp:{config_id}",
            search_instance_id=44,
            grouped_result_id="opaque-result",
            result_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            tth=tth,
            size_bytes=100_000_000,
        ),
        metrics=DcMetrics(2, 1, 2, 1_000_000),
    )


@pytest.mark.asyncio
async def test_manual_direct_connect_grab_persists_exact_client_queue_intent(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    issue_id = await _create_issue(_db_factory)
    async with _db_factory() as session:
        config = DownloadClientConfig(
            name="Dedicated Air",
            client_type=DownloadClientType.AIRDCPP,
            url="http://air.example.test:5600",
            enabled=True,
            priority=20,
        )
        session.add(config)
        await session.flush()
        session.add(AirDcppClientSettings(client_config_id=config.id))
        user_id = (await session.execute(select(User.id))).scalar_one()
        await session.commit()
        config_id = config.id

    token = get_airdcpp_route_token_store().issue(
        _dc_candidate(config_id),
        issue_id=issue_id,
        user_id=user_id,
        search_log_id=None,
    )
    api = AsyncMock()
    api.download_search_result.return_value = SimpleNamespace(id=91, merged=False)
    supervisor = SimpleNamespace(
        state=AirDcppSupervisorState.READY,
        api_client=api,
    )
    registry = SimpleNamespace(get=lambda selected: supervisor if selected == config_id else None)

    with (
        patch(
            "pullbox.api.v1.issues.get_airdcpp_supervisor_registry",
            return_value=registry,
        ),
        patch(
            "pullbox.api.v1.issues.get_settings",
            return_value=SimpleNamespace(airdcpp_enabled=True),
        ),
    ):
        response = await client.post(
            f"/api/v1/issues/{issue_id}/dc-grab",
            json={"dc_route_token": token},
        )

    assert response.status_code == 201
    assert response.json()["bundle_id"] == 91
    api.download_search_result.assert_awaited_once()
    async with _db_factory() as session:
        history = (await session.execute(select(DownloadHistory))).scalar_one()
        assert history.download_client_config_id == config_id
        assert history.state is DownloadState.SENT


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_htmx_search_results_returns_partial(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GET /htmx/issues/{id}/search-results returns 200 with HTML content."""
    issue_id = await _create_issue(_db_factory)

    mock_results = [
        _make_release("Batman 001 (2016).cbz"),
        _make_release("Superman 005 (2020).cbr"),
    ]

    mock_registry = AsyncMock()
    with (
        patch(
            "pullbox.composition.providers.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.services.search_service.SearchService.search_for_issue",
            new_callable=AsyncMock,
            return_value=mock_results,
        ),
    ):
        resp = await client.get(f"/htmx/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    html = resp.text
    assert 'data-testid="issue-search-results"' in html
    assert 'data-testid="issue-search-results-summary"' in html
    assert 'data-testid="issue-search-results-table"' in html
    # Should contain at least one result title
    assert "Batman 001 (2016).cbz" in html


@pytest.mark.asyncio
async def test_confidence_badges_rendered(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """HTML response contains confidence badge CSS classes for matched results."""
    issue_id = await _create_issue(_db_factory)

    mock_results = [
        _make_release("Batman 001 (2016).cbz"),  # HIGH (exact + year)
        _make_release("Batman 001.cbz"),  # MEDIUM (exact, no year)
    ]

    mock_registry = AsyncMock()
    with (
        patch(
            "pullbox.composition.providers.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.services.search_service.SearchService.search_for_issue",
            new_callable=AsyncMock,
            return_value=mock_results,
        ),
    ):
        resp = await client.get(f"/htmx/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    html = resp.text
    assert "pill-success" in html
    assert "pill-warning" in html


@pytest.mark.asyncio
async def test_grab_button_present_for_matched(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Matched results have Grab; rejected results have an explicit override."""
    issue_id = await _create_issue(_db_factory)

    mock_results = [
        _make_release("Batman 001 (2016).cbz", indexer_id=42),  # match
        _make_release("Superman 005 (2020).cbr"),  # reject
    ]

    mock_registry = AsyncMock()
    with (
        patch(
            "pullbox.composition.providers.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.services.search_service.SearchService.search_for_issue",
            new_callable=AsyncMock,
            return_value=mock_results,
        ),
    ):
        resp = await client.get(f"/htmx/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    html = resp.text
    # Matched results should have a grab button
    assert "Grab" in html
    assert "window.issueSearchResultActions" in html
    assert '@click="grabRelease($el)"' in html
    assert '@click="blockRelease($el)"' in html
    assert 'data-indexer-id="42"' in html
    # Rejected results should show rejection info and an explicit manual override
    assert "Rejected" in html
    assert "Grab anyway" in html
    assert '@click="grabRejectedRelease($el)"' in html


@pytest.mark.asyncio
async def test_direct_result_uses_unified_table_and_server_issued_grab_identity(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Direct results share the table without exposing provider download URLs."""
    issue_id = await _create_issue(_db_factory)
    async with _db_factory() as session:
        config = DirectProviderConfig(
            provider_id="pullbox.getcomics",
            display_name="GetComics",
            endpoint="http://getcomics-provider:8780",
            enabled=True,
            priority=10,
            state=DirectProviderState.HEALTHY,
            trust_level=DirectProviderTrustLevel.VERIFIED_PULLBOX,
        )
        session.add(config)
        await session.flush()
        provider_id = config.id
        await session.commit()

    provider = DirectSearchProvider(
        provider_config_id=provider_id,
        provider_identity="pullbox.getcomics",
        display_name="GetComics",
        endpoint="http://getcomics-provider:8780",
        bearer_token="provider-token-with-enough-length",
        allow_private_http=True,
        source_domains=("getcomics.org",),
    )
    candidate = DirectCandidate(
        provider_candidate_id="getcomics:batman-1",
        source_reference="https://getcomics.org/batman-1",
        display_title="Batman 001 (2016)",
        raw_title="Batman 001 (2016) (Digital).cbz",
        parsed=DirectParsedCandidate(
            series_title="Batman",
            issue_numbers=["1"],
            year=2016,
            format="cbz",
            quality="digital",
        ),
        provider_confidence=0.98,
    )
    release = _make_release(candidate.raw_title, indexer_name="GetComics")
    validation = ReleaseValidator().validate_all_results(
        [release],
        wanted_series="Batman",
        wanted_issue=1,
        wanted_year=2016,
    )[0][0]
    direct_outcome = DirectSearchOutcome(
        matched=(DirectValidatedCandidate(provider, candidate, release, validation),),
        rejected=(),
        failures=(),
        providers_searched=1,
        elapsed_ms=5,
    )

    with (
        patch(
            "pullbox.composition.providers.build_registry",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "pullbox.services.direct_search_coordinator.load_direct_search_providers",
            new_callable=AsyncMock,
            return_value=(provider,),
        ),
        patch(
            "pullbox.services.search_service.SearchService._search_direct_safely",
            new_callable=AsyncMock,
            return_value=direct_outcome,
        ),
    ):
        response = await client.get(f"/htmx/issues/{issue_id}/search-results")

    assert response.status_code == 200
    html = response.text
    assert "Source <span" in html
    assert "GetComics" in html
    assert "Direct" in html
    assert "Issue 1" in html
    assert "CBZ" in html
    assert "Digital" in html
    assert re.search(r'data-direct-attempt="\d+"', html)
    assert "direct://candidate/" not in html

    script = Path("src/pullbox/ui/static/js/pullbox.js").read_text(encoding="utf-8")
    assert '"/direct-grab"' in script
    assert "direct_attempt_id" in script


@pytest.mark.asyncio
async def test_empty_results_message(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty search results show a 'no results' message."""
    issue_id = await _create_issue(_db_factory)

    mock_registry = AsyncMock()
    with (
        patch(
            "pullbox.composition.providers.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.services.search_service.SearchService.search_for_issue",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        resp = await client.get(f"/htmx/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    html = resp.text
    assert "No results found" in html
    assert "configured search sources" in html
    assert "indexer settings" not in html
    assert 'data-testid="issue-search-results-empty-state"' in html


@pytest.mark.asyncio
async def test_nonexistent_issue_returns_404(client: AsyncClient) -> None:
    """Missing issue returns 404."""
    resp = await client.get("/htmx/issues/99999/search-results")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_no_indexers_returns_empty_partial(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No configured indexers returns the partial with empty results."""
    issue_id = await _create_issue(_db_factory)

    with patch(
        "pullbox.composition.providers.build_registry",
        new_callable=AsyncMock,
        return_value=None,
    ):
        resp = await client.get(f"/htmx/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    html = resp.text
    assert "No results found" in html


@pytest.mark.asyncio
async def test_low_confidence_badge_rendered(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """LOW confidence fuzzy matches render with orange badge styling."""
    issue_id = await _create_issue(_db_factory)

    mock_results = [
        _make_release("Batmans 001.cbz"),  # fuzzy match, no year → LOW
    ]

    mock_registry = AsyncMock()
    with (
        patch(
            "pullbox.composition.providers.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.services.search_service.SearchService.search_for_issue",
            new_callable=AsyncMock,
            return_value=mock_results,
        ),
    ):
        resp = await client.get(f"/htmx/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    html = resp.text
    assert "pill-warning" in html


@pytest.mark.asyncio
async def test_search_time_displayed(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Search time is rendered with a compact unit in the results header."""
    issue_id = await _create_issue(_db_factory)

    mock_results = [_make_release("Batman 001 (2016).cbz")]

    mock_registry = AsyncMock()
    with (
        patch(
            "pullbox.composition.providers.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.services.search_service.SearchService.search_for_issue",
            new_callable=AsyncMock,
            return_value=mock_results,
        ),
    ):
        resp = await client.get(f"/htmx/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    html = resp.text
    assert re.search(r"\b\d+(?:\.\d)?(?:ms|s)\b", html)


@pytest.mark.asyncio
async def test_torrent_peers_render_as_plain_row_text(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Torrent peers render inline in the row instead of inside a pill."""
    issue_id = await _create_issue(_db_factory)

    mock_results = [
        _make_release("Batman 001 (2016).cbz", is_torrent=True),
    ]

    mock_registry = AsyncMock()
    with (
        patch(
            "pullbox.composition.providers.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.services.search_service.SearchService.search_for_issue",
            new_callable=AsyncMock,
            return_value=mock_results,
        ),
    ):
        resp = await client.get(f"/htmx/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    html = resp.text
    assert "10 / 2" in html
    assert re.search(r'class="pill[^"]*">\s*10 / 2\s*</span>', html) is None


@pytest.mark.asyncio
async def test_rejection_reason_displayed(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rejected results show the specific rejection reason text."""
    issue_id = await _create_issue(_db_factory)

    mock_results = [
        _make_release("Superman 005 (2020).cbr"),  # series mismatch
        _make_release("Batman 001 (2016) covers only.cbz"),  # ignore word
    ]

    mock_registry = AsyncMock()
    with (
        patch(
            "pullbox.composition.providers.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.services.search_service.SearchService.search_for_issue",
            new_callable=AsyncMock,
            return_value=mock_results,
        ),
    ):
        resp = await client.get(f"/htmx/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    html = resp.text
    # Should contain both rejected items
    assert "rejected" in html.lower()
    assert "Superman 005 (2020).cbr" in html


@pytest.mark.asyncio
async def test_result_counts_in_header(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Header shows matched and rejected counts."""
    issue_id = await _create_issue(_db_factory)

    mock_results = [
        _make_release("Batman 001 (2016).cbz"),  # match
        _make_release("Superman 005 (2020).cbr"),  # reject
        _make_release("Random Garbage"),  # reject
    ]

    mock_registry = AsyncMock()
    with (
        patch(
            "pullbox.composition.providers.build_registry",
            new_callable=AsyncMock,
            return_value=(mock_registry, {}),
        ),
        patch(
            "pullbox.services.search_service.SearchService.search_for_issue",
            new_callable=AsyncMock,
            return_value=mock_results,
        ),
    ):
        resp = await client.get(f"/htmx/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    html = resp.text
    assert 'data-testid="issue-search-results-summary"' in html
    assert re.search(r">\s*1\s*</span>\s*matched", html)
    assert re.search(r">\s*2\s*</span>\s*rejected", html)

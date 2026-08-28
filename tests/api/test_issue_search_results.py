"""Tests for the interactive search results endpoint and validate_all_results().

Verifies:
- ReleaseValidator.validate_all_results() returns both matched and rejected lists
- Matched results are sorted by confidence descending
- GET /api/v1/issues/{id}/search-results returns both arrays with full context
- Rejected results include rejection reasons
- Matched results include match details
- Auto-grabbable flag works correctly
- Empty results handled gracefully
- Nonexistent issue returns 404

Run:
    pytest tests/api/test_issue_search_results.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactFailureClass,
    DirectProviderConfig,
    DirectProviderState,
    DirectProviderTrustLevel,
)
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import MatchConfidence
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import APIKey, User
from pullbox.providers.base import ReleaseResult
from pullbox.providers.direct.contract import DirectCandidate, DirectParsedCandidate
from pullbox.services.auth_service import AuthService
from pullbox.services.direct_acquisition_planner_service import DirectAcquisitionPlanningError
from pullbox.services.direct_search_coordinator import (
    DirectSearchDiscovery,
    DirectSearchOutcome,
    DirectSearchProvider,
    DirectValidatedCandidate,
)
from pullbox.services.release_validator import ReleaseValidator
from pullbox.services.search_service import IssueSearchTarget

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-search-results")


# ── Test Data ──────────────────────────────────────────────────────────


def _make_release(
    title: str,
    indexer_name: str = "NZBgeek",
    *,
    size_bytes: int | None = 100_000_000,
    age_days: int | None = 5,
    is_torrent: bool = False,
    ranking_priority: int = 25,
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
        ranking_priority=ranking_priority,
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
    raw_key = "pb_k1_" + "d" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="searchuser",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="search-test"))
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


# ── Unit Tests: validate_all_results() ─────────────────────────────────


@pytest.mark.asyncio
async def test_validate_all_returns_matched_and_rejected() -> None:
    """Feed mixed results — verify both matched and rejected lists populated."""
    validator = ReleaseValidator()

    results = [
        _make_release("Batman 001 (2016).cbz"),  # match
        _make_release("Superman 005 (2020).cbr"),  # reject: series mismatch
        _make_release("Batman 001 (2016) covers only.cbz"),  # reject: ignore word
        _make_release("Batman 002 (2016).cbz"),  # reject: issue mismatch
    ]

    matched, rejected = validator.validate_all_results(
        results,
        wanted_series="Batman",
        wanted_issue=1.0,
        wanted_year=2016,
    )

    assert len(matched) == 1
    assert len(rejected) == 3
    assert matched[0].is_match is True
    assert all(not r.is_match for r in rejected)


@pytest.mark.asyncio
async def test_validate_all_matched_sorted_by_confidence() -> None:
    """Matched list should be sorted HIGH > MEDIUM > LOW."""
    validator = ReleaseValidator()

    results = [
        # Fuzzy match with no year → LOW confidence
        _make_release("Batmans 001.cbz"),
        # Exact match with year → HIGH confidence
        _make_release("Batman 001 (2016).cbz"),
        # Exact match without year → MEDIUM confidence
        _make_release("Batman 001.cbz"),
    ]

    matched, _rejected = validator.validate_all_results(
        results,
        wanted_series="Batman",
        wanted_issue=1.0,
        wanted_year=2016,
    )

    assert len(matched) >= 2
    confidences = [v.confidence for v in matched]
    confidence_order = [MatchConfidence.HIGH, MatchConfidence.MEDIUM, MatchConfidence.LOW]
    # Verify ordering: each confidence should come before or equal to the next
    for i in range(len(confidences) - 1):
        assert confidence_order.index(confidences[i]) <= confidence_order.index(confidences[i + 1])


# ── Unit Tests: build_interactive_results() ────────────────────────────


@pytest.mark.asyncio
async def test_build_interactive_results_matched_and_rejected() -> None:
    """build_interactive_results() creates correct schema objects from ValidationResults."""
    from pullbox.api.v1.issues import build_interactive_results
    from pullbox.schemas.search import InteractiveSearchIssue, InteractiveSearchResponse

    validator = ReleaseValidator()
    results = [
        _make_release("Batman 001 (2016).cbz"),
        _make_release("Superman 005 (2020).cbr"),
    ]

    matched_vr, rejected_vr = validator.validate_all_results(
        results,
        wanted_series="Batman",
        wanted_issue=1.0,
        wanted_year=2016,
    )

    matched_items, rejected_items = build_interactive_results(matched_vr, rejected_vr, {})

    # Wrap in response to verify full serialization
    response = InteractiveSearchResponse(
        issue=InteractiveSearchIssue(
            id=1,
            series_title="Batman",
            issue_number=1.0,
            issue_type="issue",
            year=2016,
        ),
        matched=matched_items,
        rejected=rejected_items,
        search_time_ms=42,
    )

    assert len(response.matched) == 1
    assert len(response.rejected) == 1
    assert response.matched[0].confidence == "high"
    assert response.matched[0].auto_grabbable is True
    assert response.matched[0].quality_score > 0
    assert response.matched[0].match_details.parsed_series == "Batman"
    assert response.matched[0].match_details.parsed_issue == 1.0
    assert response.matched[0].match_details.series_similarity > 0.9
    assert response.rejected[0].rejection_reason is not None
    assert response.rejected[0].confidence is None
    assert response.rejected[0].download_url == results[1].download_url
    assert response.rejected[0].indexer_name == results[1].indexer_name
    assert response.rejected[0].is_torrent == results[1].is_torrent
    assert response.search_time_ms == 42

    # Verify JSON serialization round-trips
    data = response.model_dump()
    assert data["issue"]["series_title"] == "Batman"
    assert isinstance(data["matched"][0]["quality_score"], float)
    assert data["rejected"][0]["download_url"] == results[1].download_url


async def test_build_direct_interactive_results_uses_attempt_identity_not_url() -> None:
    from pullbox.api.v1.issues import build_direct_interactive_results

    provider = DirectSearchProvider(
        provider_config_id=9,
        provider_identity="pullbox.getcomics",
        display_name="GetComics",
        endpoint="http://getcomics-provider:8780",
        bearer_token="provider-token-with-enough-length",
        allow_private_http=True,
    )
    candidate = DirectCandidate(
        provider_candidate_id="getcomics:batman-1",
        source_reference="https://getcomics.org/batman-1",
        display_title="Batman 001 (2016) (Digital)",
        raw_title="Batman 001 (2016) (Digital).cbz",
        parsed=DirectParsedCandidate(
            series_title="Batman",
            issue_numbers=["1"],
            year=2016,
            format="cbz",
            quality="digital",
        ),
        provider_confidence=0.97,
    )
    release = _make_release(
        "Batman 001 (2016) (Digital).cbz",
        indexer_name="GetComics",
    )
    validation = ReleaseValidator().validate_all_results(
        [release],
        wanted_series="Batman",
        wanted_issue=1,
        wanted_year=2016,
    )[0][0]
    discovery = DirectSearchDiscovery(
        attempt_id=42,
        result=DirectValidatedCandidate(provider, candidate, release, validation),
    )

    matched, rejected = build_direct_interactive_results(
        (discovery,),
        eval_kwargs={},
        issue_type=IssueType.ISSUE,
    )

    assert rejected == []
    assert matched[0].source_kind == "direct"
    assert matched[0].method == "Direct"
    assert matched[0].direct_attempt_id == 42
    assert matched[0].download_url is None
    assert matched[0].coverage == ["1"]
    assert matched[0].format == "cbz"
    assert matched[0].quality == "digital"
    assert "getcomics.org" not in repr(matched[0])


async def test_build_direct_interactive_results_hides_fingerprint_alternates() -> None:
    from pullbox.api.v1.issues import build_direct_interactive_results

    provider = DirectSearchProvider(
        provider_config_id=9,
        provider_identity="pullbox.libgen",
        display_name="LibGen",
        endpoint="http://libgen-provider:8780",
        bearer_token="provider-token-with-enough-length",
        allow_private_http=True,
    )
    candidate = DirectCandidate(
        provider_candidate_id="libgen:batman-1",
        source_reference="https://libgen.gl/book/1",
        display_title="Batman 001 (2016) (Digital)",
        raw_title="Batman 001 (2016) (Digital).cbz",
        parsed=DirectParsedCandidate(
            series_title="Batman",
            issue_numbers=["1"],
            year=2016,
            format="cbz",
        ),
        provider_confidence=0.97,
    )
    release = _make_release("Batman 001 (2016) (Digital).cbz", indexer_name="LibGen")
    validation = ReleaseValidator().validate_all_results(
        [release],
        wanted_series="Batman",
        wanted_issue=1,
        wanted_year=2016,
    )[0][0]
    hidden = DirectSearchDiscovery(
        attempt_id=43,
        result=DirectValidatedCandidate(provider, candidate, release, validation),
        visible=False,
    )

    matched, rejected = build_direct_interactive_results(
        (hidden,),
        eval_kwargs={},
        issue_type=IssueType.ISSUE,
    )

    assert matched == []
    assert rejected == []


def test_interactive_results_respect_direct_source_priority() -> None:
    from pullbox.api.v1.issues import (
        build_interactive_results,
        sort_interactive_results_by_source_priority,
    )

    release = _make_release("Batman 001 (2016) (Digital).cbz")
    validation = ReleaseValidator().validate_all_results(
        [release],
        wanted_series="Batman",
        wanted_issue=1,
        wanted_year=2016,
    )[0][0]
    indexer_item = build_interactive_results([validation], [], {})[0][0]
    direct_item = indexer_item.model_copy(
        update={
            "indexer_name": "GetComics",
            "source_kind": "direct",
            "method": "Direct",
        }
    )

    ordered = sort_interactive_results_by_source_priority(
        [indexer_item, direct_item],
        ["direct", "usenet", "torrent"],
    )

    assert [item.source_kind for item in ordered] == ["direct", "indexer"]


def test_interactive_direct_results_prefer_quality_before_provider_priority() -> None:
    from pullbox.api.v1.issues import (
        build_interactive_results,
        sort_interactive_results_by_source_priority,
    )

    getcomics_release = _make_release(
        "Batman 001 (2016)",
        "GetComics",
        size_bytes=None,
        age_days=None,
        ranking_priority=10,
    )
    annas_release = _make_release(
        "Batman 001 (2016) (Digital).cbz",
        "Anna's Archive",
        size_bytes=None,
        age_days=None,
        ranking_priority=20,
    )
    matched, rejected = ReleaseValidator().validate_all_results(
        [getcomics_release, annas_release],
        wanted_series="Batman",
        wanted_issue=1,
        wanted_year=2016,
    )
    items, _ = build_interactive_results(
        matched,
        rejected,
        {},
        source_priority=["direct", "usenet", "torrent"],
        scoring_priority=25,
    )
    direct_items = [item.model_copy(update={"source_kind": "direct"}) for item in items]

    # Provider priority is a final tie-breaker, so the richer candidate keeps
    # its lead after semantic and quality scoring.
    assert direct_items[0].indexer_name == "Anna's Archive"
    ordered = sort_interactive_results_by_source_priority(
        direct_items,
        ["direct", "usenet", "torrent"],
    )

    assert [item.indexer_name for item in ordered] == ["Anna's Archive", "GetComics"]
    assert "ranking_priority" not in ordered[0].model_dump()


def test_interactive_direct_results_use_provider_priority_for_quality_ties() -> None:
    from pullbox.api.v1.issues import (
        build_interactive_results,
        sort_interactive_results_by_source_priority,
    )

    lower_priority = _make_release(
        "Batman 001 (2016) (Digital).cbz",
        "LibGen",
        ranking_priority=20,
    )
    preferred = _make_release(
        "Batman 001 (2016) (Digital).cbz",
        "GetComics",
        ranking_priority=10,
    )
    matched, rejected = ReleaseValidator().validate_all_results(
        [lower_priority, preferred],
        wanted_series="Batman",
        wanted_issue=1,
        wanted_year=2016,
    )
    items, _ = build_interactive_results(matched, rejected, {}, scoring_priority=25)
    direct_items = [item.model_copy(update={"source_kind": "direct"}) for item in items]

    ordered = sort_interactive_results_by_source_priority(
        direct_items,
        ["direct", "usenet", "torrent"],
    )

    assert [item.indexer_name for item in ordered] == ["GetComics", "LibGen"]


@pytest.mark.asyncio
async def test_build_interactive_results_auto_grabbable_logic() -> None:
    """Interactive auto-grabbable mirrors per-type automated routing thresholds."""
    from pullbox.api.v1.issues import build_interactive_results

    validator = ReleaseValidator()
    results = [
        _make_release("Batman 001 (2016).cbz"),  # HIGH
        _make_release("Batman 001.cbz"),  # MEDIUM
    ]

    matched_vr, rejected_vr = validator.validate_all_results(
        results,
        wanted_series="Batman",
        wanted_issue=1.0,
        wanted_year=2016,
    )

    matched_items, _ = build_interactive_results(
        matched_vr,
        rejected_vr,
        {},
        issue_type=IssueType.ISSUE,
        type_thresholds={"issue": "medium"},
    )

    high_items = [m for m in matched_items if m.confidence == "high"]
    medium_items = [m for m in matched_items if m.confidence == "medium"]

    assert len(high_items) >= 1
    for item in high_items:
        assert item.auto_grabbable is True
        assert item.quality_score > 10

    assert len(medium_items) >= 1
    for item in medium_items:
        assert item.auto_grabbable is True

    tpb_items, _ = build_interactive_results(
        matched_vr,
        rejected_vr,
        {},
        issue_type=IssueType.TPB,
        type_thresholds={"tpb": "high"},
    )
    for item in [m for m in tpb_items if m.confidence == "medium"]:
        assert item.auto_grabbable is False


@pytest.mark.asyncio
async def test_build_interactive_results_empty_inputs() -> None:
    """Empty validation results produce empty item lists."""
    from pullbox.api.v1.issues import build_interactive_results

    matched_items, rejected_items = build_interactive_results([], [], {})
    assert matched_items == []
    assert rejected_items == []


@pytest.mark.asyncio
async def test_build_interactive_results_custom_eval_kwargs() -> None:
    """Custom eval_kwargs (min_score, confidence_blend) affect scoring."""
    from pullbox.api.v1.issues import build_interactive_results

    validator = ReleaseValidator()
    results = [_make_release("Batman 001 (2016).cbz")]

    matched_vr, rejected_vr = validator.validate_all_results(
        results,
        wanted_series="Batman",
        wanted_issue=1.0,
        wanted_year=2016,
    )

    # With default kwargs
    items_default, _ = build_interactive_results(matched_vr, rejected_vr, {})

    # With high min_score — should still be auto_grabbable since it's HIGH
    items_custom, _ = build_interactive_results(matched_vr, rejected_vr, {"confidence_blend": 0.80})

    # Different blend should produce different quality scores
    assert items_default[0].quality_score != items_custom[0].quality_score


def test_interactive_results_rank_indexers_within_the_selected_source_lane() -> None:
    from pullbox.api.v1.issues import build_interactive_results

    lower_priority = _make_release(
        "Batman 001 (2016) (Digital).cbz",
        "Lower priority",
        ranking_priority=50,
    )
    higher_priority = _make_release(
        "Batman 001 (2016) (Digital).cbz",
        "Higher priority",
        ranking_priority=1,
    )
    matched, rejected = ReleaseValidator().validate_all_results(
        [lower_priority, higher_priority],
        wanted_series="Batman",
        wanted_issue=1,
        wanted_year=2016,
    )

    items, _ = build_interactive_results(
        matched,
        rejected,
        {},
        source_priority=["usenet", "torrent", "direct"],
    )

    assert [item.indexer_name for item in items] == ["Higher priority", "Lower priority"]


# ── API Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_matched_and_rejected(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """HTTP endpoint returns both matched and rejected arrays."""
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
        resp = await client.get(f"/api/v1/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    data = resp.json()
    assert "matched" in data
    assert "rejected" in data
    assert len(data["matched"]) == 1
    assert len(data["rejected"]) == 1
    assert data["issue"]["id"] == issue_id
    assert "search_time_ms" in data
    assert "details" not in data
    assert "search_details" not in data
    assert "top_rejected" not in data
    assert "rejected_diagnostics_count" not in data
    for item in [*data["matched"], *data["rejected"]]:
        assert "query" not in item
        assert "reason_summary" not in item


async def test_direct_only_search_persists_server_identity_without_download_url(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
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
        response = await client.get(f"/api/v1/issues/{issue_id}/search-results")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["matched"]) == 1
    item = payload["matched"][0]
    assert item["source_kind"] == "direct"
    assert item["download_url"] is None
    assert isinstance(item["direct_attempt_id"], int)
    assert payload["search_log_id"] is not None

    async with _db_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, item["direct_attempt_id"])
        assert attempt is not None
        assert attempt.search_log_id == payload["search_log_id"]
        assert "getcomics.org" not in repr(attempt.candidate_snapshot)


async def test_direct_grab_plans_commits_and_dispatches_ephemeral_source(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
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
        attempt = DirectAcquisitionAttempt(
            request_key="direct-grab-api-test",
            issue_id=issue_id,
            provider_config_id=config.id,
            provider_identity=config.provider_id,
            provider_candidate_id="candidate-1",
            state=DirectAcquisitionState.DISCOVERED,
            requested_coverage={"issue_numbers": ["1"]},
            candidate_snapshot={"display_title": "Batman 001 (2016)"},
            plan_snapshot={},
            progress_snapshot={},
        )
        session.add(attempt)
        await session.flush()
        attempt_id = attempt.id
        await session.commit()

    ephemeral_source = object()

    async def plan(session: AsyncSession, *, acquisition_id: int, **_kwargs: object):
        planned_attempt = await session.get(DirectAcquisitionAttempt, acquisition_id)
        assert planned_attempt is not None
        planned_attempt.state = DirectAcquisitionState.PLANNED
        return SimpleNamespace(
            attempt=planned_attempt,
            selected_artifact=SimpleNamespace(id=77),
            initial_source=ephemeral_source,
        )

    runner = SimpleNamespace(dispatch=AsyncMock(return_value=True))
    with (
        patch("pullbox.api.v1.issues.plan_direct_acquisition", side_effect=plan),
        patch(
            "pullbox.api.v1.issues.get_direct_acquisition_runner",
            return_value=runner,
        ),
    ):
        response = await client.post(
            f"/api/v1/issues/{issue_id}/direct-grab",
            json={"direct_attempt_id": attempt_id},
        )

    assert response.status_code == 201
    assert response.json() == {
        "issue_id": issue_id,
        "acquisition_id": attempt_id,
        "artifact_id": 77,
        "title": "Batman 001 (2016)",
        "status": "queued",
    }
    runner.dispatch.assert_awaited_once_with(
        attempt_id,
        77,
        initial_source=ephemeral_source,
    )


async def test_direct_grab_uses_hidden_provider_fallback_when_primary_cannot_plan(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    issue_id = await _create_issue(_db_factory)
    async with _db_factory() as session:
        primary_config = DirectProviderConfig(
            provider_id="pullbox.getcomics",
            display_name="GetComics",
            endpoint="http://getcomics-provider:8780",
            enabled=True,
            priority=10,
            state=DirectProviderState.HEALTHY,
            trust_level=DirectProviderTrustLevel.VERIFIED_PULLBOX,
        )
        alternate_config = DirectProviderConfig(
            provider_id="pullbox.libgen",
            display_name="LibGen",
            endpoint="http://libgen-provider:8780",
            enabled=True,
            priority=20,
            state=DirectProviderState.HEALTHY,
            trust_level=DirectProviderTrustLevel.VERIFIED_PULLBOX,
        )
        session.add_all([primary_config, alternate_config])
        await session.flush()
        primary = DirectAcquisitionAttempt(
            request_key="direct-grab-primary",
            issue_id=issue_id,
            provider_config_id=primary_config.id,
            provider_identity=primary_config.provider_id,
            provider_candidate_id="candidate-primary",
            state=DirectAcquisitionState.DISCOVERED,
            requested_coverage={"issue_numbers": ["1"]},
            candidate_snapshot={"display_title": "Batman 001 (2016)", "visible": True},
            plan_snapshot={},
            progress_snapshot={},
        )
        alternate = DirectAcquisitionAttempt(
            request_key="direct-grab-alternate",
            issue_id=issue_id,
            provider_config_id=alternate_config.id,
            provider_identity=alternate_config.provider_id,
            provider_candidate_id="candidate-alternate",
            state=DirectAcquisitionState.DISCOVERED,
            requested_coverage={"issue_numbers": ["1"]},
            candidate_snapshot={"display_title": "Batman 001 (2016)", "visible": False},
            plan_snapshot={},
            progress_snapshot={},
        )
        session.add_all([primary, alternate])
        await session.flush()
        primary.candidate_snapshot = {
            **primary.candidate_snapshot,
            "alternate_attempt_ids": [alternate.id],
        }
        primary_id = primary.id
        alternate_id = alternate.id
        await session.commit()

    ephemeral_source = object()

    async def plan(session: AsyncSession, *, acquisition_id: int, **_kwargs: object):
        planned_attempt = await session.get(DirectAcquisitionAttempt, acquisition_id)
        assert planned_attempt is not None
        if acquisition_id == primary_id:
            planned_attempt.state = DirectAcquisitionState.FAILED
            raise DirectAcquisitionPlanningError(
                "provider_resolve_unavailable",
                "The provider is unavailable.",
                failure_class=DirectArtifactFailureClass.PROVIDER_UNAVAILABLE,
            )
        planned_attempt.state = DirectAcquisitionState.PLANNED
        return SimpleNamespace(
            attempt=planned_attempt,
            selected_artifact=SimpleNamespace(id=99),
            initial_source=ephemeral_source,
        )

    runner = SimpleNamespace(dispatch=AsyncMock(return_value=True))
    with (
        patch("pullbox.api.v1.issues.plan_direct_acquisition", side_effect=plan),
        patch(
            "pullbox.api.v1.issues.get_direct_acquisition_runner",
            return_value=runner,
        ),
    ):
        response = await client.post(
            f"/api/v1/issues/{issue_id}/direct-grab",
            json={"direct_attempt_id": primary_id},
        )

    assert response.status_code == 201
    assert response.json()["acquisition_id"] == alternate_id
    runner.dispatch.assert_awaited_once_with(
        alternate_id,
        99,
        initial_source=ephemeral_source,
    )


@pytest.mark.asyncio
async def test_rejected_includes_reason(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Each rejected result has a rejection_reason string."""
    issue_id = await _create_issue(_db_factory)

    mock_results = [
        _make_release("Superman 005 (2020).cbr"),
        _make_release("Random Garbage Title"),
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
        resp = await client.get(f"/api/v1/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    data = resp.json()
    for rejected in data["rejected"]:
        assert "rejection_reason" in rejected
        assert isinstance(rejected["rejection_reason"], str)
        assert len(rejected["rejection_reason"]) > 0


@pytest.mark.asyncio
async def test_includes_match_details(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Each matched result has parsed_series, parsed_issue, series_similarity."""
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
        resp = await client.get(f"/api/v1/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["matched"]) == 1
    details = data["matched"][0]["match_details"]
    assert details["parsed_series"] is not None
    assert details["parsed_issue"] is not None
    assert details["series_similarity"] > 0


@pytest.mark.asyncio
async def test_auto_grabbable_flag(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Auto-grabbable reflects the standard issue type's medium threshold."""
    issue_id = await _create_issue(_db_factory)

    mock_results = [
        _make_release("Batman 001 (2016).cbz"),  # HIGH confidence (exact + year)
        _make_release("Batman 001.cbz"),  # MEDIUM confidence (exact, no year)
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
        resp = await client.get(f"/api/v1/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["matched"]) == 2

    high_results = [m for m in data["matched"] if m["confidence"] == "high"]
    medium_results = [m for m in data["matched"] if m["confidence"] == "medium"]

    assert len(high_results) >= 1
    for r in high_results:
        assert r["auto_grabbable"] is True

    assert len(medium_results) >= 1
    for r in medium_results:
        assert r["auto_grabbable"] is True


@pytest.mark.asyncio
async def test_no_results_returns_empty(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty indexer response → empty matched and rejected arrays."""
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
        resp = await client.get(f"/api/v1/issues/{issue_id}/search-results")

    assert resp.status_code == 200
    data = resp.json()
    assert data["matched"] == []
    assert data["rejected"] == []
    assert data["search_time_ms"] >= 0


@pytest.mark.asyncio
async def test_nonexistent_issue_returns_404(client: AsyncClient) -> None:
    """Missing issue returns 404."""
    resp = await client.get("/api/v1/issues/99999/search-results")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_issue_interactive_search_ignores_indexer_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.api.v1 import issues as issues_api

    target = IssueSearchTarget(
        issue_id=1,
        series_id=2,
        series_title="Infernal Hulk",
        issue_number=1,
        issue_type=IssueType.ISSUE,
        series_year=2026,
    )
    runtime = SimpleNamespace(
        registry=object(),
        failure_threshold=3,
        indexer_configs={},
        eval_kwargs={},
        validator_kwargs={},
        source_priority=None,
        type_thresholds={},
        two_pass_enabled=False,
        direct_providers=(),
    )
    constructor_args: dict[str, object] = {}

    class FakeSearchService:
        def __init__(self, **kwargs: object) -> None:
            constructor_args.update(kwargs)

        async def search_issue_target(self, *args: object, **kwargs: object) -> object:
            return SimpleNamespace(
                matched=[object()],
                rejected=[],
                direct_outcome=None,
                search_details={},
            )

    session = AsyncMock()
    monkeypatch.setattr(issues_api, "load_issue_search_target", AsyncMock(return_value=target))
    monkeypatch.setattr(issues_api, "build_search_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr(issues_api, "SearchService", FakeSearchService)
    monkeypatch.setattr(issues_api, "build_interactive_results", lambda *args, **kwargs: ([], []))

    await issues_api._run_issue_search(session, target.issue_id, include_download_clients=False)

    assert constructor_args["ignore_indexer_backoff"] is True

"""Planning direct candidates into restart-safe acquisition attempts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactFailureClass,
    DirectArtifactHostKind,
    DirectArtifactState,
    DirectHostAccountState,
    DirectHostConfig,
    DirectProviderConfig,
    DirectProviderState,
    DirectResolverKind,
)
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.direct.client import DirectProviderClientError
from pullbox.providers.direct.contract import (
    DirectArtifact,
    DirectArtifactCoverage,
    DirectArtifactRoute,
    DirectMirror,
    DirectQuotaStatus,
    DirectResolveResponse,
    DirectResolverProfile,
)
from pullbox.services.blocklist_service import BlocklistService
from pullbox.services.direct_acquisition_planner_service import (
    DirectAcquisitionPlanningError,
    direct_route_identity,
    plan_direct_acquisition,
    plan_direct_acquisition_with_provider_fallback,
    resolve_planned_artifact_source,
)
from pullbox.services.direct_resolver_service import ProviderResolverOption

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)


async def test_hidden_provider_alternate_is_planned_after_primary_failure(
    session: AsyncSession,
) -> None:
    alternate_provider = DirectProviderConfig(
        id=2,
        provider_id="community.libgen",
        display_name="LibGen",
        endpoint="http://libgen:8080",
        enabled=True,
        priority=20,
        state=DirectProviderState.HEALTHY,
    )
    primary = DirectAcquisitionAttempt(
        request_key="primary-fingerprint-result",
        issue_id=1,
        provider_config_id=1,
        provider_identity="community.getcomics",
        provider_candidate_id="primary-candidate",
        state=DirectAcquisitionState.DISCOVERED,
        requested_coverage={"issue_numbers": ["1"]},
        candidate_snapshot={"display_title": "Planner Series 001", "visible": True},
        plan_snapshot={},
        progress_snapshot={},
    )
    alternate = DirectAcquisitionAttempt(
        request_key="alternate-fingerprint-result",
        issue_id=1,
        provider_config_id=2,
        provider_identity="community.libgen",
        provider_candidate_id="alternate-candidate",
        state=DirectAcquisitionState.DISCOVERED,
        requested_coverage={"issue_numbers": ["1"]},
        candidate_snapshot={"display_title": "Planner Series 001", "visible": False},
        plan_snapshot={},
        progress_snapshot={},
    )
    session.add_all([alternate_provider, primary, alternate])
    await session.flush()
    primary.candidate_snapshot = {
        **primary.candidate_snapshot,
        "alternate_attempt_ids": [alternate.id],
    }
    alternate.candidate_snapshot = {
        **alternate.candidate_snapshot,
        "primary_attempt_id": primary.id,
    }
    planned_artifact = SimpleNamespace(id=88)
    calls: list[int] = []

    async def planner(
        _session: AsyncSession,
        *,
        acquisition_id: int,
        **_kwargs: object,
    ) -> object:
        calls.append(acquisition_id)
        if acquisition_id == primary.id:
            primary.state = DirectAcquisitionState.FAILED
            raise DirectAcquisitionPlanningError(
                "provider_resolve_unavailable",
                "The provider is unavailable.",
                failure_class=DirectArtifactFailureClass.PROVIDER_UNAVAILABLE,
            )
        alternate.state = DirectAcquisitionState.PLANNED
        return SimpleNamespace(
            attempt=alternate,
            selected_artifact=planned_artifact,
            initial_source=object(),
        )

    result = await plan_direct_acquisition_with_provider_fallback(
        session,
        acquisition_id=primary.id,
        planner=planner,
    )

    assert result.attempt is alternate
    assert calls == [primary.id, alternate.id]
    assert primary.progress_snapshot["fallback_attempt_id"] == alternate.id
    assert primary.progress_snapshot["fallback_provider_identity"] == "community.libgen"
    assert alternate.progress_snapshot["fallback_from_attempt_id"] == primary.id
    assert alternate.progress_snapshot["fallback_from_provider_identity"] == "community.getcomics"


async def test_transfer_recovery_skips_the_already_failed_primary_provider(
    session: AsyncSession,
) -> None:
    alternate_provider = DirectProviderConfig(
        id=2,
        provider_id="community.libgen",
        display_name="LibGen",
        endpoint="http://libgen:8080",
        enabled=True,
        priority=20,
        state=DirectProviderState.HEALTHY,
    )
    primary = DirectAcquisitionAttempt(
        request_key="failed-primary-fingerprint-result",
        issue_id=1,
        provider_config_id=1,
        provider_identity="community.getcomics",
        provider_candidate_id="failed-primary-candidate",
        state=DirectAcquisitionState.FAILED,
        failure_class=DirectArtifactFailureClass.PROVIDER_UNAVAILABLE,
        requested_coverage={"issue_numbers": ["1"]},
        candidate_snapshot={"display_title": "Planner Series 001", "visible": True},
        plan_snapshot={},
        progress_snapshot={},
    )
    alternate = DirectAcquisitionAttempt(
        request_key="recovery-alternate-fingerprint-result",
        issue_id=1,
        provider_config_id=2,
        provider_identity="community.libgen",
        provider_candidate_id="recovery-alternate-candidate",
        state=DirectAcquisitionState.DISCOVERED,
        requested_coverage={"issue_numbers": ["1"]},
        candidate_snapshot={"display_title": "Planner Series 001", "visible": False},
        plan_snapshot={},
        progress_snapshot={},
    )
    session.add_all([alternate_provider, primary, alternate])
    await session.flush()
    primary.candidate_snapshot = {
        **primary.candidate_snapshot,
        "alternate_attempt_ids": [alternate.id],
    }
    alternate.candidate_snapshot = {
        **alternate.candidate_snapshot,
        "primary_attempt_id": primary.id,
    }
    calls: list[int] = []

    async def planner(
        _session: AsyncSession,
        *,
        acquisition_id: int,
        **_kwargs: object,
    ) -> object:
        calls.append(acquisition_id)
        if acquisition_id == primary.id:
            raise AssertionError("recovery retried the failed primary provider")
        alternate.state = DirectAcquisitionState.PLANNED
        return SimpleNamespace(
            attempt=alternate,
            selected_artifact=SimpleNamespace(id=89),
            initial_source=object(),
        )

    result = await plan_direct_acquisition_with_provider_fallback(
        session,
        acquisition_id=primary.id,
        planner=planner,
        skip_selected_attempt=True,
    )

    assert result.attempt is alternate
    assert calls == [alternate.id]


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        series = Series(
            id=1,
            comicvine_id=800_001,
            title="Planner Series",
            sort_title="Planner Series",
            year_start=2026,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        db_session.add(series)
        db_session.add(
            Issue(
                id=1,
                series_id=1,
                comicvine_id=800_002,
                issue_number=1,
                issue_type=IssueType.ISSUE,
                status=IssueStatus.WANTED,
            )
        )
        provider = DirectProviderConfig(
            id=1,
            provider_id="community.getcomics",
            display_name="GetComics",
            endpoint="http://provider:8080",
            enabled=True,
            priority=10,
            state=DirectProviderState.HEALTHY,
            negotiated_protocol="direct-download-provider/v1",
            encrypted_bearer_token="unused-in-test",
            configuration_metadata={"allow_private_http": True, "public_values": {}},
            manifest_snapshot={"source_domains": ["getcomics.org"]},
        )
        db_session.add(provider)
        db_session.add_all(
            [
                DirectHostConfig(
                    host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
                    enabled=True,
                    preference=20,
                    account_state=DirectHostAccountState.NOT_CONFIGURED,
                ),
                DirectHostConfig(
                    host_kind=DirectArtifactHostKind.PIXELDRAIN,
                    enabled=True,
                    preference=10,
                    account_state=DirectHostAccountState.HEALTHY,
                ),
                DirectHostConfig(
                    host_kind=DirectArtifactHostKind.TERABOX,
                    enabled=True,
                    preference=5,
                    account_state=DirectHostAccountState.AUTHENTICATION_REQUIRED,
                ),
            ]
        )
        db_session.add(
            DirectAcquisitionAttempt(
                id=1,
                request_key="direct-search:test",
                issue_id=1,
                provider_config_id=1,
                provider_identity="community.getcomics",
                provider_candidate_id="candidate-1",
                state=DirectAcquisitionState.DISCOVERED,
                requested_coverage={"issue_numbers": ["1"], "issue_type": "issue"},
                candidate_snapshot={"display_title": "Planner Series 001 (2026)"},
                plan_snapshot={},
                progress_snapshot={"stage": "discovered"},
            )
        )
        await db_session.commit()
        yield db_session
    await engine.dispose()


def _response(*, reverse: bool = False) -> DirectResolveResponse:
    mirrors = [
        DirectMirror(
            mirror_id="generic-mirror",
            host_kind="generic_https",
            final_url="https://files.example.test/signed.cbz?token=hidden",
            size_bytes=100,
        ),
        DirectMirror(
            mirror_id="pixel-mirror",
            host_kind="pixeldrain",
            share_url="https://pixeldrain.com/u/abc123",
            size_bytes=100,
            checksum="md5:11111111111111111111111111111111",
        ),
        DirectMirror(
            mirror_id="terabox-mirror",
            host_kind="terabox",
            share_url="https://terabox.com/s/example",
            size_bytes=100,
        ),
    ]
    if reverse:
        mirrors.reverse()
    return DirectResolveResponse(
        protocol_version="direct-download-provider/v1",
        request_id="00000000-0000-0000-0000-000000000001",
        artifacts=[
            DirectArtifact(
                artifact_id="provider-artifact-1",
                coverage=DirectArtifactCoverage(issue_numbers=["1"]),
                route=DirectArtifactRoute.DIRECT_ARTIFACT,
                format="cbz",
                quality="digital",
                size_bytes=100,
                mirrors=mirrors,
            )
        ],
    )


def _generic_response() -> DirectResolveResponse:
    return DirectResolveResponse(
        protocol_version="direct-download-provider/v1",
        request_id="00000000-0000-0000-0000-000000000001",
        artifacts=[
            DirectArtifact(
                artifact_id="provider-artifact-1",
                coverage=DirectArtifactCoverage(issue_numbers=["1"]),
                route=DirectArtifactRoute.DIRECT_ARTIFACT,
                format="cbz",
                quality="digital",
                size_bytes=100,
                mirrors=[
                    DirectMirror(
                        mirror_id="generic-mirror",
                        host_kind="generic_https",
                        final_url="https://files.example.test/signed.cbz?token=hidden",
                        size_bytes=100,
                    )
                ],
            )
        ],
    )


class _ResolveClient:
    def __init__(self, response: DirectResolveResponse) -> None:
        self.response = response
        self.requests: list[Any] = []
        self.closed = False

    async def resolve(self, request: Any) -> DirectResolveResponse:
        self.requests.append(request)
        return self.response.model_copy(update={"request_id": request.request_id})

    async def aclose(self) -> None:
        self.closed = True


class _FallbackResolveClient(_ResolveClient):
    async def resolve(self, request: Any) -> DirectResolveResponse:
        self.requests.append(request)
        profile = request.resolver_profile
        if profile is None:
            raise DirectProviderClientError(
                "browser_challenge_required",
                "Browser challenge required.",
                retryable=True,
            )
        if profile.endpoint == "http://flaresolverr:8191":
            raise DirectProviderClientError(
                "resolver_timed_out",
                "Resolver timed out.",
                retryable=True,
            )
        return self.response.model_copy(update={"request_id": request.request_id})


class _FailingResolveClient(_ResolveClient):
    def __init__(self, error: DirectProviderClientError) -> None:
        super().__init__(_response())
        self.error = error

    async def resolve(self, request: Any) -> DirectResolveResponse:
        self.requests.append(request)
        raise self.error


@pytest.mark.asyncio
async def test_planning_selects_best_eligible_route_and_persists_no_urls(
    session: AsyncSession,
) -> None:
    client = _ResolveClient(_response())

    result = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: client,
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert result.attempt.state is DirectAcquisitionState.PLANNED
    assert result.selected_artifact.host_kind is DirectArtifactHostKind.PIXELDRAIN
    assert result.selected_artifact.state is DirectArtifactState.PLANNED
    assert result.selected_artifact.is_selected is True
    assert result.plan.complete is True
    assert result.plan.pinned_route_applied is False
    rendered = repr(result.attempt.plan_snapshot)
    assert "https://" not in rendered
    assert "signed.cbz" not in rendered
    assert "token" not in rendered.casefold()
    assert "pixel-mirror" in rendered
    assert client.closed is True


@pytest.mark.asyncio
async def test_planning_uses_estimated_size_for_ranking_not_transfer_identity(
    session: AsyncSession,
) -> None:
    response = _generic_response()
    response.artifacts[0].size_is_estimate = True
    response.artifacts[0].mirrors[0].size_bytes = None

    result = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: _ResolveClient(response),
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert result.selected_artifact.expected_size == 100
    assert result.initial_source.expected_size is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested_coverage",
    [
        {"issue_numbers": ["1"], "issue_type": "deluxe"},
        {"issue_numbers": ["1"], "issue_type": "deluxe", "volume": "1"},
    ],
)
async def test_planning_accepts_matching_volume_only_coverage_for_collection(
    session: AsyncSession,
    requested_coverage: dict[str, object],
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    attempt.requested_coverage = requested_coverage
    response = _response()
    response.artifacts[0].coverage = DirectArtifactCoverage(volume="1")
    await session.flush()

    result = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: _ResolveClient(response),
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert result.plan.complete is True
    assert result.plan.requested == frozenset({"1"})
    assert result.selected_artifact.host_kind is DirectArtifactHostKind.PIXELDRAIN


@pytest.mark.asyncio
async def test_planning_persists_selected_contiguous_pack_coverage(
    session: AsyncSession,
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    attempt.requested_coverage = {"issue_numbers": ["5"], "issue_type": "issue"}
    response = _response()
    response.artifacts[0].coverage = DirectArtifactCoverage(issue_numbers=["5", "6"])
    await session.flush()

    result = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: _ResolveClient(response),
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert result.attempt.plan_snapshot["coverage"]["selected_content_issue_numbers"] == ["5", "6"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_coverage", "artifact_volume"),
    [
        ({"issue_numbers": ["1"], "issue_type": "issue"}, "1"),
        (
            {"issue_numbers": ["1"], "issue_type": "deluxe", "volume": "1"},
            "2",
        ),
    ],
)
async def test_planning_rejects_unsafe_volume_only_coverage(
    session: AsyncSession,
    requested_coverage: dict[str, object],
    artifact_volume: str,
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    attempt.requested_coverage = requested_coverage
    response = _response()
    response.artifacts[0].coverage = DirectArtifactCoverage(volume=artifact_volume)
    await session.flush()

    with pytest.raises(DirectAcquisitionPlanningError) as error:
        await plan_direct_acquisition(
            session,
            acquisition_id=1,
            provider_client_factory=lambda **_kwargs: _ResolveClient(response),
            provider_secret_loader=lambda _config: _provider_material(),
            now=lambda: NOW,
        )

    assert error.value.code == "no_eligible_complete_plan"


@pytest.mark.asyncio
async def test_planning_accepts_one_exact_title_only_nonstandard_artifact(
    session: AsyncSession,
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    attempt.requested_coverage = {
        "issue_numbers": ["1"],
        "issue_type": "deluxe",
        "volume": "1",
    }
    attempt.candidate_snapshot = {
        "display_title": "Planner Series Deluxe Edition (2026)",
        "semantic_decision": {
            "is_match": False,
            "confidence": "low",
            "series_similarity": 1.0,
            "match_type": "title_only",
        },
    }
    response = _response()
    response.artifacts[0].coverage = DirectArtifactCoverage()
    await session.flush()

    result = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: _ResolveClient(response),
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert result.plan.complete is True
    assert result.attempt.plan_snapshot["coverage"]["title_only_override"] is True


@pytest.mark.asyncio
async def test_planning_accepts_exact_title_only_collection_quality_alternatives(
    session: AsyncSession,
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    attempt.requested_coverage = {
        "issue_numbers": ["1"],
        "issue_type": "tpb",
        "volume": "1",
    }
    attempt.candidate_snapshot = {
        "display_title": "Black Science Compendium (TPB) (2023)",
        "parsed": {"series_title": "Black Science Compendium"},
        "semantic_decision": {
            "is_match": True,
            "confidence": "low",
            "series_similarity": 1.0,
            "match_type": "exact",
        },
    }
    response = _response()
    response.artifacts[0].coverage = DirectArtifactCoverage(description="Black Science Compendium")
    response.artifacts.append(
        DirectArtifact(
            artifact_id="provider-artifact-sd",
            coverage=DirectArtifactCoverage(description="Black Science Compendium"),
            route=DirectArtifactRoute.DIRECT_ARTIFACT,
            format="cbz",
            size_bytes=651 * 1024 * 1024,
            mirrors=[
                DirectMirror(
                    mirror_id="provider-artifact-sd-mirror",
                    host_kind="pixeldrain",
                    share_url="https://pixeldrain.com/u/sd",
                )
            ],
        )
    )
    await session.flush()

    result = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: _ResolveClient(response),
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert result.plan.complete is True
    assert len(result.plan.selected) == 1
    assert result.attempt.plan_snapshot["coverage"]["title_only_override"] is True
    routes = result.attempt.plan_snapshot["artifacts"]
    assert len({route["content_identity"] for route in routes}) == 2
    assert len({route["fallback_identity"] for route in routes}) == 1


@pytest.mark.asyncio
async def test_planning_rejects_title_only_alternatives_with_mixed_collection_titles(
    session: AsyncSession,
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    attempt.requested_coverage = {
        "issue_numbers": ["1"],
        "issue_type": "tpb",
        "volume": "1",
    }
    attempt.candidate_snapshot = {
        "parsed": {"series_title": "Black Science Compendium"},
        "semantic_decision": {
            "is_match": True,
            "series_similarity": 1.0,
        },
    }
    response = _response()
    response.artifacts[0].coverage = DirectArtifactCoverage(description="Black Science Compendium")
    response.artifacts.append(
        DirectArtifact(
            artifact_id="provider-artifact-unrelated",
            coverage=DirectArtifactCoverage(description="Another Collection"),
            route=DirectArtifactRoute.DIRECT_ARTIFACT,
            format="cbz",
            mirrors=[
                DirectMirror(
                    mirror_id="provider-artifact-unrelated-mirror",
                    host_kind="pixeldrain",
                    share_url="https://pixeldrain.com/u/unrelated",
                )
            ],
        )
    )
    await session.flush()

    with pytest.raises(DirectAcquisitionPlanningError) as error:
        await plan_direct_acquisition(
            session,
            acquisition_id=1,
            provider_client_factory=lambda **_kwargs: _ResolveClient(response),
            provider_secret_loader=lambda _config: _provider_material(),
            now=lambda: NOW,
        )

    assert error.value.code == "no_eligible_complete_plan"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("issue_type", "series_similarity", "artifact_count"),
    [
        ("issue", 1.0, 1),
        ("deluxe", 0.89, 1),
        ("deluxe", 1.0, 2),
    ],
)
async def test_planning_rejects_unsafe_title_only_coverage(
    session: AsyncSession,
    issue_type: str,
    series_similarity: float,
    artifact_count: int,
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    attempt.requested_coverage = {
        "issue_numbers": ["1"],
        "issue_type": issue_type,
        "volume": "1",
    }
    attempt.candidate_snapshot = {
        "display_title": "Planner Series Deluxe Edition (2026)",
        "semantic_decision": {
            "is_match": False,
            "confidence": "low",
            "series_similarity": series_similarity,
            "match_type": "title_only",
        },
    }
    response = _response()
    response.artifacts[0].coverage = DirectArtifactCoverage()
    if artifact_count == 2:
        response.artifacts.append(
            DirectArtifact(
                artifact_id="provider-artifact-2",
                coverage=DirectArtifactCoverage(),
                route=DirectArtifactRoute.DIRECT_ARTIFACT,
                format="cbz",
                mirrors=[
                    DirectMirror(
                        mirror_id="provider-artifact-2-mirror",
                        host_kind="pixeldrain",
                        share_url="https://pixeldrain.com/u/second",
                    )
                ],
            )
        )
    await session.flush()

    with pytest.raises(DirectAcquisitionPlanningError) as error:
        await plan_direct_acquisition(
            session,
            acquisition_id=1,
            provider_client_factory=lambda **_kwargs: _ResolveClient(response),
            provider_secret_loader=lambda _config: _provider_material(),
            now=lambda: NOW,
        )

    assert error.value.code == "no_eligible_complete_plan"


@pytest.mark.asyncio
async def test_planning_fails_closed_instead_of_truncating_multi_artifact_plan(
    session: AsyncSession,
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    attempt.requested_coverage = {"issue_numbers": ["1", "2"], "issue_type": "issue"}
    response = _response()
    response.artifacts[0].coverage = DirectArtifactCoverage(issue_numbers=["1"])
    response.artifacts.append(
        DirectArtifact(
            artifact_id="provider-artifact-2",
            coverage=DirectArtifactCoverage(issue_numbers=["2"]),
            route=DirectArtifactRoute.DIRECT_ARTIFACT,
            format="cbz",
            mirrors=[
                DirectMirror(
                    mirror_id="provider-artifact-2-mirror",
                    host_kind="pixeldrain",
                    share_url="https://pixeldrain.com/u/second",
                )
            ],
        )
    )
    await session.flush()

    with pytest.raises(DirectAcquisitionPlanningError) as error:
        await plan_direct_acquisition(
            session,
            acquisition_id=1,
            provider_client_factory=lambda **_kwargs: _ResolveClient(response),
            provider_secret_loader=lambda _config: _provider_material(),
            now=lambda: NOW,
        )

    assert error.value.code == "multi_artifact_plan_unsupported"


@pytest.mark.asyncio
async def test_planning_ignores_retired_hosts_when_viable_mirrors_remain(
    session: AsyncSession,
) -> None:
    response = _response()
    response.artifacts[0].mirrors.extend(
        [
            DirectMirror(
                mirror_id="zippyshare-mirror",
                host_kind="generic_https",
                final_url="https://www12.zippyshare.com/v/example/file.html",
            ),
            DirectMirror(
                mirror_id="dropapk-mirror",
                host_kind="generic_https",
                final_url="https://dropapk.to/example",
            ),
        ]
    )

    result = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: _ResolveClient(response),
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert result.selected_artifact.host_kind is DirectArtifactHostKind.PIXELDRAIN
    rendered = repr(result.attempt.plan_snapshot)
    assert "zippyshare-mirror" not in rendered
    assert "dropapk-mirror" not in rendered


@pytest.mark.asyncio
async def test_generic_only_provider_does_not_require_visible_host_setting(
    session: AsyncSession,
) -> None:
    provider = await session.get(DirectProviderConfig, 1)
    assert provider is not None
    provider.provider_id = "pullbox.annas_archive"
    provider.manifest_snapshot = {
        "protocol_version": "direct-download-provider/v1",
        "provider_id": "pullbox.annas_archive",
        "display_name": "Anna's Archive",
        "description": "A direct provider fixture.",
        "provider_version": "1.0.0",
        "supported_protocol_versions": ["direct-download-provider/v1"],
        "publisher": "Pullbox",
        "license": "GPL-3.0-or-later",
        "source_domains": ["annas-archive.gd"],
        "artifact_host_patterns": ["generic_https"],
        "capabilities": {"search": True, "resolve": True},
        "configuration_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
    generic = (
        await session.execute(
            select(DirectHostConfig).where(
                DirectHostConfig.host_kind == DirectArtifactHostKind.GENERIC_HTTPS
            )
        )
    ).scalar_one()
    generic.enabled = False
    await session.flush()

    result = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: _ResolveClient(_generic_response()),
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert result.selected_artifact.host_kind is DirectArtifactHostKind.GENERIC_HTTPS
    assert result.plan.complete is True


@pytest.mark.asyncio
async def test_planning_resolve_tries_ordinary_http_then_ranked_resolvers(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.services import direct_resolver_service

    provider = await session.get(DirectProviderConfig, 1)
    assert provider is not None
    provider.resolver_enabled = True
    options = (
        ProviderResolverOption(
            resolver_id=1,
            resolver_name="FlareSolverr",
            resolver_kind=DirectResolverKind.FLARESOLVERR,
            profile=DirectResolverProfile(
                endpoint="http://flaresolverr:8191",
                timeout_seconds=60,
                max_concurrency=1,
                declared_domains=["getcomics.org"],
            ),
        ),
        ProviderResolverOption(
            resolver_id=2,
            resolver_name="Byparr",
            resolver_kind=DirectResolverKind.BYPARR,
            profile=DirectResolverProfile(
                endpoint="http://byparr:8191",
                timeout_seconds=60,
                max_concurrency=1,
                declared_domains=["getcomics.org"],
            ),
        ),
    )

    async def profiles(*_args: object) -> tuple[ProviderResolverOption, ...]:
        return options

    monkeypatch.setattr(direct_resolver_service, "build_provider_resolver_profiles", profiles)
    client = _FallbackResolveClient(_response())

    result = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: client,
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert result.attempt.state is DirectAcquisitionState.PLANNED
    assert [
        request.resolver_profile.endpoint if request.resolver_profile else None
        for request in client.requests
    ] == [None, "http://flaresolverr:8191", "http://byparr:8191"]


@pytest.mark.asyncio
async def test_planning_skips_only_the_blocklisted_artifact_route(
    session: AsyncSession,
) -> None:
    blocked_route = direct_route_identity(
        "community.getcomics",
        "candidate-1",
        "provider-artifact-1",
        "pixel-mirror",
    )
    await BlocklistService.add_direct_artifact_entry(
        session,
        "Planner Series 001 (2026)",
        route_identity=blocked_route,
        artifact_host="PixelDrain",
        issue_id=1,
        series_id=1,
        error_message="The PixelDrain artifact is unavailable.",
    )

    result = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: _ResolveClient(_response()),
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert result.selected_artifact.host_kind is DirectArtifactHostKind.GENERIC_HTTPS
    blocked_snapshot = next(
        route
        for route in result.attempt.plan_snapshot["artifacts"]
        if route["artifact_identity"] == blocked_route
    )
    assert blocked_snapshot["eligible"] is False
    assert blocked_snapshot["eligibility_code"] == "route_blocklisted"


@pytest.mark.asyncio
async def test_planning_accepts_only_pre_plan_semantic_review_intervention(
    session: AsyncSession,
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    attempt.state = DirectAcquisitionState.INTERVENTION
    attempt.failure_class = DirectArtifactFailureClass.USER_ACTION
    attempt.failure_code = "semantic_review_required"
    attempt.error_message = "Review this direct result before downloading."
    await session.flush()

    result = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: _ResolveClient(_response()),
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert result.attempt.state is DirectAcquisitionState.PLANNED
    assert result.attempt.failure_class is None
    assert result.attempt.failure_code is None
    assert result.attempt.error_message is None


@pytest.mark.asyncio
async def test_planning_retries_pre_plan_intervention_after_configuration_is_fixed(
    session: AsyncSession,
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    attempt.state = DirectAcquisitionState.INTERVENTION
    attempt.failure_code = "artifact_host_auth_required"
    await session.flush()

    result = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: _ResolveClient(_response()),
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert result.attempt.state is DirectAcquisitionState.PLANNED
    assert result.attempt.failure_code is None


@pytest.mark.asyncio
async def test_pre_plan_review_survives_temporary_provider_failure_for_retry(
    session: AsyncSession,
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    attempt.state = DirectAcquisitionState.INTERVENTION
    attempt.failure_code = "semantic_review_required"
    await session.flush()

    with pytest.raises(DirectAcquisitionPlanningError) as error:
        await plan_direct_acquisition(
            session,
            acquisition_id=1,
            provider_client_factory=lambda **_kwargs: _FailingResolveClient(
                DirectProviderClientError(
                    "source_unavailable",
                    "Source is temporarily unavailable.",
                    retryable=True,
                )
            ),
            provider_secret_loader=lambda _config: _provider_material(),
            now=lambda: NOW,
        )

    assert error.value.code == "source_unavailable"
    assert attempt.state is DirectAcquisitionState.INTERVENTION
    assert attempt.failure_code == "source_unavailable"

    result = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: _ResolveClient(_response()),
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert result.attempt.state is DirectAcquisitionState.PLANNED
    assert result.attempt.failure_code is None


@pytest.mark.asyncio
async def test_planning_normalizes_provider_error_and_persists_retry_context(
    session: AsyncSession,
) -> None:
    client = _FailingResolveClient(
        DirectProviderClientError(
            "provider_timed_out",
            "Provider timed out at https://secret.invalid/path?token=hidden",
            retryable=True,
        )
    )

    with pytest.raises(DirectAcquisitionPlanningError) as error:
        await plan_direct_acquisition(
            session,
            acquisition_id=1,
            provider_client_factory=lambda **_kwargs: client,
            provider_secret_loader=lambda _config: _provider_material(),
            now=lambda: NOW,
        )

    assert error.value.code == "provider_timed_out"
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    assert attempt.state is DirectAcquisitionState.FAILED
    assert attempt.failure_class is DirectArtifactFailureClass.PROVIDER_UNAVAILABLE
    assert attempt.failure_code == "provider_timed_out"
    assert "secret.invalid" not in (attempt.error_message or "")


@pytest.mark.asyncio
async def test_planning_persists_provider_quota_from_successful_resolution(
    session: AsyncSession,
) -> None:
    response = _response()
    response.quota = DirectQuotaStatus(remaining=22, limit=25, window_seconds=64_800)

    await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: _ResolveClient(response),
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    provider = await session.get(DirectProviderConfig, 1)
    assert provider is not None
    quota = provider.configuration_metadata["quota_status"]
    assert quota["remaining"] == 22
    assert quota["limit"] == 25


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_state"),
    [
        ("source_quota_limited", DirectProviderState.RATE_LIMITED),
        ("source_authentication_required", DirectProviderState.AUTHENTICATION_REQUIRED),
        ("source_unavailable", DirectProviderState.DEGRADED),
        ("candidate_not_found", DirectProviderState.HEALTHY),
    ],
)
async def test_source_failures_do_not_create_intervention_rows(
    session: AsyncSession,
    code: str,
    expected_state: DirectProviderState,
) -> None:
    client = _FailingResolveClient(
        DirectProviderClientError(
            code,
            "Safe provider failure.",
            retryable=code == "source_unavailable",
        )
    )

    with pytest.raises(DirectAcquisitionPlanningError) as error:
        await plan_direct_acquisition(
            session,
            acquisition_id=1,
            provider_client_factory=lambda **_kwargs: client,
            provider_secret_loader=lambda _config: _provider_material(),
            now=lambda: NOW,
        )

    assert error.value.intervention is False
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    provider = await session.get(DirectProviderConfig, 1)
    assert attempt is not None
    assert provider is not None
    assert attempt.state is DirectAcquisitionState.FAILED
    assert provider.state is expected_state


@pytest.mark.asyncio
async def test_unavailable_candidate_error_tells_user_to_try_another_result(
    session: AsyncSession,
) -> None:
    client = _FailingResolveClient(
        DirectProviderClientError(
            "candidate_not_found",
            "Provider-specific source details must not be exposed.",
            retryable=False,
        )
    )

    with pytest.raises(DirectAcquisitionPlanningError) as error:
        await plan_direct_acquisition(
            session,
            acquisition_id=1,
            provider_client_factory=lambda **_kwargs: client,
            provider_secret_loader=lambda _config: _provider_material(),
            now=lambda: NOW,
        )

    assert str(error.value) == (
        "The selected direct result is no longer downloadable. Try another search result."
    )


@pytest.mark.asyncio
async def test_first_quota_failure_persists_provider_retry_window(
    session: AsyncSession,
) -> None:
    client = _FailingResolveClient(
        DirectProviderClientError(
            "source_quota_limited",
            "Safe provider failure.",
            retry_after_seconds=64_800,
        )
    )

    with pytest.raises(DirectAcquisitionPlanningError):
        await plan_direct_acquisition(
            session,
            acquisition_id=1,
            provider_client_factory=lambda **_kwargs: client,
            provider_secret_loader=lambda _config: _provider_material(),
            now=lambda: NOW,
        )

    provider = await session.get(DirectProviderConfig, 1)
    assert provider is not None
    quota = provider.configuration_metadata["quota_status"]
    assert quota["remaining"] == 0
    assert quota["reset_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("provider_disabled", "provider_disabled"),
        ("candidate_disabled", "candidate_not_resolvable"),
        ("candidate_expired", "candidate_expired"),
    ],
)
async def test_planning_revalidates_provider_and_candidate_discovery_state(
    session: AsyncSession,
    mutation: str,
    expected_code: str,
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    provider = await session.get(DirectProviderConfig, 1)
    assert attempt is not None
    assert provider is not None
    if mutation == "provider_disabled":
        provider.enabled = False
    elif mutation == "candidate_disabled":
        attempt.candidate_snapshot = {"can_resolve": False}
    else:
        attempt.candidate_snapshot = {
            "can_resolve": True,
            "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
        }
    client = _ResolveClient(_response())
    await session.flush()

    with pytest.raises(DirectAcquisitionPlanningError) as error:
        await plan_direct_acquisition(
            session,
            acquisition_id=1,
            provider_client_factory=lambda **_kwargs: client,
            provider_secret_loader=lambda _config: _provider_material(),
            now=lambda: NOW,
        )

    assert error.value.code == expected_code
    assert client.requests == []


@pytest.mark.asyncio
async def test_planning_is_deterministic_and_manual_pin_cannot_select_ineligible_route(
    session: AsyncSession,
) -> None:
    first = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: _ResolveClient(_response(reverse=True)),
        provider_secret_loader=lambda _config: _provider_material(),
        pinned_route_identity=direct_route_identity(
            "community.getcomics",
            "candidate-1",
            "provider-artifact-1",
            "terabox-mirror",
        ),
        now=lambda: NOW,
    )

    assert first.selected_artifact.host_kind is DirectArtifactHostKind.PIXELDRAIN
    assert first.plan.explanation_code == "pinned_route_ineligible"
    assert first.plan.pinned_route_applied is False


@pytest.mark.asyncio
async def test_planning_fails_closed_when_provider_host_claim_disagrees_with_url(
    session: AsyncSession,
) -> None:
    response = _response()
    response.artifacts[0].mirrors[0] = DirectMirror(
        mirror_id="mismatch",
        host_kind="pixeldrain",
        share_url="https://mega.nz/file/example#secret",
    )

    with pytest.raises(DirectAcquisitionPlanningError, match="host identity") as error:
        await plan_direct_acquisition(
            session,
            acquisition_id=1,
            provider_client_factory=lambda **_kwargs: _ResolveClient(response),
            provider_secret_loader=lambda _config: _provider_material(),
            now=lambda: NOW,
        )

    assert error.value.code == "provider_host_kind_mismatch"
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    assert "mega.nz" not in repr(attempt.progress_snapshot)


@pytest.mark.asyncio
async def test_planning_classifies_unsafe_provider_headers_without_a_raw_error(
    session: AsyncSession,
) -> None:
    response = _response()
    response.artifacts[0].mirrors[1] = DirectMirror(
        mirror_id="unsafe-header-mirror",
        host_kind="pixeldrain",
        share_url="https://pixeldrain.com/u/abc123",
        source_headers={"Authorization": "Bearer provider-secret"},
    )

    with pytest.raises(DirectAcquisitionPlanningError) as error:
        await plan_direct_acquisition(
            session,
            acquisition_id=1,
            provider_client_factory=lambda **_kwargs: _ResolveClient(response),
            provider_secret_loader=lambda _config: _provider_material(),
            now=lambda: NOW,
        )

    assert error.value.code == "unsafe_provider_header"
    attempt = await session.get(DirectAcquisitionAttempt, 1)
    assert attempt is not None
    assert attempt.failure_class is DirectArtifactFailureClass.CANDIDATE_INVALID
    assert "provider-secret" not in repr(attempt.progress_snapshot)


@pytest.mark.asyncio
async def test_source_reresolution_uses_only_stable_snapshot_ids(
    session: AsyncSession,
) -> None:
    client = _ResolveClient(_response())
    planned = await plan_direct_acquisition(
        session,
        acquisition_id=1,
        provider_client_factory=lambda **_kwargs: client,
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    refreshed = _ResolveClient(_response())
    request = await resolve_planned_artifact_source(
        session,
        acquisition_id=1,
        artifact_id=planned.selected_artifact.id,
        provider_client_factory=lambda **_kwargs: refreshed,
        provider_secret_loader=lambda _config: _provider_material(),
        now=lambda: NOW,
    )

    assert request.host_kind is DirectArtifactHostKind.PIXELDRAIN
    assert request.share_url == "https://pixeldrain.com/u/abc123"
    assert request.final_url is None
    assert request.checksum == "md5:11111111111111111111111111111111"
    assert "signed.cbz" not in repr(request)
    assert refreshed.closed is True


def _provider_material() -> Any:
    class _Material:
        bearer_token = "x" * 32
        configuration: ClassVar[dict[str, str]] = {}

        def __repr__(self) -> str:
            return "_Material(<redacted>)"

    return _Material()

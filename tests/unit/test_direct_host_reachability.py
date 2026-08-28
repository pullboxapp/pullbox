"""Artifact-host reachability and real-operation status contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.direct_acquisition import (
    DirectArtifactFailureClass,
    DirectArtifactHostKind,
    DirectHostAccountState,
    DirectHostConfig,
    DirectHostOperationalResult,
    DirectHostReachabilityState,
)
from pullbox.providers.artifact_hosts.contract import ArtifactHostResolutionError
from pullbox.services.direct_host_reachability import (
    DirectHostProbeObservation,
    check_direct_host_reachability,
    probe_direct_host_endpoint,
    record_direct_host_operational_result,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from pytest_httpx import HTTPXMock


NOW = datetime(2026, 7, 30, 19, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_mega_reachability_uses_streamed_get(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url="https://mega.nz/", status_code=200)

    observation = await probe_direct_host_endpoint(DirectArtifactHostKind.MEGA)

    request = httpx_mock.get_request()
    assert request is not None
    assert request.method == "GET"
    assert observation == DirectHostProbeObservation(contacted=True, status_code=200)


@pytest.mark.asyncio
async def test_non_mega_reachability_keeps_head_probe(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="HEAD", url="https://pixeldrain.com/", status_code=204)

    observation = await probe_direct_host_endpoint(DirectArtifactHostKind.PIXELDRAIN)

    request = httpx_mock.get_request()
    assert request is not None
    assert request.method == "HEAD"
    assert observation == DirectHostProbeObservation(contacted=True, status_code=204)


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_reachability_check_records_reachable_without_downloading(
    session: AsyncSession,
) -> None:
    config = DirectHostConfig(
        host_kind=DirectArtifactHostKind.PIXELDRAIN,
        enabled=True,
    )
    session.add(config)
    await session.commit()
    observed: list[DirectArtifactHostKind] = []

    async def probe(host_kind: DirectArtifactHostKind) -> DirectHostProbeObservation:
        observed.append(host_kind)
        return DirectHostProbeObservation(contacted=True, status_code=204)

    result = await check_direct_host_reachability(
        session,
        DirectArtifactHostKind.PIXELDRAIN,
        probe=probe,
        now=lambda: NOW,
    )

    await session.refresh(config)
    assert observed == [DirectArtifactHostKind.PIXELDRAIN]
    assert result.reachable is True
    assert result.state is DirectHostReachabilityState.REACHABLE
    assert config.reachability_state is DirectHostReachabilityState.REACHABLE
    assert config.last_tested_at == NOW
    assert config.last_reachable_at == NOW
    assert config.last_operational_result is None
    assert config.last_operational_at is None


@pytest.mark.asyncio
async def test_failed_probe_preserves_last_reachable_time(
    session: AsyncSession,
) -> None:
    previous = NOW - timedelta(days=1)
    config = DirectHostConfig(
        host_kind=DirectArtifactHostKind.MEDIAFIRE,
        enabled=True,
        reachability_state=DirectHostReachabilityState.REACHABLE,
        last_reachable_at=previous,
    )
    session.add(config)
    await session.commit()

    async def probe(_host_kind: DirectArtifactHostKind) -> DirectHostProbeObservation:
        return DirectHostProbeObservation(
            contacted=False,
            status_code=None,
            error_code="artifact_host_probe_timeout",
        )

    result = await check_direct_host_reachability(
        session,
        DirectArtifactHostKind.MEDIAFIRE,
        probe=probe,
        now=lambda: NOW,
    )

    await session.refresh(config)
    assert result.reachable is False
    assert result.state is DirectHostReachabilityState.NOT_REACHABLE
    assert config.last_tested_at == NOW
    assert config.last_reachable_at == previous
    assert config.last_error_code == "artifact_host_probe_timeout"


@pytest.mark.asyncio
async def test_generic_https_explains_that_reachability_is_verified_per_url(
    session: AsyncSession,
) -> None:
    probed = False

    async def probe(_host_kind: DirectArtifactHostKind) -> DirectHostProbeObservation:
        nonlocal probed
        probed = True
        return DirectHostProbeObservation(contacted=True, status_code=204)

    result = await check_direct_host_reachability(
        session,
        DirectArtifactHostKind.GENERIC_HTTPS,
        probe=probe,
        now=lambda: NOW,
    )

    assert probed is False
    assert result.reachable is False
    assert result.state is DirectHostReachabilityState.NOT_CHECKED
    assert "per download" in result.message.lower()


@pytest.mark.asyncio
async def test_generic_https_failure_records_operation_without_global_outage(
    session: AsyncSession,
) -> None:
    config = DirectHostConfig(
        host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
        enabled=True,
        reachability_state=DirectHostReachabilityState.NOT_CHECKED,
    )
    session.add(config)
    await session.commit()
    error = ArtifactHostResolutionError(
        code="artifact_host_unavailable",
        message="This final-file URL is unavailable.",
        failure_class=DirectArtifactFailureClass.TRANSIENT_HOST,
        retryable=True,
        intervention=False,
    )

    await record_direct_host_operational_result(
        session,
        host_config_id=config.id,
        occurred_at=NOW,
        succeeded=False,
        error=error,
    )
    await session.flush()

    assert config.last_operational_result is DirectHostOperationalResult.FAILED
    assert config.last_operational_at == NOW
    assert config.last_error_code == "artifact_host_unavailable"
    assert config.reachability_state is DirectHostReachabilityState.NOT_CHECKED
    assert config.last_reachable_at is None


@pytest.mark.asyncio
async def test_real_operations_are_recorded_for_anonymous_hosts(
    session: AsyncSession,
) -> None:
    config = DirectHostConfig(
        host_kind=DirectArtifactHostKind.MEDIAFIRE,
        enabled=True,
    )
    session.add(config)
    await session.commit()

    await record_direct_host_operational_result(
        session,
        host_config_id=config.id,
        occurred_at=NOW,
        succeeded=True,
        error=None,
    )
    await session.flush()

    assert config.last_operational_result is DirectHostOperationalResult.SUCCESSFUL
    assert config.last_operational_at == NOW
    assert config.reachability_state is DirectHostReachabilityState.REACHABLE
    assert config.last_reachable_at == NOW


@pytest.mark.asyncio
async def test_auth_failure_records_actionable_state_and_failed_operation(
    session: AsyncSession,
) -> None:
    config = DirectHostConfig(
        host_kind=DirectArtifactHostKind.DATANODES,
        enabled=True,
    )
    session.add(config)
    await session.commit()
    error = ArtifactHostResolutionError(
        code="artifact_host_auth_required",
        message="The account session must be refreshed.",
        failure_class=DirectArtifactFailureClass.ARTIFACT_HOST_AUTH_REQUIRED,
        retryable=False,
        intervention=True,
    )

    await record_direct_host_operational_result(
        session,
        host_config_id=config.id,
        occurred_at=NOW,
        succeeded=False,
        error=error,
    )
    await session.flush()

    assert config.last_operational_result is DirectHostOperationalResult.FAILED
    assert config.last_operational_at == NOW
    assert config.reachability_state is DirectHostReachabilityState.AUTHENTICATION_REQUIRED
    assert config.last_reachable_at == NOW
    assert config.last_error_code == "artifact_host_auth_required"


@pytest.mark.asyncio
async def test_resolver_failure_preserves_datanodes_account_health(
    session: AsyncSession,
) -> None:
    previous = NOW - timedelta(hours=1)
    config = DirectHostConfig(
        host_kind=DirectArtifactHostKind.DATANODES,
        enabled=True,
        reachability_state=DirectHostReachabilityState.REACHABLE,
        account_state=DirectHostAccountState.HEALTHY,
        last_reachable_at=previous,
    )
    session.add(config)
    await session.commit()
    error = ArtifactHostResolutionError(
        code="artifact_host_resolver_unavailable",
        message="The TRAWL browser pool is temporarily busy or unavailable.",
        failure_class=DirectArtifactFailureClass.RESOLVER,
        retryable=True,
        intervention=False,
    )

    await record_direct_host_operational_result(
        session,
        host_config_id=config.id,
        occurred_at=NOW,
        succeeded=False,
        error=error,
    )
    await session.flush()

    assert config.last_operational_result is DirectHostOperationalResult.FAILED
    assert config.reachability_state is DirectHostReachabilityState.REACHABLE
    assert config.account_state is DirectHostAccountState.HEALTHY
    assert config.last_reachable_at == NOW
    assert config.last_error_code == "artifact_host_resolver_unavailable"

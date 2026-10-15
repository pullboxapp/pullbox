"""User-directed source switching for durable direct acquisitions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models.blocklist import BlocklistEntry
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
    DirectArtifactFailureClass,
    DirectArtifactHostKind,
    DirectArtifactRouteKind,
    DirectArtifactState,
)
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.services.blocklist_service import BlocklistService
from pullbox.services.direct_acquisition_switch import (
    DirectSourceSwitchError,
    list_source_switch_options,
    queue_source_switch,
)
from tests.sqlite_schema import create_test_schema

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
async def session_factory(
    tmp_path: Path,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'source-switch.db'}")
    async with engine.begin() as connection:
        await create_test_schema(connection)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            Series(
                id=1,
                comicvine_id=910_001,
                title="Switch Series",
                sort_title="Switch Series",
                year_start=2026,
                status=SeriesStatus.CONTINUING,
                series_type=SeriesType.STANDARD,
                monitored=True,
                issue_count=1,
            )
        )
        session.add(
            Issue(
                id=1,
                series_id=1,
                comicvine_id=910_002,
                issue_number=1,
                issue_type=IssueType.ISSUE,
                status=IssueStatus.DOWNLOADING,
            )
        )
        attempt = DirectAcquisitionAttempt(
            id=1,
            request_key="source-switch:1",
            issue_id=1,
            provider_identity="pullbox.getcomics",
            provider_candidate_id="candidate-1",
            state=DirectAcquisitionState.CANCELLED,
            plan_revision=1,
            plan_snapshot={
                "schema_version": 1,
                "selected_artifact_identity": "route:current",
                "artifacts": [
                    {
                        "artifact_identity": "route:current",
                        "content_identity": "artifact:primary",
                        "route_kind": "direct",
                        "host_kind": "generic_https",
                        "eligible": True,
                        "eligibility_code": "eligible",
                        "expected_size": 1_000,
                    },
                    {
                        "artifact_identity": "route:pixeldrain",
                        "content_identity": "artifact:primary",
                        "route_kind": "direct",
                        "host_kind": "pixeldrain",
                        "eligible": True,
                        "eligibility_code": "eligible",
                        "expected_size": 1_000,
                    },
                    {
                        "artifact_identity": "route:mediafire",
                        "content_identity": "artifact:primary",
                        "route_kind": "direct",
                        "host_kind": "mediafire",
                        "eligible": True,
                        "eligibility_code": "eligible",
                        "expected_size": 1_100,
                    },
                    {
                        "artifact_identity": "route:attempted",
                        "content_identity": "artifact:primary",
                        "route_kind": "direct",
                        "host_kind": "rootz",
                        "eligible": True,
                        "eligibility_code": "eligible",
                    },
                    {
                        "artifact_identity": "route:differentcontent",
                        "content_identity": "artifact:other",
                        "route_kind": "direct",
                        "host_kind": "terabox",
                        "eligible": True,
                        "eligibility_code": "eligible",
                    },
                    {
                        "artifact_identity": "route:ineligible",
                        "content_identity": "artifact:primary",
                        "route_kind": "direct",
                        "host_kind": "mega",
                        "eligible": False,
                        "eligibility_code": "host_disabled",
                    },
                ],
            },
            progress_revision=2,
            progress_snapshot={"schema_version": 1, "stage": "cancelled"},
            candidate_snapshot={"display_title": "Switch Series 001 (2026)"},
            completed_at=NOW,
            cancelled_at=NOW,
        )
        attempt.artifact_attempts = [
            DirectArtifactAttempt(
                id=1,
                sequence_no=0,
                artifact_identity="route:current",
                route_kind=DirectArtifactRouteKind.DIRECT,
                host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
                state=DirectArtifactState.CANCELLED,
                is_selected=True,
                expected_size=1_000,
                bytes_transferred=0,
                completed_at=NOW,
            ),
            DirectArtifactAttempt(
                id=2,
                sequence_no=1,
                artifact_identity="route:attempted",
                route_kind=DirectArtifactRouteKind.DIRECT,
                host_kind=DirectArtifactHostKind.ROOTZ,
                state=DirectArtifactState.FAILED,
                is_selected=False,
                completed_at=NOW,
            ),
        ]
        session.add(attempt)
        session.add(
            DownloadHistory(
                id=1,
                issue_id=1,
                title="Switch Series 001 (2026)",
                download_url="pullbox-direct://attempt/1",
                download_client=DownloadClientType.DIRECT,
                external_id="direct:1",
                state=DownloadState.FAILED,
                error_message="Cancelled by user",
            )
        )
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_switch_options_include_only_untried_unblocked_equivalent_routes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None
        await BlocklistService.add_direct_artifact_entry(
            session,
            "Switch Series 001 (2026)",
            route_identity="route:mediafire",
            artifact_host="MediaFire",
            issue_id=1,
            series_id=1,
            error_message="Blocked for test",
        )
        await session.commit()

        options = await list_source_switch_options(session, attempt)

    assert [option.artifact_identity for option in options] == ["route:pixeldrain"]
    assert options[0].host_kind is DirectArtifactHostKind.PIXELDRAIN
    assert options[0].expected_size == 1_000


@pytest.mark.parametrize(
    ("failure_class", "failure_code"),
    [
        (DirectArtifactFailureClass.RESOLVER, "artifact_host_resolver_unavailable"),
        (DirectArtifactFailureClass.TRANSIENT_HOST, "artifact_host_unavailable"),
        (DirectArtifactFailureClass.ARTIFACT_HOST_AUTH_REQUIRED, "artifact_host_auth_required"),
        (DirectArtifactFailureClass.ARTIFACT_HOST_CHALLENGE, "artifact_host_challenge"),
        (DirectArtifactFailureClass.HOST_QUOTA, "artifact_host_quota_exhausted"),
        (DirectArtifactFailureClass.USER_ACTION, "source_switched_by_user"),
    ],
)
@pytest.mark.asyncio
async def test_switch_options_include_recoverable_attempted_routes(
    session_factory: async_sessionmaker[AsyncSession],
    failure_class: DirectArtifactFailureClass,
    failure_code: str,
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None
        _add_attempted_datanodes_route(
            attempt,
            failure_class=failure_class,
            failure_code=failure_code,
        )

        options = await list_source_switch_options(session, attempt)

    assert [option.artifact_identity for option in options] == [
        "route:pixeldrain",
        "route:mediafire",
        "route:datanodes",
    ]


@pytest.mark.asyncio
async def test_switch_options_exclude_permanently_failed_attempted_routes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None
        _add_attempted_datanodes_route(
            attempt,
            failure_class=DirectArtifactFailureClass.PERMANENT_MIRROR,
            failure_code="artifact_host_contract_changed",
        )

        options = await list_source_switch_options(session, attempt)

    assert [option.artifact_identity for option in options] == [
        "route:pixeldrain",
        "route:mediafire",
    ]


@pytest.mark.asyncio
async def test_queue_source_switch_reuses_recoverable_attempt_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None
        recoverable = _add_attempted_datanodes_route(
            attempt,
            failure_class=DirectArtifactFailureClass.RESOLVER,
            failure_code="artifact_host_resolver_unavailable",
        )
        original_count = len(attempt.artifact_attempts)

        outcome = await queue_source_switch(
            session,
            attempt,
            attempt.artifact_attempts[0],
            target_artifact_identity="route:datanodes",
            block_current=False,
            at=NOW,
        )

    assert outcome.selected is recoverable
    assert len(attempt.artifact_attempts) == original_count
    assert recoverable.state is DirectArtifactState.PLANNED
    assert recoverable.is_selected is True
    assert recoverable.failure_class is None
    assert recoverable.failure_code is None
    assert recoverable.error_message is None
    assert recoverable.retry_count == 0
    assert recoverable.completed_at is None
    route = next(
        item
        for item in attempt.plan_snapshot["artifacts"]
        if item["artifact_identity"] == "route:datanodes"
    )
    assert route["eligible"] is True
    assert route["eligibility_code"] == "eligible"


@pytest.mark.asyncio
async def test_queue_source_switch_restarts_from_zero_without_blocklisting_current_route(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None
        current = attempt.artifact_attempts[0]

        outcome = await queue_source_switch(
            session,
            attempt,
            current,
            target_artifact_identity="route:pixeldrain",
            block_current=False,
            at=NOW,
        )

    assert outcome.previous_host is DirectArtifactHostKind.GENERIC_HTTPS
    assert outcome.selected.host_kind is DirectArtifactHostKind.PIXELDRAIN
    assert outcome.selected.bytes_transferred == 0
    assert outcome.selected.is_selected is True
    assert current.is_selected is False
    assert current.state is DirectArtifactState.CANCELLED
    assert current.failure_code == "source_switched_by_user"
    assert attempt.state is DirectAcquisitionState.QUEUED
    assert attempt.cancelled_at is None
    assert attempt.completed_at is None
    assert attempt.plan_snapshot["selected_artifact_identity"] == "route:pixeldrain"
    assert attempt.progress_snapshot["stage"] == "source_switch_queued"

    async with session_factory() as session:
        assert list((await session.execute(select(BlocklistEntry))).scalars()) == []
        history = (await session.execute(select(DownloadHistory))).scalar_one()
        assert history.state is DownloadState.QUEUED
        assert history.error_message is None


@pytest.mark.asyncio
async def test_queue_source_switch_can_block_only_the_abandoned_route(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None
        current = attempt.artifact_attempts[0]

        await queue_source_switch(
            session,
            attempt,
            current,
            target_artifact_identity="route:pixeldrain",
            block_current=True,
            at=NOW,
        )

    async with session_factory() as session:
        entries = list((await session.execute(select(BlocklistEntry))).scalars())
        assert len(entries) == 1
        assert entries[0].download_url == "pullbox-direct://artifact/route:current"
        assert entries[0].release_group == "HTTPS"


@pytest.mark.asyncio
async def test_queue_source_switch_rejects_a_non_equivalent_requested_route(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None

        with pytest.raises(DirectSourceSwitchError, match="not available"):
            await queue_source_switch(
                session,
                attempt,
                attempt.artifact_attempts[0],
                target_artifact_identity="route:differentcontent",
                block_current=False,
                at=NOW,
            )


def _add_attempted_datanodes_route(
    attempt: DirectAcquisitionAttempt,
    *,
    failure_class: DirectArtifactFailureClass,
    failure_code: str,
) -> DirectArtifactAttempt:
    routes = list(attempt.plan_snapshot["artifacts"])
    routes.append(
        {
            "artifact_identity": "route:datanodes",
            "content_identity": "artifact:primary",
            "route_kind": "direct",
            "host_kind": "datanodes",
            "eligible": False,
            "eligibility_code": "route_failed",
            "expected_size": 1_000,
        }
    )
    attempt.plan_snapshot = {**attempt.plan_snapshot, "artifacts": routes}
    artifact = DirectArtifactAttempt(
        sequence_no=max(item.sequence_no for item in attempt.artifact_attempts) + 1,
        artifact_identity="route:datanodes",
        route_kind=DirectArtifactRouteKind.DIRECT,
        host_kind=DirectArtifactHostKind.DATANODES,
        state=DirectArtifactState.FAILED,
        is_selected=False,
        expected_size=1_000,
        retry_count=3,
        next_retry_at=NOW,
        failure_class=failure_class,
        failure_code=failure_code,
        error_message="Previous route attempt failed.",
        completed_at=NOW,
    )
    attempt.artifact_attempts.append(artifact)
    return artifact

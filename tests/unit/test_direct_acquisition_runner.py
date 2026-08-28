"""Background dispatch and restart recovery for direct acquisition."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.blocklist import BlocklistEntry, BlocklistReason
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
from pullbox.models.operation_progress import OperationProgress, OperationProgressType
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.artifact_hosts.contract import HostResolutionRequest
from pullbox.services.direct_acquisition_switch import DirectSourceSwitchError
from pullbox.services.direct_download_history_adapter import (
    ensure_direct_download_history,
    sync_direct_download_history,
)
from pullbox.tasks.direct_acquisition_task import DirectAcquisitionRunner

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


@pytest.fixture
async def session_factory(
    tmp_path: Path,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'direct-runner.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            Series(
                id=1,
                comicvine_id=900_001,
                title="Runner Series",
                sort_title="Runner Series",
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
                comicvine_id=900_002,
                issue_number=1,
                issue_type=IssueType.ISSUE,
                status=IssueStatus.WANTED,
            )
        )
        attempt = DirectAcquisitionAttempt(
            id=1,
            request_key="direct-runner:1",
            issue_id=1,
            provider_identity="community.test",
            provider_candidate_id="candidate-1",
            state=DirectAcquisitionState.PLANNED,
            plan_revision=1,
            plan_snapshot={"schema_version": 1},
            progress_snapshot={"stage": "planned"},
            candidate_snapshot={"display_title": "Runner Series 001 (2026)"},
        )
        attempt.artifact_attempts = [
            DirectArtifactAttempt(
                id=1,
                sequence_no=0,
                artifact_identity="route:one",
                route_kind=DirectArtifactRouteKind.DIRECT,
                host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
                state=DirectArtifactState.PLANNED,
                is_selected=True,
            )
        ]
        session.add(attempt)
        await session.commit()
    yield factory
    await engine.dispose()


def _source(name: str) -> HostResolutionRequest:
    return HostResolutionRequest(
        artifact_identity="route:one",
        host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
        share_url=None,
        final_url=f"https://files.example/{name}.cbz?secret=hidden",
    )


@pytest.mark.asyncio
async def test_history_adapter_collapses_legacy_duplicate_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        artifact = await session.get(DirectArtifactAttempt, 1)
        assert attempt is not None and artifact is not None
        legacy = DownloadHistory(
            issue_id=1,
            title="Legacy direct row",
            download_url="pullbox-direct://attempt/1",
            download_client=DownloadClientType.DIRECT,
            external_id=None,
            state=DownloadState.FAILED,
            error_message="Legacy client routing failed.",
        )
        canonical = DownloadHistory(
            issue_id=1,
            title="Canonical direct row",
            download_url="pullbox-direct://attempt/1",
            download_client=DownloadClientType.DIRECT,
            external_id="direct:1",
            state=DownloadState.QUEUED,
        )
        session.add_all([legacy, canonical])
        await session.flush()
        blocklist_entry = BlocklistEntry(
            release_title="Legacy direct row",
            release_title_normalized="legacy direct row",
            download_url="pullbox-direct://artifact/route:one",
            issue_id=1,
            reason=BlocklistReason.FAILED,
            download_history_id=legacy.id,
        )
        session.add(blocklist_entry)
        await session.flush()
        blocklist_entry_id = blocklist_entry.id

        history = await ensure_direct_download_history(
            session,
            attempt,
            artifact,
            at=datetime(2026, 7, 28, tzinfo=UTC),
        )
        await session.commit()

    async with session_factory() as session:
        histories = list((await session.execute(select(DownloadHistory))).scalars())
        blocklist_entry = await session.get(BlocklistEntry, blocklist_entry_id)

    assert len(histories) == 1
    assert histories[0].id == history.id
    assert histories[0].external_id == "direct:1"
    assert histories[0].download_url == "pullbox-direct://attempt/1"
    assert blocklist_entry is not None
    assert blocklist_entry.download_history_id == history.id


@pytest.mark.asyncio
async def test_history_adapter_projects_direct_progress_to_shared_activity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        artifact = await session.get(DirectArtifactAttempt, 1)
        assert attempt is not None and artifact is not None
        attempt.provider_identity = "pullbox.getcomics"
        attempt.state = DirectAcquisitionState.DOWNLOADING
        attempt.progress_snapshot = {
            "stage": "downloading",
            "host_kind": "pixeldrain",
            "percent": 40,
            "bytes_transferred": 40_000_000,
            "total_bytes": 100_000_000,
            "bytes_per_second": 2_000_000,
            "eta_seconds": 30,
        }
        history = await sync_direct_download_history(
            session,
            attempt,
            artifact,
            at=datetime(2026, 8, 27, tzinfo=UTC),
        )
        await session.commit()

    async with session_factory() as session:
        operation = (
            await session.execute(
                select(OperationProgress).where(
                    OperationProgress.operation_type == OperationProgressType.DOWNLOAD,
                    OperationProgress.operation_key == str(history.id),
                )
            )
        ).scalar_one()

    assert operation.overall_percent == pytest.approx(40.0)
    assert operation.overall_current == 40_000_000
    assert operation.overall_total == 100_000_000
    assert operation.rate == 2_000_000
    assert operation.eta_seconds == 30
    assert operation.source_label == "GetComics via PixelDrain"
    assert operation.message == "Downloading from PixelDrain"


@pytest.mark.asyncio
async def test_history_adapter_projects_provider_queue_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        artifact = await session.get(DirectArtifactAttempt, 1)
        assert attempt is not None and artifact is not None
        attempt.provider_identity = "pullbox.libgen"
        attempt.state = DirectAcquisitionState.QUEUED
        attempt.progress_snapshot = {
            "stage": "provider_queue",
            "provider_name": "Library Genesis",
            "host_kind": "generic_https",
        }
        history = await sync_direct_download_history(
            session,
            attempt,
            artifact,
            at=datetime(2026, 8, 28, tzinfo=UTC),
        )
        await session.commit()

    async with session_factory() as session:
        operation = (
            await session.execute(
                select(OperationProgress).where(
                    OperationProgress.operation_type == OperationProgressType.DOWNLOAD,
                    OperationProgress.operation_key == str(history.id),
                )
            )
        ).scalar_one()

    assert operation.message == "Queued for Library Genesis"
    assert operation.source_label == "Library Genesis via HTTPS"


@dataclass
class _Executor:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    sources: list[HostResolutionRequest] = field(default_factory=list)

    async def execute(self, session: AsyncSession, **kwargs: Any) -> None:
        self.sources.append(await kwargs["source_factory"]())
        self.sources.append(await kwargs["source_factory"]())
        self.started.set()
        await self.release.wait()
        attempt = await session.get(DirectAcquisitionAttempt, kwargs["acquisition_id"])
        assert attempt is not None
        attempt.state = DirectAcquisitionState.COMPLETED
        await session.commit()


@dataclass
class _CancellableExecutor:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    async def execute(self, _session: AsyncSession, **kwargs: Any) -> None:
        cancel_event = kwargs["cancel_event"]
        assert isinstance(cancel_event, asyncio.Event)
        self.started.set()
        await cancel_event.wait()
        self.cancelled.set()


@dataclass
class _InactiveCancelExecutor:
    calls: list[tuple[int, int]] = field(default_factory=list)

    async def execute(self, _session: AsyncSession, **_kwargs: Any) -> None:
        raise AssertionError("Inactive cancellation must not dispatch execution")

    async def cancel(self, _session: AsyncSession, **kwargs: Any) -> bool:
        self.calls.append((kwargs["acquisition_id"], kwargs["artifact_id"]))
        return True


@dataclass
class _CompletingExecutor:
    artifact_ids: list[int] = field(default_factory=list)

    async def execute(self, session: AsyncSession, **kwargs: Any) -> None:
        self.artifact_ids.append(kwargs["artifact_id"])
        attempt = await session.get(DirectAcquisitionAttempt, kwargs["acquisition_id"])
        assert attempt is not None
        attempt.state = DirectAcquisitionState.COMPLETED
        await session.commit()


@dataclass
class _FallbackExecutor:
    artifact_ids: list[int] = field(default_factory=list)

    async def execute(self, session: AsyncSession, **kwargs: Any) -> None:
        artifact_id = kwargs["artifact_id"]
        self.artifact_ids.append(artifact_id)
        attempt = await session.get(DirectAcquisitionAttempt, kwargs["acquisition_id"])
        artifact = await session.get(DirectArtifactAttempt, artifact_id)
        assert attempt is not None and artifact is not None
        if len(self.artifact_ids) == 1:
            artifact.state = DirectArtifactState.FAILED
            artifact.is_selected = False
            attempt.state = DirectAcquisitionState.QUEUED
            session.add(
                DirectArtifactAttempt(
                    id=2,
                    acquisition_attempt_id=attempt.id,
                    sequence_no=1,
                    artifact_identity="route:two",
                    route_kind=DirectArtifactRouteKind.DIRECT,
                    host_kind=DirectArtifactHostKind.PIXELDRAIN,
                    state=DirectArtifactState.PLANNED,
                    is_selected=True,
                )
            )
        else:
            attempt.state = DirectAcquisitionState.COMPLETED
            artifact.state = DirectArtifactState.COMPLETED
        await session.commit()


@dataclass
class _ProviderFallbackExecutor:
    acquisition_ids: list[int] = field(default_factory=list)

    async def execute(self, session: AsyncSession, **kwargs: Any) -> None:
        acquisition_id = kwargs["acquisition_id"]
        self.acquisition_ids.append(acquisition_id)
        attempt = await session.get(DirectAcquisitionAttempt, acquisition_id)
        artifact = await session.get(DirectArtifactAttempt, kwargs["artifact_id"])
        assert attempt is not None and artifact is not None
        if acquisition_id == 1:
            attempt.state = DirectAcquisitionState.FAILED
            attempt.failure_class = DirectArtifactFailureClass.PERMANENT_MIRROR
            attempt.failure_code = "artifact_not_found"
            artifact.state = DirectArtifactState.FAILED
            artifact.failure_class = DirectArtifactFailureClass.PERMANENT_MIRROR
            artifact.failure_code = "artifact_not_found"
        else:
            attempt.state = DirectAcquisitionState.COMPLETED
            artifact.state = DirectArtifactState.COMPLETED
        await session.commit()


@dataclass
class _SourceSwitchExecutor:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    artifact_ids: list[int] = field(default_factory=list)

    async def execute(self, session: AsyncSession, **kwargs: Any) -> None:
        artifact_id = kwargs["artifact_id"]
        self.artifact_ids.append(artifact_id)
        attempt = await session.get(DirectAcquisitionAttempt, kwargs["acquisition_id"])
        artifact = await session.get(DirectArtifactAttempt, artifact_id)
        assert attempt is not None and artifact is not None
        if len(self.artifact_ids) == 1:
            cancel_event = kwargs["cancel_event"]
            assert isinstance(cancel_event, asyncio.Event)
            self.started.set()
            await cancel_event.wait()
            attempt.state = DirectAcquisitionState.CANCELLED
            attempt.cancelled_at = datetime.now(UTC)
            attempt.completed_at = attempt.cancelled_at
            artifact.state = DirectArtifactState.CANCELLED
            artifact.completed_at = attempt.cancelled_at
            artifact.bytes_transferred = 0
            artifact.quarantine_path = None
        else:
            attempt.state = DirectAcquisitionState.COMPLETED
            artifact.state = DirectArtifactState.COMPLETED
        await session.commit()


@dataclass
class _UnexpectedFailureExecutor:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def execute(self, _session: AsyncSession, **_kwargs: Any) -> None:
        self.started.set()
        await self.release.wait()
        raise RuntimeError("sensitive internal failure")


@pytest.mark.asyncio
async def test_runner_queues_once_uses_ephemeral_source_then_reresolves(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(
            DownloadHistory(
                issue_id=1,
                title="Runner Series 001 (2026)",
                download_url="pullbox-direct://attempt/1",
                download_client=DownloadClientType.DIRECT,
                external_id=None,
                state=DownloadState.RETRY_PENDING,
            )
        )
        await session.commit()

    executor = _Executor()
    resolver_calls = 0

    async def resolver(_session: AsyncSession, **_kwargs: Any) -> HostResolutionRequest:
        nonlocal resolver_calls
        resolver_calls += 1
        return _source("refreshed")

    runner = DirectAcquisitionRunner(
        session_factory,
        executor=executor,
        source_resolver=resolver,
    )

    assert await runner.dispatch(1, 1, initial_source=_source("initial")) is True
    assert await runner.dispatch(1, 1, initial_source=_source("duplicate")) is False
    await asyncio.wait_for(executor.started.wait(), timeout=1)
    assert executor.sources[0].final_url and "initial.cbz" in executor.sources[0].final_url
    assert executor.sources[1].final_url and "refreshed.cbz" in executor.sources[1].final_url
    assert resolver_calls == 1

    async with session_factory() as session:
        history = (await session.execute(select(DownloadHistory))).scalar_one()
        issue = await session.get(Issue, 1)
        assert issue is not None
        assert history.download_client is DownloadClientType.DIRECT
        assert history.external_id == "direct:1"
        assert history.download_url == "pullbox-direct://attempt/1"
        assert history.title == "Runner Series 001 (2026)"
        assert history.state is DownloadState.QUEUED
        assert issue.status is IssueStatus.DOWNLOADING
        assert "files.example" not in f"{history.download_url} {history.title}"

    executor.release.set()
    await runner.wait_idle()
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None
        assert attempt.state is DirectAcquisitionState.COMPLETED
    await runner.aclose()


@pytest.mark.asyncio
async def test_runner_recovers_queued_attempt_without_ephemeral_urls(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None
        attempt.state = DirectAcquisitionState.QUEUED
        await session.commit()

    executor = _Executor()

    async def resolver(_session: AsyncSession, **_kwargs: Any) -> HostResolutionRequest:
        return _source("recovered")

    runner = DirectAcquisitionRunner(
        session_factory,
        executor=executor,
        source_resolver=resolver,
    )

    assert await runner.recover_and_dispatch(now=datetime(2026, 7, 28, tzinfo=UTC)) == 1
    await asyncio.wait_for(executor.started.wait(), timeout=1)
    assert all(
        source.final_url and "recovered.cbz" in source.final_url for source in executor.sources
    )
    executor.release.set()
    await runner.wait_idle()
    await runner.aclose()


@pytest.mark.asyncio
async def test_runner_dispatches_due_retry_without_restarting(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    retry_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        artifact = await session.get(DirectArtifactAttempt, 1)
        assert attempt is not None and artifact is not None
        attempt.state = DirectAcquisitionState.RETRY_PENDING
        attempt.next_retry_at = retry_at
        artifact.state = DirectArtifactState.RETRY_PENDING
        artifact.next_retry_at = retry_at
        await session.commit()

    executor = _Executor()

    async def resolver(_session: AsyncSession, **_kwargs: Any) -> HostResolutionRequest:
        return _source("scheduled-retry")

    runner = DirectAcquisitionRunner(
        session_factory,
        executor=executor,
        source_resolver=resolver,
    )

    assert await runner.dispatch_due_retries(now=retry_at) == 1
    await asyncio.wait_for(executor.started.wait(), timeout=1)
    assert all(
        source.final_url and "scheduled-retry.cbz" in source.final_url
        for source in executor.sources
    )
    executor.release.set()
    await runner.wait_idle()
    await runner.aclose()


@pytest.mark.asyncio
async def test_runner_continues_automatically_with_queued_fallback_route(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    executor = _FallbackExecutor()

    async def resolver(_session: AsyncSession, **kwargs: Any) -> HostResolutionRequest:
        return HostResolutionRequest(
            artifact_identity="route:two",
            host_kind=DirectArtifactHostKind.PIXELDRAIN,
            share_url="https://pixeldrain.com/u/fallback",
            final_url=None,
        )

    runner = DirectAcquisitionRunner(
        session_factory,
        executor=executor,
        source_resolver=resolver,
    )

    assert await runner.dispatch(1, 1, initial_source=_source("initial")) is True
    await runner.wait_idle()

    assert executor.artifact_ids == [1, 2]
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None
        assert attempt.state is DirectAcquisitionState.COMPLETED
    await runner.aclose()


@pytest.mark.asyncio
async def test_runner_continues_with_hidden_provider_after_routes_are_exhausted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        primary = await session.get(DirectAcquisitionAttempt, 1)
        assert primary is not None
        alternate = DirectAcquisitionAttempt(
            id=2,
            request_key="direct-runner:provider-fallback",
            issue_id=1,
            provider_identity="community.libgen",
            provider_candidate_id="candidate-2",
            state=DirectAcquisitionState.DISCOVERED,
            requested_coverage={"issue_numbers": ["1"]},
            candidate_snapshot={
                "display_title": "Runner Series 001 (2026)",
                "visible": False,
                "primary_attempt_id": 1,
            },
            plan_snapshot={},
            progress_snapshot={"stage": "discovered"},
        )
        session.add(alternate)
        primary.candidate_snapshot = {
            **primary.candidate_snapshot,
            "visible": True,
            "alternate_attempt_ids": [2],
        }
        await session.commit()

    executor = _ProviderFallbackExecutor()
    fallback_calls: list[int] = []

    async def fallback_planner(
        session: AsyncSession,
        *,
        acquisition_id: int,
        skip_selected_attempt: bool,
    ) -> object:
        assert skip_selected_attempt is True
        fallback_calls.append(acquisition_id)
        alternate = await session.get(DirectAcquisitionAttempt, 2)
        assert alternate is not None
        alternate.state = DirectAcquisitionState.PLANNED
        artifact = DirectArtifactAttempt(
            acquisition_attempt_id=alternate.id,
            sequence_no=0,
            artifact_identity="route:provider-alternate",
            route_kind=DirectArtifactRouteKind.DIRECT,
            host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
            state=DirectArtifactState.PLANNED,
            is_selected=True,
        )
        session.add(artifact)
        await session.flush()
        return SimpleNamespace(
            attempt=alternate,
            selected_artifact=artifact,
            initial_source=_source("provider-alternate"),
        )

    runner = DirectAcquisitionRunner(
        session_factory,
        executor=executor,
        source_resolver=AsyncMock(),
        provider_fallback_planner=fallback_planner,
    )

    assert await runner.dispatch(1, 1, initial_source=_source("primary")) is True
    await runner.wait_idle()

    assert fallback_calls == [1]
    assert executor.acquisition_ids == [1, 2]
    async with session_factory() as session:
        alternate = await session.get(DirectAcquisitionAttempt, 2)
        assert alternate is not None
        assert alternate.state is DirectAcquisitionState.COMPLETED
    await runner.aclose()


@pytest.mark.asyncio
async def test_runner_marks_unexpected_worker_failure_instead_of_leaving_download_stuck(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    executor = _UnexpectedFailureExecutor()
    runner = DirectAcquisitionRunner(
        session_factory,
        executor=executor,
        source_resolver=lambda *_args, **_kwargs: _source("unused"),  # type: ignore[arg-type]
    )

    assert await runner.dispatch(1, 1, initial_source=_source("initial")) is True
    await asyncio.wait_for(executor.started.wait(), timeout=1)
    async with session_factory() as session:
        issue = await session.get(Issue, 1)
        assert issue is not None
        assert issue.status is IssueStatus.DOWNLOADING

    executor.release.set()
    with pytest.raises(RuntimeError, match="sensitive internal failure"):
        await runner.wait_idle()

    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        artifact = await session.get(DirectArtifactAttempt, 1)
        issue = await session.get(Issue, 1)
        assert attempt is not None and artifact is not None and issue is not None
        assert attempt.state is DirectAcquisitionState.FAILED
        assert artifact.state is DirectArtifactState.FAILED
        assert attempt.failure_class is DirectArtifactFailureClass.TRANSIENT_SOURCE
        assert attempt.failure_code == "direct_acquisition_worker_failed"
        assert attempt.error_message == "Direct acquisition stopped unexpectedly."
        assert issue.status is IssueStatus.WANTED
        assert "sensitive" not in repr(attempt.progress_snapshot)
    await runner.aclose()


@pytest.mark.asyncio
async def test_runner_cancel_signals_only_the_requested_acquisition(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    executor = _CancellableExecutor()

    async def resolver(_session: AsyncSession, **_kwargs: Any) -> HostResolutionRequest:
        return _source("cancelled")

    runner = DirectAcquisitionRunner(
        session_factory,
        executor=executor,
        source_resolver=resolver,
    )

    assert await runner.dispatch(1, 1) is True
    await asyncio.wait_for(executor.started.wait(), timeout=1)

    assert await runner.cancel(1) is True
    await asyncio.wait_for(executor.cancelled.wait(), timeout=1)
    await runner.wait_idle()
    assert await runner.cancel(999) is False
    await runner.aclose()


@pytest.mark.asyncio
async def test_runner_cancel_delegates_recoverable_inactive_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        artifact = await session.get(DirectArtifactAttempt, 1)
        assert attempt is not None and artifact is not None
        attempt.state = DirectAcquisitionState.RETRY_PENDING
        artifact.state = DirectArtifactState.RETRY_PENDING
        await session.commit()

    executor = _InactiveCancelExecutor()
    runner = DirectAcquisitionRunner(
        session_factory,
        executor=executor,
        source_resolver=lambda *_args, **_kwargs: _source("unused"),  # type: ignore[arg-type]
    )

    assert await runner.cancel(1) is True
    assert executor.calls == [(1, 1)]
    assert await runner.cancel(999) is False
    await runner.aclose()


@pytest.mark.asyncio
async def test_runner_switches_active_transfer_after_cooperative_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None
        attempt.plan_snapshot = {
            "schema_version": 1,
            "selected_artifact_identity": "route:one",
            "artifacts": [
                {
                    "artifact_identity": "route:one",
                    "content_identity": "artifact:primary",
                    "route_kind": "direct",
                    "host_kind": "generic_https",
                    "eligible": True,
                    "eligibility_code": "eligible",
                },
                {
                    "artifact_identity": "route:two",
                    "content_identity": "artifact:primary",
                    "route_kind": "direct",
                    "host_kind": "pixeldrain",
                    "eligible": True,
                    "eligibility_code": "eligible",
                },
            ],
        }
        attempt.artifact_attempts[0].bytes_transferred = 512
        await session.commit()

    executor = _SourceSwitchExecutor()

    async def resolver(_session: AsyncSession, **_kwargs: Any) -> HostResolutionRequest:
        return HostResolutionRequest(
            artifact_identity="route:two",
            host_kind=DirectArtifactHostKind.PIXELDRAIN,
            share_url="https://pixeldrain.com/u/replacement",
            final_url=None,
        )

    runner = DirectAcquisitionRunner(
        session_factory,
        executor=executor,
        source_resolver=resolver,
    )

    assert await runner.dispatch(1, 1, initial_source=_source("initial")) is True
    await asyncio.wait_for(executor.started.wait(), timeout=1)

    outcome = await runner.switch_source(
        1,
        target_artifact_identity="route:two",
        block_current=False,
    )
    await runner.wait_idle()

    assert outcome.previous_host is DirectArtifactHostKind.GENERIC_HTTPS
    assert outcome.selected.host_kind is DirectArtifactHostKind.PIXELDRAIN
    assert executor.artifact_ids == [1, outcome.selected.id]
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None
        assert attempt.state is DirectAcquisitionState.COMPLETED
        current, replacement = attempt.artifact_attempts
        assert current.state is DirectArtifactState.CANCELLED
        assert current.failure_code == "source_switched_by_user"
        assert replacement.state is DirectArtifactState.COMPLETED
        assert replacement.is_selected is True
        assert attempt.progress_snapshot["previous_bytes_discarded"] == 512
        assert list((await session.execute(select(BlocklistEntry))).scalars()) == []
    await runner.aclose()


@pytest.mark.asyncio
async def test_runner_rejects_source_switch_before_cancelling_when_no_route_exists(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None
        attempt.plan_snapshot = {
            "schema_version": 1,
            "selected_artifact_identity": "route:one",
            "artifacts": [
                {
                    "artifact_identity": "route:one",
                    "content_identity": "artifact:primary",
                    "route_kind": "direct",
                    "host_kind": "generic_https",
                    "eligible": True,
                    "eligibility_code": "eligible",
                }
            ],
        }
        await session.commit()

    executor = _SourceSwitchExecutor()
    runner = DirectAcquisitionRunner(
        session_factory,
        executor=executor,
        source_resolver=AsyncMock(),
    )

    assert await runner.dispatch(1, 1, initial_source=_source("initial")) is True
    await asyncio.wait_for(executor.started.wait(), timeout=1)

    with pytest.raises(DirectSourceSwitchError, match="No other verified"):
        await runner.switch_source(1)

    assert executor.artifact_ids == [1]
    assert await runner.cancel(1) is True
    await runner.wait_idle()
    await runner.aclose()


@pytest.mark.asyncio
async def test_runner_reopens_terminal_attempt_for_explicit_retry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        artifact = await session.get(DirectArtifactAttempt, 1)
        assert attempt is not None and artifact is not None
        attempt.state = DirectAcquisitionState.FAILED
        attempt.retry_count = attempt.max_retries
        attempt.completed_at = datetime(2026, 7, 28, tzinfo=UTC)
        artifact.state = DirectArtifactState.FAILED
        artifact.retry_count = artifact.max_retries
        artifact.completed_at = datetime(2026, 7, 28, tzinfo=UTC)
        await session.commit()

    executor = _Executor()

    async def resolver(_session: AsyncSession, **_kwargs: Any) -> HostResolutionRequest:
        return _source("manual-retry")

    runner = DirectAcquisitionRunner(
        session_factory,
        executor=executor,
        source_resolver=resolver,
    )

    assert await runner.retry(1) is True
    await asyncio.wait_for(executor.started.wait(), timeout=1)
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        artifact = await session.get(DirectArtifactAttempt, 1)
        assert attempt is not None and artifact is not None
        assert attempt.state is DirectAcquisitionState.RETRY_PENDING
        assert artifact.state is DirectArtifactState.RETRY_PENDING
        assert attempt.retry_count == 0
        assert artifact.retry_count == 0
        assert attempt.completed_at is None
        assert artifact.completed_at is None
        assert attempt.progress_snapshot["stage"] == "retry_requested"

    executor.release.set()
    await runner.wait_idle()
    await runner.aclose()


@pytest.mark.asyncio
async def test_runner_manual_retry_never_requeues_a_blocklisted_route(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        artifact = await session.get(DirectArtifactAttempt, 1)
        assert attempt is not None and artifact is not None
        attempt.state = DirectAcquisitionState.FAILED
        attempt.plan_snapshot = {
            "schema_version": 1,
            "selected_artifact_identity": "route:one",
            "artifacts": [
                {
                    "artifact_identity": "route:one",
                    "content_identity": "artifact:primary",
                    "route_kind": "direct",
                    "host_kind": "generic_https",
                    "eligible": True,
                    "eligibility_code": "eligible",
                },
                {
                    "artifact_identity": "route:two",
                    "content_identity": "artifact:primary",
                    "route_kind": "direct",
                    "host_kind": "pixeldrain",
                    "eligible": True,
                    "eligibility_code": "eligible",
                },
            ],
        }
        artifact.state = DirectArtifactState.FAILED
        artifact.failure_class = DirectArtifactFailureClass.PERMANENT_MIRROR
        artifact.failure_code = "artifact_not_found"
        artifact.error_message = "The selected route no longer exists."
        session.add(
            BlocklistEntry(
                release_title="Runner Series 001 (2026)",
                release_title_normalized="direct-artifact:route:one",
                download_url="pullbox-direct://artifact/route:one",
                issue_id=1,
                reason=BlocklistReason.FAILED,
                release_group="Generic Https",
            )
        )
        await session.commit()

    executor = _CompletingExecutor()

    async def resolver(_session: AsyncSession, **kwargs: Any) -> HostResolutionRequest:
        assert kwargs["artifact_id"] != 1
        return HostResolutionRequest(
            artifact_identity="route:two",
            host_kind=DirectArtifactHostKind.PIXELDRAIN,
            share_url="https://pixeldrain.com/u/fallback",
            final_url=None,
        )

    runner = DirectAcquisitionRunner(
        session_factory,
        executor=executor,
        source_resolver=resolver,
    )

    assert await runner.retry(1) is True
    await runner.wait_idle()

    async with session_factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, 1)
        assert attempt is not None
        await session.refresh(attempt, attribute_names=["artifact_attempts"])
        selected = [artifact for artifact in attempt.artifact_attempts if artifact.is_selected]
        assert len(selected) == 1
        assert selected[0].artifact_identity == "route:two"
        assert executor.artifact_ids == [selected[0].id]

    await runner.aclose()

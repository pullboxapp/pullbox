"""Durable execution tests for direct artifact acquisition."""

from __future__ import annotations

import asyncio
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import pullbox.services.direct_acquisition_executor as direct_executor_module
from pullbox.models import Base
from pullbox.models.blocklist import BlocklistEntry
from pullbox.models.config import SystemConfig
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
    DirectArtifactFailureClass,
    DirectArtifactHostKind,
    DirectArtifactRouteKind,
    DirectArtifactState,
    DirectHostAccountState,
    DirectHostConfig,
    DirectHostOperationalResult,
    DirectHostReachabilityState,
    DirectProviderConfig,
    DirectProviderState,
    DirectProviderTrustLevel,
)
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.operation_progress import (
    OperationProgress,
    OperationProgressState,
    OperationProgressType,
)
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.artifact_hosts.contract import (
    ArtifactHostResolutionError,
    ArtifactResolutionProgress,
    ArtifactTransferProtocol,
    HostResolutionRequest,
    ResolvedTransfer,
)
from pullbox.providers.artifact_hosts.mega import (
    MegaBridgePausedError,
    MegaBridgeTransferError,
    MegaBridgeTransferResult,
)
from pullbox.providers.artifact_hosts.transport_contract import (
    ArtifactTransferCancelledError,
    ArtifactTransferError,
    ArtifactTransferPausedError,
    ArtifactTransferResult,
    HttpTransferCheckpoint,
    TransferProgressSnapshot,
)
from pullbox.services.blocklist_service import BlocklistService
from pullbox.services.direct_acquisition_executor import (
    DirectAcquisitionExecutor,
    _recover_http_checkpoint,
    _SlowSourceTracker,
)
from pullbox.services.direct_acquisition_fallback import (
    queue_next_artifact_route,
    supports_route_fallback,
)
from pullbox.services.direct_acquisition_planner_service import (
    DirectAcquisitionPlanningError,
)
from pullbox.services.direct_artifact_post_processing import DirectPostProcessingResult
from pullbox.services.direct_artifact_quarantine import DirectArtifactQuarantine
from pullbox.services.direct_configuration_service import update_host_credentials
from pullbox.services.operation_progress_dispatch import drain_operation_progress_updates

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def test_unsafe_routes_fall_back_while_content_safety_remains_a_hard_stop() -> None:
    assert supports_route_fallback(DirectArtifactFailureClass.UNSAFE_ROUTE) is True
    assert supports_route_fallback(DirectArtifactFailureClass.SAFETY) is False


def test_slow_source_tracker_requires_sustained_progress_regardless_of_eta() -> None:
    tracker = _SlowSourceTracker()

    assert tracker.observe(at=0.0, bytes_transferred=0) is False
    assert tracker.observe(at=59.0, bytes_transferred=2 * 1024**2) is False
    assert tracker.observe(at=60.0, bytes_transferred=2 * 1024**2) is True

    stalled = _SlowSourceTracker()
    assert stalled.observe(at=0.0, bytes_transferred=0) is False
    assert stalled.observe(at=60.0, bytes_transferred=0) is False


def test_slow_source_tracker_uses_recovery_hysteresis() -> None:
    tracker = _SlowSourceTracker()

    assert tracker.observe(at=0.0, bytes_transferred=0) is False
    assert tracker.observe(at=60.0, bytes_transferred=2 * 1024**2) is True
    assert tracker.observe(at=75.0, bytes_transferred=4 * 1024**2) is True
    assert tracker.observe(at=90.0, bytes_transferred=6 * 1024**2) is False

    boundary = _SlowSourceTracker()
    assert boundary.observe(at=0.0, bytes_transferred=0) is False
    assert boundary.observe(at=60.0, bytes_transferred=2 * 1024**2) is True
    exactly_750_kbps = 2 * 1024**2 + int((750_000 / 8) * 30)
    assert (
        boundary.observe(
            at=90.0,
            bytes_transferred=exactly_750_kbps,
        )
        is True
    )
    assert (
        boundary.observe(
            at=120.0,
            bytes_transferred=exactly_750_kbps + 100_000 * 30,
        )
        is False
    )


@pytest.fixture
async def session(tmp_path: Path) -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        series = Series(
            comicvine_id=995_001,
            title="Direct Executor",
            sort_title="Direct Executor",
            year_start=2026,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        db_session.add(series)
        await db_session.flush()
        db_session.add(
            Issue(
                id=1,
                series_id=series.id,
                comicvine_id=996_001,
                issue_number=1,
                status=IssueStatus.WANTED,
                issue_type=IssueType.ISSUE,
            )
        )
        library_root = LibraryRoot(
            id=1,
            name="Test Library",
            path=str(tmp_path / "library"),
            enabled=True,
        )
        db_session.add(library_root)
        db_session.add(
            LibraryFile(
                id=77,
                file_path="/library/Issue 1.cbz",
                file_name="Issue 1.cbz",
                file_size=100,
                file_format=FileFormat.CBZ,
                file_modified_at=NOW,
                match_confidence=MatchConfidence.HIGH,
                issue_id=1,
                library_root_id=1,
            )
        )
        await db_session.commit()
        yield db_session
    await engine.dispose()


def _attempt() -> DirectAcquisitionAttempt:
    attempt = DirectAcquisitionAttempt(
        request_key="direct-executor:1",
        issue_id=1,
        provider_identity="community.test",
        provider_candidate_id="candidate-1",
        state=DirectAcquisitionState.QUEUED,
        plan_revision=1,
        plan_snapshot={"schema_version": 1},
        candidate_snapshot={
            "display_title": "Direct Executor 001 (2026)",
            "semantic_decision": {"confidence": "high", "series_similarity": 1.0},
        },
        progress_revision=0,
        progress_snapshot={},
    )
    attempt.artifact_attempts = [
        DirectArtifactAttempt(
            sequence_no=0,
            artifact_identity="artifact-1",
            route_kind=DirectArtifactRouteKind.DIRECT,
            host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
            state=DirectArtifactState.PLANNED,
            is_selected=True,
            etag='"stable"',
        )
    ]
    return attempt


def _source_request() -> HostResolutionRequest:
    return HostResolutionRequest(
        artifact_identity="artifact-1",
        host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
        share_url=None,
        final_url="https://files.example/signed-secret.cbz",
        expected_size=None,
        etag='"stable"',
    )


@dataclass
class _FakeResolver:
    resolved: ResolvedTransfer
    calls: int = 0

    async def resolve(
        self,
        request: HostResolutionRequest,
        *,
        credentials: Any,
        progress_callback: Any = None,
    ) -> ResolvedTransfer:
        self.calls += 1
        assert request.artifact_identity == "artifact-1"
        assert credentials == {}
        return self.resolved


@dataclass
class _AccountResolver:
    resolved: ResolvedTransfer | None = None
    error: ArtifactHostResolutionError | None = None

    async def resolve(
        self,
        request: HostResolutionRequest,
        *,
        credentials: Any,
        progress_callback: Any = None,
    ) -> ResolvedTransfer:
        assert request.host_kind is DirectArtifactHostKind.PIXELDRAIN
        assert credentials == {"api_key": "configured-pixeldrain-key"}
        if self.error is not None:
            raise self.error
        assert self.resolved is not None
        return self.resolved


class _SuccessfulTransport:
    def __init__(self) -> None:
        self.checkpoint: HttpTransferCheckpoint | None = None

    async def transfer(self, **kwargs: Any) -> ArtifactTransferResult:
        destination = kwargs["destination"]
        self.checkpoint = kwargs["checkpoint"]
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("001.jpg", b"synthetic fixture")
        size = destination.stat().st_size
        await kwargs["progress_callback"](
            TransferProgressSnapshot(
                bytes_transferred=size,
                total_bytes=size,
                percent=100,
                bytes_per_second=1024.0,
                eta_seconds=0.0,
            )
        )
        return ArtifactTransferResult(
            path=destination,
            bytes_transferred=size,
            expected_size=size,
            etag='"stable"',
            last_modified=None,
            filename_hint="issue.cbz",
            resumed=self.checkpoint is not None,
        )


class _InitialProgressTransport(_SuccessfulTransport):
    def __init__(self, attempt: DirectAcquisitionAttempt) -> None:
        super().__init__()
        self._attempt = attempt
        self.initial_progress: dict[str, object] | None = None
        self.initial_etag: str | None = None

    async def transfer(self, **kwargs: Any) -> ArtifactTransferResult:
        self.initial_progress = dict(self._attempt.progress_snapshot)
        self.initial_etag = self._attempt.artifact_attempts[0].etag
        return await super().transfer(**kwargs)


class _FailingTransport:
    def __init__(self, error: BaseException, *, write_partial: bool = False) -> None:
        self.error = error
        self.write_partial = write_partial

    async def transfer(self, **kwargs: Any) -> ArtifactTransferResult:
        if self.write_partial:
            kwargs["destination"].write_bytes(b"partial")
        raise self.error


class _UnexpectedTransport:
    async def transfer(self, **_kwargs: Any) -> ArtifactTransferResult:
        raise AssertionError("A post-processing retry must not download the artifact again")


class _CancelledTaskTransport:
    async def transfer(self, **kwargs: Any) -> ArtifactTransferResult:
        destination = kwargs["destination"]
        destination.write_bytes(b"restartable-partial")
        await kwargs["progress_callback"](
            TransferProgressSnapshot(
                bytes_transferred=destination.stat().st_size,
                total_bytes=100,
                percent=19,
                bytes_per_second=100.0,
                eta_seconds=1.0,
            )
        )
        raise asyncio.CancelledError


class _PausedMegaRunner:
    async def transfer(self, **kwargs: Any) -> MegaBridgeTransferResult:
        destination = kwargs["destination"]
        destination.write_bytes(b"non-resumable-mega-partial")
        await kwargs["progress_callback"](destination.stat().st_size, 100)
        raise MegaBridgePausedError


class _SuccessfulMegaRunner:
    def __init__(self) -> None:
        self.session: str | None = None
        self.checksum: str | None = None

    async def transfer(self, **kwargs: Any) -> MegaBridgeTransferResult:
        self.session = kwargs["session"]
        self.checksum = kwargs["checksum"]
        assert "app_key" not in kwargs
        destination = kwargs["destination"]
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("001.jpg", b"synthetic fixture")
        size = destination.stat().st_size
        await kwargs["progress_callback"](size, size)
        return MegaBridgeTransferResult(
            bytes_transferred=size,
            filename_hint="issue.cbz",
            command_summary="pullbox-mega-bridge",
            _destination=destination,
        )


class _UnavailableMegaRunner:
    async def transfer(self, **_kwargs: Any) -> MegaBridgeTransferResult:
        raise MegaBridgeTransferError(
            code="mega_link_unavailable",
            message="The MEGA transfer could not be completed.",
            failure_class=DirectArtifactFailureClass.PERMANENT_MIRROR,
            retryable=False,
            intervention=True,
        )


@dataclass
class _StaticResolver:
    resolved: ResolvedTransfer

    async def resolve(
        self,
        _request: HostResolutionRequest,
        *,
        credentials: Any,
        progress_callback: Any = None,
    ) -> ResolvedTransfer:
        assert credentials == {}
        return self.resolved


@dataclass
class _ProgressResolver:
    resolved: ResolvedTransfer
    attempt: DirectAcquisitionAttempt
    observed_snapshot: dict[str, object] | None = None

    async def resolve(
        self,
        _request: HostResolutionRequest,
        *,
        credentials: Any,
        progress_callback: Any = None,
    ) -> ResolvedTransfer:
        assert credentials == {}
        assert progress_callback is not None
        await progress_callback(
            ArtifactResolutionProgress(
                resolver_id=3,
                resolver_name="TRAWL",
                resolver_kind="trawl",
                attempt=1,
                total=1,
                scope="datanodes",
            )
        )
        self.observed_snapshot = dict(self.attempt.progress_snapshot)
        return self.resolved


def _executor(
    tmp_path: Path,
    *,
    transport: Any,
    post_processor: Any,
) -> DirectAcquisitionExecutor:
    resolved = ResolvedTransfer(
        host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
        url="https://files.example/signed-secret.cbz",
        etag='"stable"',
        allowed_domains=("files.example",),
        transport_protocol=ArtifactTransferProtocol.HTTPS,
    )
    return DirectAcquisitionExecutor(
        host_resolver=_FakeResolver(resolved),
        http_transport=transport,
        mega_runner=object(),
        quarantine=DirectArtifactQuarantine(tmp_path / "quarantine"),
        post_processor=post_processor,
        now=lambda: NOW,
    )


async def _successful_post_processor(*_args: Any, **_kwargs: Any) -> DirectPostProcessingResult:
    return DirectPostProcessingResult(
        library_file_id=77,
        final_path=Path("/library/Issue 1.cbz"),
    )


@pytest.mark.asyncio
async def test_executor_completes_with_durable_redacted_progress(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    session.add(attempt)
    await session.flush()
    history = DownloadHistory(
        issue_id=attempt.issue_id,
        title="Direct Executor 001 (2026)",
        download_url=f"pullbox-direct://attempt/{attempt.id}",
        download_client=DownloadClientType.DIRECT,
        external_id=f"direct:{attempt.id}",
        state=DownloadState.QUEUED,
    )
    session.add(history)
    await session.commit()
    artifact = attempt.artifact_attempts[0]
    source_calls = 0
    observed_download_history_id: int | None = None

    async def source_factory() -> HostResolutionRequest:
        nonlocal source_calls
        source_calls += 1
        return _source_request()

    async def post_processor(*_args: Any, **kwargs: Any) -> DirectPostProcessingResult:
        nonlocal observed_download_history_id
        observed_download_history_id = kwargs["download_history_id"]
        return await _successful_post_processor()

    result = await _executor(
        tmp_path,
        transport=_SuccessfulTransport(),
        post_processor=post_processor,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=source_factory,
    )

    assert result.state is DirectAcquisitionState.COMPLETED
    assert attempt.state is DirectAcquisitionState.COMPLETED
    assert artifact.state is DirectArtifactState.COMPLETED
    assert attempt.library_file_id == 77
    assert artifact.quarantine_path is None
    assert attempt.progress_snapshot["stage"] == "completed"
    assert "signed-secret" not in repr(attempt.progress_snapshot)
    assert source_calls == 1
    assert not (tmp_path / "quarantine" / f"attempt-{attempt.id}").exists()
    refreshed_history = (
        await session.execute(
            select(DownloadHistory).where(DownloadHistory.external_id == f"direct:{attempt.id}")
        )
    ).scalar_one()
    assert refreshed_history.state is DownloadState.IMPORTED
    assert refreshed_history.file_size == artifact.expected_size
    assert refreshed_history.final_path == "/library/Issue 1.cbz"
    assert refreshed_history.imported_at == NOW
    assert refreshed_history.error_message is None
    assert observed_download_history_id == refreshed_history.id

    await drain_operation_progress_updates()
    post_processing_operations = list(
        (
            await session.execute(
                select(OperationProgress).where(
                    OperationProgress.operation_type == OperationProgressType.POST_PROCESSING
                )
            )
        ).scalars()
    )
    assert len(post_processing_operations) == 1
    post_processing_operation = post_processing_operations[0]
    assert post_processing_operation.operation_key == str(refreshed_history.id)
    assert post_processing_operation.state is OperationProgressState.COMPLETED
    assert post_processing_operation.overall_percent == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_executor_publishes_known_size_before_first_transfer_byte(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    artifact = attempt.artifact_attempts[0]
    artifact.expected_size = 59_247_008
    artifact.etag = None
    session.add(attempt)
    await session.commit()
    transport = _InitialProgressTransport(attempt)

    await _executor(
        tmp_path,
        transport=transport,
        post_processor=_successful_post_processor,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=_async_source,
    )

    assert transport.initial_progress == {
        "schema_version": 1,
        "stage": "downloading",
        "artifact_attempt_id": artifact.id,
        "host_kind": "generic_https",
        "bytes_transferred": 0,
        "total_bytes": 59_247_008,
        "percent": 0,
        "bytes_per_second": None,
        "eta_seconds": None,
        "source_slow": False,
    }
    assert transport.initial_etag == '"stable"'


@pytest.mark.asyncio
async def test_executor_routes_contiguous_pack_to_pack_post_processor(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = _attempt()
    attempt.plan_snapshot = {
        "coverage": {"selected_content_issue_numbers": ["1", "2"]},
    }
    session.add(attempt)
    await session.commit()
    observed: dict[str, Any] = {}

    async def pack_post_processor(*_args: Any, **kwargs: Any) -> DirectPostProcessingResult:
        observed.update(kwargs)
        return DirectPostProcessingResult(
            library_file_id=77,
            final_path=Path("/library/Issue 1.cbz"),
            imported_issue_ids=(attempt.issue_id, 2),
        )

    async def single_post_processor(*_args: Any, **_kwargs: Any) -> DirectPostProcessingResult:
        raise AssertionError("A contiguous pack must not use the one-file post-processor.")

    monkeypatch.setattr(
        direct_executor_module,
        "run_direct_artifact_pack_post_processing",
        pack_post_processor,
    )
    artifact = attempt.artifact_attempts[0]
    result = await _executor(
        tmp_path,
        transport=_SuccessfulTransport(),
        post_processor=single_post_processor,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=lambda: _async_value(_source_request()),
    )

    assert result.state is DirectAcquisitionState.COMPLETED
    assert observed["expected_issue_numbers"] == frozenset({"1", "2"})
    history = (
        await session.execute(
            select(DownloadHistory).where(DownloadHistory.external_id == f"direct:{attempt.id}")
        )
    ).scalar_one()
    assert observed["download_history_id"] == history.id


@pytest.mark.asyncio
async def test_executor_allows_internal_https_for_generic_only_provider(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    provider = DirectProviderConfig(
        provider_id="pullbox.annas_archive",
        display_name="Anna's Archive",
        endpoint="http://annas-archive:8780",
        enabled=True,
        priority=10,
        state=DirectProviderState.HEALTHY,
        negotiated_protocol="direct-download-provider/v1",
        trust_level=DirectProviderTrustLevel.VERIFIED_PULLBOX,
        encrypted_bearer_token="unused-in-test",
        manifest_snapshot={
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
        },
    )
    session.add(provider)
    await session.flush()
    session.add(
        DirectHostConfig(
            host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
            enabled=False,
            preference=50,
        )
    )
    attempt = _attempt()
    attempt.provider_config_id = provider.id
    session.add(attempt)
    await session.commit()
    artifact = attempt.artifact_attempts[0]

    result = await _executor(
        tmp_path,
        transport=_SuccessfulTransport(),
        post_processor=_successful_post_processor,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=lambda: _async_value(_source_request()),
    )

    assert result.state is DirectAcquisitionState.COMPLETED


@pytest.mark.asyncio
async def test_executor_persists_native_resolver_attempt_before_transfer(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    session.add(attempt)
    await session.commit()
    artifact = attempt.artifact_attempts[0]
    resolved = ResolvedTransfer(
        host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
        url="https://files.example/signed-secret.cbz",
        allowed_domains=("files.example",),
    )
    resolver = _ProgressResolver(resolved=resolved, attempt=attempt)
    executor = DirectAcquisitionExecutor(
        host_resolver=resolver,
        http_transport=_SuccessfulTransport(),
        mega_runner=object(),
        quarantine=DirectArtifactQuarantine(tmp_path / "quarantine"),
        post_processor=_successful_post_processor,
        now=lambda: NOW,
    )

    await executor.execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=lambda: _async_value(_source_request()),
    )

    assert resolver.observed_snapshot == {
        "schema_version": 1,
        "stage": "resolver",
        "artifact_attempt_id": artifact.id,
        "host_kind": "generic_https",
        "bytes_transferred": 0,
        "total_bytes": None,
        "percent": None,
        "bytes_per_second": None,
        "eta_seconds": None,
        "resolver_id": 3,
        "resolver_name": "TRAWL",
        "resolver_kind": "trawl",
        "resolver_attempt": 1,
        "resolver_total": 1,
        "resolver_scope": "datanodes",
    }


@pytest.mark.asyncio
async def test_executor_pauses_http_with_resume_checkpoint(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    session.add(attempt)
    await session.commit()
    artifact = attempt.artifact_attempts[0]
    checkpoint = HttpTransferCheckpoint(
        bytes_transferred=7,
        expected_size=100,
        etag='"stable"',
        last_modified=None,
    )
    transport = _FailingTransport(ArtifactTransferPausedError(checkpoint), write_partial=True)

    result = await _executor(
        tmp_path,
        transport=transport,
        post_processor=_successful_post_processor,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=_async_source,
    )

    assert result.state is DirectAcquisitionState.PAUSED
    assert artifact.state is DirectArtifactState.PAUSED
    assert artifact.bytes_transferred == 7
    assert artifact.quarantine_path is not None
    assert Path(artifact.quarantine_path).exists()


@pytest.mark.asyncio
async def test_executor_cancellation_is_terminal_and_cleans_quarantine(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    session.add(attempt)
    await session.commit()
    artifact = attempt.artifact_attempts[0]

    result = await _executor(
        tmp_path,
        transport=_FailingTransport(ArtifactTransferCancelledError(), write_partial=True),
        post_processor=_successful_post_processor,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=_async_source,
    )

    assert result.state is DirectAcquisitionState.CANCELLED
    assert artifact.state is DirectArtifactState.CANCELLED
    assert artifact.quarantine_path is None
    assert not (tmp_path / "quarantine" / f"attempt-{attempt.id}").exists()


@pytest.mark.asyncio
async def test_executor_honors_cancellation_before_resolving_source(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    session.add(attempt)
    await session.commit()
    artifact = attempt.artifact_attempts[0]
    cancel_event = asyncio.Event()
    cancel_event.set()
    source_called = False

    async def source_factory() -> HostResolutionRequest:
        nonlocal source_called
        source_called = True
        return _source_request()

    result = await _executor(
        tmp_path,
        transport=_SuccessfulTransport(),
        post_processor=_successful_post_processor,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=source_factory,
        cancel_event=cancel_event,
    )

    assert source_called is False
    assert result.state is DirectAcquisitionState.CANCELLED
    assert artifact.state is DirectArtifactState.CANCELLED


@pytest.mark.asyncio
async def test_executor_cancels_recoverable_attempt_and_removes_checkpoint(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    attempt.state = DirectAcquisitionState.RETRY_PENDING
    artifact = attempt.artifact_attempts[0]
    artifact.state = DirectArtifactState.RETRY_PENDING
    session.add(attempt)
    await session.commit()
    quarantine = DirectArtifactQuarantine(tmp_path / "quarantine")
    workspace = quarantine.prepare(acquisition_id=attempt.id, artifact_id=artifact.id)
    workspace.partial_path.write_bytes(b"restartable checkpoint")
    artifact.quarantine_path = str(workspace.partial_path)
    artifact.bytes_transferred = workspace.partial_path.stat().st_size
    await session.commit()
    executor = DirectAcquisitionExecutor(
        host_resolver=object(),
        http_transport=object(),
        mega_runner=object(),
        quarantine=quarantine,
        post_processor=_successful_post_processor,
        now=lambda: NOW,
    )

    cancelled = await executor.cancel(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
    )

    assert cancelled is True
    assert attempt.state is DirectAcquisitionState.CANCELLED
    assert artifact.state is DirectArtifactState.CANCELLED
    assert artifact.quarantine_path is None
    assert artifact.bytes_transferred == 0
    assert not workspace.directory.exists()


@pytest.mark.asyncio
async def test_executor_schedules_retry_without_losing_safe_partial(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    session.add(attempt)
    await session.commit()
    artifact = attempt.artifact_attempts[0]
    error = ArtifactTransferError(
        code="artifact_host_unavailable",
        message="The artifact host is temporarily unavailable.",
        failure_class=DirectArtifactFailureClass.TRANSIENT_HOST,
        retryable=True,
        intervention=False,
    )

    result = await _executor(
        tmp_path,
        transport=_FailingTransport(error, write_partial=True),
        post_processor=_successful_post_processor,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=_async_source,
    )

    assert result.state is DirectAcquisitionState.RETRY_PENDING
    assert artifact.state is DirectArtifactState.RETRY_PENDING
    assert artifact.retry_count == 1
    assert attempt.retry_count == 1
    assert artifact.next_retry_at is not None
    assert Path(artifact.quarantine_path or "").exists()


def test_executor_recovers_checksum_protected_partial_without_http_validator(
    tmp_path: Path,
) -> None:
    quarantine = DirectArtifactQuarantine(tmp_path / "quarantine")
    workspace = quarantine.prepare(acquisition_id=1, artifact_id=1)
    workspace.partial_path.write_bytes(b"partial")
    artifact = _attempt().artifact_attempts[0]
    artifact.etag = None
    artifact.last_modified_at = None
    artifact.expected_size = 100

    checkpoint = _recover_http_checkpoint(
        workspace,
        artifact,
        ResolvedTransfer(
            host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
            url="https://files.example/signed-secret.pdf",
            expected_size=100,
            checksum="md5:11111111111111111111111111111111",
            range_supported=True,
        ),
    )

    assert checkpoint is not None
    assert checkpoint.bytes_transferred == len(b"partial")
    assert workspace.partial_path.exists()


@pytest.mark.asyncio
async def test_executor_falls_back_to_next_ranked_route_without_blocking_provider(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    artifact = attempt.artifact_attempts[0]
    artifact.artifact_identity = "route:mega"
    artifact.host_kind = DirectArtifactHostKind.MEGA
    artifact.expected_size = 55_574_528
    attempt.plan_snapshot = {
        "schema_version": 1,
        "provider_identity": "community.test",
        "provider_candidate_id": "candidate-1",
        "provider_state": "healthy",
        "selected_artifact_identity": "route:mega",
        "artifacts": [
            {
                "artifact_identity": "route:mega",
                "content_identity": "artifact:primary",
                "route_kind": "direct",
                "host_kind": "mega",
                "eligible": True,
                "eligibility_code": "eligible",
                "expected_size": 55_574_528,
            },
            {
                "artifact_identity": "route:pixeldrain",
                "content_identity": "artifact:primary",
                "route_kind": "direct",
                "host_kind": "pixeldrain",
                "eligible": True,
                "eligibility_code": "eligible",
                "expected_size": 55_574_528,
            },
        ],
    }
    session.add(attempt)
    await session.commit()
    resolved = ResolvedTransfer(
        host_kind=DirectArtifactHostKind.MEGA,
        url="https://mega.nz/file/id#secret-link-key",
        expected_size=55_574_528,
        allowed_domains=("mega.nz",),
        transport_protocol=ArtifactTransferProtocol.MEGA_BRIDGE,
    )
    executor = DirectAcquisitionExecutor(
        host_resolver=_StaticResolver(resolved),
        http_transport=object(),
        mega_runner=_UnavailableMegaRunner(),
        quarantine=DirectArtifactQuarantine(tmp_path / "quarantine"),
        post_processor=_successful_post_processor,
        now=lambda: NOW,
    )

    result = await executor.execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=lambda: _async_value(
            HostResolutionRequest(
                artifact_identity="route:mega",
                host_kind=DirectArtifactHostKind.MEGA,
                share_url="https://mega.nz/file/id#secret-link-key",
                final_url=None,
                expected_size=55_574_528,
            )
        ),
    )

    await session.refresh(attempt, attribute_names=["artifact_attempts"])
    failed, fallback = sorted(attempt.artifact_attempts, key=lambda item: item.sequence_no)
    assert result.state is DirectAcquisitionState.QUEUED
    assert failed.state is DirectArtifactState.FAILED
    assert failed.is_selected is False
    assert failed.failure_code == "mega_link_unavailable"
    assert fallback.host_kind is DirectArtifactHostKind.PIXELDRAIN
    assert fallback.state is DirectArtifactState.PLANNED
    assert fallback.is_selected is True
    assert attempt.plan_revision == 2
    assert attempt.plan_snapshot["selected_artifact_identity"] == "route:pixeldrain"
    assert attempt.plan_snapshot["route_failures"] == [
        {
            "artifact_identity": "route:mega",
            "failure_class": "permanent_mirror",
            "failure_code": "mega_link_unavailable",
            "host_kind": "mega",
            "sequence_no": 0,
        }
    ]
    assert attempt.provider_identity == "community.test"
    blocklist = (await session.execute(select(BlocklistEntry))).scalars().all()
    assert len(blocklist) == 1
    assert blocklist[0].release_title == "Direct Executor 001 (2026)"
    assert blocklist[0].release_group == "MEGA"
    assert blocklist[0].release_title_normalized.startswith("direct-artifact:")
    assert blocklist[0].download_url == "pullbox-direct://artifact/route:mega"
    assert (
        await BlocklistService.is_release_blocked(
            session,
            "Direct Executor 001 (2026)",
        )
        is False
    )


@pytest.mark.asyncio
async def test_unsafe_route_is_blocklisted_before_same_content_fallback(
    session: AsyncSession,
) -> None:
    attempt = _attempt()
    failed = attempt.artifact_attempts[0]
    failed.artifact_identity = "route:generic"
    failed.failure_class = DirectArtifactFailureClass.UNSAFE_ROUTE
    failed.failure_code = "unsafe_artifact_url"
    failed.error_message = "The artifact URL did not pass public HTTPS safety checks."
    attempt.plan_snapshot = {
        "schema_version": 1,
        "selected_artifact_identity": failed.artifact_identity,
        "artifacts": [
            {
                "artifact_identity": failed.artifact_identity,
                "content_identity": "artifact:primary",
                "route_kind": "direct",
                "host_kind": "generic_https",
                "eligible": True,
                "eligibility_code": "eligible",
            },
            {
                "artifact_identity": "route:pixeldrain",
                "content_identity": "artifact:primary",
                "route_kind": "direct",
                "host_kind": "pixeldrain",
                "eligible": True,
                "eligibility_code": "eligible",
            },
        ],
    }
    session.add(attempt)
    await session.commit()

    fallback = await queue_next_artifact_route(session, attempt, failed, at=NOW)

    assert fallback is not None
    assert fallback.host_kind is DirectArtifactHostKind.PIXELDRAIN
    assert fallback.is_selected is True
    blocklist = (await session.execute(select(BlocklistEntry))).scalars().all()
    assert len(blocklist) == 1
    assert blocklist[0].release_group == "Generic Https"
    assert blocklist[0].download_url == "pullbox-direct://artifact/route:generic"


@pytest.mark.asyncio
async def test_fallback_never_switches_to_a_different_provider_artifact(
    session: AsyncSession,
) -> None:
    attempt = _attempt()
    failed = attempt.artifact_attempts[0]
    failed.artifact_identity = "route:1111"
    failed.host_kind = DirectArtifactHostKind.MEGA
    failed.failure_class = DirectArtifactFailureClass.TRANSIENT_HOST
    failed.failure_code = "artifact_host_unavailable"
    failed.error_message = "The selected host is temporarily unavailable."
    attempt.plan_snapshot = {
        "schema_version": 1,
        "selected_artifact_identity": failed.artifact_identity,
        "artifacts": [
            {
                "artifact_identity": failed.artifact_identity,
                "content_identity": "artifact:issue-1",
                "route_kind": "direct",
                "host_kind": "mega",
                "eligible": True,
                "eligibility_code": "eligible",
                "expected_size": 100,
            },
            {
                "artifact_identity": "route:2222",
                "content_identity": "artifact:issue-2",
                "route_kind": "direct",
                "host_kind": "pixeldrain",
                "eligible": True,
                "eligibility_code": "eligible",
                "expected_size": 100,
            },
        ],
    }
    session.add(attempt)
    await session.commit()

    fallback = await queue_next_artifact_route(session, attempt, failed, at=NOW)

    assert fallback is None
    assert len(attempt.artifact_attempts) == 1


@pytest.mark.asyncio
async def test_fallback_switches_to_explicitly_verified_equivalent_artifact(
    session: AsyncSession,
) -> None:
    attempt = _attempt()
    failed = attempt.artifact_attempts[0]
    failed.artifact_identity = "route:aaaa"
    failed.host_kind = DirectArtifactHostKind.MEDIAFIRE
    failed.failure_class = DirectArtifactFailureClass.TRANSIENT_HOST
    failed.failure_code = "artifact_host_unavailable"
    failed.error_message = "The selected host is temporarily unavailable."
    attempt.plan_snapshot = {
        "schema_version": 1,
        "selected_artifact_identity": failed.artifact_identity,
        "artifacts": [
            {
                "artifact_identity": failed.artifact_identity,
                "content_identity": "artifact:sd",
                "fallback_identity": "coverage:black-science-compendium",
                "route_kind": "direct",
                "host_kind": "mediafire",
                "eligible": True,
                "eligibility_code": "eligible",
                "expected_size": 651 * 1024 * 1024,
            },
            {
                "artifact_identity": "route:bbbb",
                "content_identity": "artifact:hd",
                "fallback_identity": "coverage:black-science-compendium",
                "route_kind": "direct",
                "host_kind": "terabox",
                "eligible": True,
                "eligibility_code": "eligible",
                "expected_size": 2_040_109_466,
            },
        ],
    }
    session.add(attempt)
    await session.commit()

    fallback = await queue_next_artifact_route(session, attempt, failed, at=NOW)

    assert fallback is not None
    assert fallback.host_kind is DirectArtifactHostKind.TERABOX
    assert fallback.artifact_identity == "route:bbbb"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_code", ["provider_artifact_changed", "provider_mirror_changed"])
async def test_executor_falls_back_without_blocklisting_transient_provider_churn(
    session: AsyncSession,
    tmp_path: Path,
    failure_code: str,
) -> None:
    attempt = _attempt()
    artifact = attempt.artifact_attempts[0]
    artifact.artifact_identity = "route:mega"
    artifact.host_kind = DirectArtifactHostKind.MEGA
    attempt.plan_snapshot = {
        "schema_version": 1,
        "provider_identity": "community.test",
        "provider_candidate_id": "candidate-1",
        "provider_state": "healthy",
        "selected_artifact_identity": "route:mega",
        "artifacts": [
            {
                "artifact_identity": "route:mega",
                "content_identity": "artifact:primary",
                "route_kind": "direct",
                "host_kind": "mega",
                "eligible": True,
                "eligibility_code": "eligible",
            },
            {
                "artifact_identity": "route:pixeldrain",
                "content_identity": "artifact:primary",
                "route_kind": "direct",
                "host_kind": "pixeldrain",
                "eligible": True,
                "eligibility_code": "eligible",
            },
        ],
    }
    session.add(attempt)
    await session.commit()

    async def missing_mirror() -> HostResolutionRequest:
        raise DirectAcquisitionPlanningError(
            failure_code,
            "The provider response temporarily omitted the selected source.",
        )

    result = await _executor(
        tmp_path,
        transport=object(),
        post_processor=_successful_post_processor,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=missing_mirror,
    )

    await session.refresh(attempt, attribute_names=["artifact_attempts"])
    failed, fallback = sorted(attempt.artifact_attempts, key=lambda item: item.sequence_no)
    assert result.state is DirectAcquisitionState.QUEUED
    assert failed.failure_class is DirectArtifactFailureClass.RESOLVER
    assert failed.failure_code == failure_code
    assert fallback.host_kind is DirectArtifactHostKind.PIXELDRAIN
    assert fallback.is_selected is True
    assert attempt.provider_identity == "community.test"
    blocklist = (await session.execute(select(BlocklistEntry))).scalars().all()
    assert blocklist == []


@pytest.mark.asyncio
async def test_executor_classifies_provider_authentication_failure_during_reresolution(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    artifact = attempt.artifact_attempts[0]
    session.add(attempt)
    await session.commit()

    async def authentication_failed() -> HostResolutionRequest:
        raise DirectAcquisitionPlanningError(
            "provider_authentication_failed",
            "The direct provider bearer token is unavailable.",
            failure_class=DirectArtifactFailureClass.PROVIDER_UNAVAILABLE,
            retryable=False,
            intervention=True,
        )

    result = await _executor(
        tmp_path,
        transport=object(),
        post_processor=_successful_post_processor,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=authentication_failed,
    )

    assert result.state is DirectAcquisitionState.INTERVENTION
    assert attempt.failure_class is DirectArtifactFailureClass.PROVIDER_UNAVAILABLE
    assert attempt.failure_code == "provider_authentication_failed"
    assert artifact.failure_code == "provider_authentication_failed"


async def _async_value[T](value: T) -> T:
    return value


@pytest.mark.asyncio
async def test_executor_retries_post_processing_from_quarantine_without_redownload(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    session.add(attempt)
    await session.commit()
    artifact = attempt.artifact_attempts[0]

    async def failed_post_processing(*_args: Any, **_kwargs: Any) -> DirectPostProcessingResult:
        raise RuntimeError("synthetic placement failure")

    first = await _executor(
        tmp_path,
        transport=_SuccessfulTransport(),
        post_processor=failed_post_processing,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=_async_source,
    )

    assert first.state is DirectAcquisitionState.INTERVENTION
    assert attempt.failure_class is DirectArtifactFailureClass.POST_PROCESS
    assert artifact.quarantine_path is not None
    assert Path(artifact.quarantine_path).exists()
    source_called = False

    async def unexpected_source() -> HostResolutionRequest:
        nonlocal source_called
        source_called = True
        raise AssertionError("Post-processing retry must reuse the quarantine artifact")

    second = await _executor(
        tmp_path,
        transport=_UnexpectedTransport(),
        post_processor=_successful_post_processor,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=unexpected_source,
    )

    assert source_called is False
    assert second.state is DirectAcquisitionState.COMPLETED
    assert artifact.state is DirectArtifactState.COMPLETED
    assert artifact.quarantine_path is None


@pytest.mark.asyncio
async def test_executor_applies_approved_resource_exception_without_redownload(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    attempt.state = DirectAcquisitionState.POST_PROCESSING
    attempt.plan_snapshot = {
        "safety_review": {
            "kind": "archive_decompressed_size",
            "overrideable": True,
            "allowed_once": True,
        }
    }
    artifact = attempt.artifact_attempts[0]
    artifact.state = DirectArtifactState.VALIDATING
    session.add(attempt)
    await session.commit()
    quarantine = DirectArtifactQuarantine(tmp_path / "quarantine")
    workspace = quarantine.prepare(acquisition_id=attempt.id, artifact_id=artifact.id)
    final_path = workspace.directory / f"artifact-{artifact.id}.cbz"
    final_path.write_bytes(b"completed transfer")
    artifact.quarantine_path = str(final_path)
    await session.commit()
    observed: dict[str, object] = {}

    async def post_processor(*_args: Any, **kwargs: Any) -> DirectPostProcessingResult:
        observed.update(kwargs)
        return await _successful_post_processor()

    async def unexpected_source() -> HostResolutionRequest:
        raise AssertionError("Approved safety review must not resolve or download again")

    result = await _executor(
        tmp_path,
        transport=_UnexpectedTransport(),
        post_processor=post_processor,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=unexpected_source,
    )

    assert result.state is DirectAcquisitionState.COMPLETED
    assert observed["source_path"] == final_path
    assert observed["allow_resource_safety_exception"] is True


@pytest.mark.asyncio
async def test_executor_recovers_inflight_transfer_from_persisted_checkpoint(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    attempt.state = DirectAcquisitionState.DOWNLOADING
    artifact = attempt.artifact_attempts[0]
    artifact.state = DirectArtifactState.TRANSFERRING
    session.add(attempt)
    await session.commit()
    quarantine = DirectArtifactQuarantine(tmp_path / "quarantine")
    workspace = quarantine.prepare(acquisition_id=attempt.id, artifact_id=artifact.id)
    workspace.partial_path.write_bytes(b"partial")
    artifact.quarantine_path = str(workspace.partial_path)
    artifact.bytes_transferred = workspace.partial_path.stat().st_size
    await session.commit()
    transport = _SuccessfulTransport()
    resolved = ResolvedTransfer(
        host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
        url="https://files.example/refreshed.cbz",
        etag='"stable"',
        allowed_domains=("files.example",),
    )
    executor = DirectAcquisitionExecutor(
        host_resolver=_FakeResolver(resolved),
        http_transport=transport,
        mega_runner=object(),
        quarantine=quarantine,
        post_processor=_successful_post_processor,
        now=lambda: NOW,
    )

    result = await executor.execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=_async_source,
    )

    assert result.state is DirectAcquisitionState.COMPLETED
    assert transport.checkpoint is not None
    assert transport.checkpoint.bytes_transferred == len(b"partial")
    assert transport.checkpoint.etag == '"stable"'


@pytest.mark.asyncio
async def test_credentialed_transfer_records_reachable_successful_host_state(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    artifact = attempt.artifact_attempts[0]
    artifact.host_kind = DirectArtifactHostKind.PIXELDRAIN
    config = DirectHostConfig(
        host_kind=DirectArtifactHostKind.PIXELDRAIN,
        enabled=True,
    )
    update_host_credentials(config, {"api_key": "configured-pixeldrain-key"})
    session.add_all([attempt, config])
    await session.commit()
    resolved = ResolvedTransfer(
        host_kind=DirectArtifactHostKind.PIXELDRAIN,
        url="https://pixeldrain.com/api/file/fixture",
        etag='"stable"',
        allowed_domains=("pixeldrain.com",),
    )

    async def source() -> HostResolutionRequest:
        return HostResolutionRequest(
            artifact_identity="artifact-1",
            host_kind=DirectArtifactHostKind.PIXELDRAIN,
            share_url="https://pixeldrain.com/u/fixture",
            final_url=None,
        )

    executor = DirectAcquisitionExecutor(
        host_resolver=_AccountResolver(resolved=resolved),
        http_transport=_SuccessfulTransport(),
        mega_runner=object(),
        quarantine=DirectArtifactQuarantine(tmp_path / "quarantine"),
        post_processor=_successful_post_processor,
        now=lambda: NOW,
    )

    result = await executor.execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=source,
    )

    await session.refresh(config)
    assert result.state is DirectAcquisitionState.COMPLETED
    assert config.account_state is DirectHostAccountState.HEALTHY
    assert config.reachability_state is DirectHostReachabilityState.REACHABLE
    assert config.last_reachable_at == NOW
    assert config.last_operational_result is DirectHostOperationalResult.SUCCESSFUL
    assert config.last_operational_at == NOW
    assert config.last_tested_at is None
    assert config.last_error_code is None


@pytest.mark.asyncio
async def test_credentialed_auth_failure_records_reauthentication_state(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    artifact = attempt.artifact_attempts[0]
    artifact.host_kind = DirectArtifactHostKind.PIXELDRAIN
    config = DirectHostConfig(
        host_kind=DirectArtifactHostKind.PIXELDRAIN,
        enabled=True,
    )
    update_host_credentials(config, {"api_key": "configured-pixeldrain-key"})
    session.add_all([attempt, config])
    await session.commit()
    error = ArtifactHostResolutionError(
        code="artifact_host_auth_required",
        message="This artifact host requires account authentication.",
        failure_class=DirectArtifactFailureClass.ARTIFACT_HOST_AUTH_REQUIRED,
        retryable=False,
        intervention=True,
    )

    async def source() -> HostResolutionRequest:
        return HostResolutionRequest(
            artifact_identity="artifact-1",
            host_kind=DirectArtifactHostKind.PIXELDRAIN,
            share_url="https://pixeldrain.com/u/fixture",
            final_url=None,
        )

    executor = DirectAcquisitionExecutor(
        host_resolver=_AccountResolver(error=error),
        http_transport=object(),
        mega_runner=object(),
        quarantine=DirectArtifactQuarantine(tmp_path / "quarantine"),
        post_processor=_successful_post_processor,
        now=lambda: NOW,
    )

    result = await executor.execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=source,
    )

    await session.refresh(config)
    assert result.state is DirectAcquisitionState.INTERVENTION
    assert config.account_state is DirectHostAccountState.AUTHENTICATION_REQUIRED
    assert config.reachability_state is DirectHostReachabilityState.AUTHENTICATION_REQUIRED
    assert config.last_reachable_at == NOW
    assert config.last_operational_result is DirectHostOperationalResult.FAILED
    assert config.last_operational_at == NOW
    assert config.last_tested_at is None
    assert config.last_error_code == "artifact_host_auth_required"


@pytest.mark.asyncio
async def test_anonymous_transfer_records_operational_result(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    artifact = attempt.artifact_attempts[0]
    artifact.host_kind = DirectArtifactHostKind.MEDIAFIRE
    config = DirectHostConfig(
        host_kind=DirectArtifactHostKind.MEDIAFIRE,
        enabled=True,
    )
    session.add_all([attempt, config])
    await session.commit()
    resolved = ResolvedTransfer(
        host_kind=DirectArtifactHostKind.MEDIAFIRE,
        url="https://download.mediafire.com/fixture.cbz",
        allowed_domains=("mediafire.com",),
    )

    async def source() -> HostResolutionRequest:
        return HostResolutionRequest(
            artifact_identity="artifact-1",
            host_kind=DirectArtifactHostKind.MEDIAFIRE,
            share_url="https://www.mediafire.com/file/fixture",
            final_url=None,
        )

    executor = DirectAcquisitionExecutor(
        host_resolver=_FakeResolver(resolved),
        http_transport=_SuccessfulTransport(),
        mega_runner=object(),
        quarantine=DirectArtifactQuarantine(tmp_path / "quarantine"),
        post_processor=_successful_post_processor,
        now=lambda: NOW,
    )

    result = await executor.execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=source,
    )

    await session.refresh(config)
    assert result.state is DirectAcquisitionState.COMPLETED
    assert config.account_state is DirectHostAccountState.NOT_CONFIGURED
    assert config.reachability_state is DirectHostReachabilityState.REACHABLE
    assert config.last_operational_result is DirectHostOperationalResult.SUCCESSFUL
    assert config.last_operational_at == NOW


@pytest.mark.asyncio
async def test_process_task_cancellation_preserves_restartable_inflight_state(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    session.add(attempt)
    await session.commit()
    artifact = attempt.artifact_attempts[0]

    with pytest.raises(asyncio.CancelledError):
        await _executor(
            tmp_path,
            transport=_CancelledTaskTransport(),
            post_processor=_successful_post_processor,
        ).execute(
            session,
            acquisition_id=attempt.id,
            artifact_id=artifact.id,
            source_factory=_async_source,
        )

    await session.refresh(attempt)
    await session.refresh(artifact)
    assert attempt.state is DirectAcquisitionState.DOWNLOADING
    assert artifact.state is DirectArtifactState.TRANSFERRING
    assert artifact.bytes_transferred == len(b"restartable-partial")
    assert Path(artifact.quarantine_path or "").read_bytes() == b"restartable-partial"


@pytest.mark.asyncio
async def test_mega_pause_discards_partial_and_restarts_from_zero(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    artifact = attempt.artifact_attempts[0]
    artifact.host_kind = DirectArtifactHostKind.MEGA
    session.add(attempt)
    await session.commit()

    async def mega_source() -> HostResolutionRequest:
        return HostResolutionRequest(
            artifact_identity="artifact-1",
            host_kind=DirectArtifactHostKind.MEGA,
            share_url="https://mega.nz/file/fixture#fixture-key",
            final_url=None,
            expected_size=100,
        )

    resolved = ResolvedTransfer(
        host_kind=DirectArtifactHostKind.MEGA,
        url="https://mega.nz/file/fixture#fixture-key",
        expected_size=100,
        allowed_domains=("mega.nz",),
        transport_protocol=ArtifactTransferProtocol.MEGA_BRIDGE,
    )
    executor = DirectAcquisitionExecutor(
        host_resolver=_FakeResolver(resolved),
        http_transport=object(),
        mega_runner=_PausedMegaRunner(),
        quarantine=DirectArtifactQuarantine(tmp_path / "quarantine"),
        post_processor=_successful_post_processor,
        now=lambda: NOW,
    )

    result = await executor.execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=mega_source,
    )

    assert result.state is DirectAcquisitionState.PAUSED
    assert artifact.state is DirectArtifactState.PAUSED
    assert artifact.bytes_transferred == 0
    assert artifact.etag is None
    assert not Path(artifact.quarantine_path or "").exists()


@pytest.mark.asyncio
async def test_executor_forwards_mega_session_to_bridge_only(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    account_session = "mega-revocable-session"
    attempt = _attempt()
    artifact = attempt.artifact_attempts[0]
    artifact.host_kind = DirectArtifactHostKind.MEGA
    session.add(attempt)
    await session.commit()

    async def mega_source() -> HostResolutionRequest:
        return HostResolutionRequest(
            artifact_identity="artifact-1",
            host_kind=DirectArtifactHostKind.MEGA,
            share_url="https://mega.nz/file/fixture#fixture-key",
            final_url=None,
        )

    checksum = f"sha256:{'a' * 64}"
    resolved = ResolvedTransfer(
        host_kind=DirectArtifactHostKind.MEGA,
        url="https://mega.nz/file/fixture#fixture-key",
        allowed_domains=("mega.nz",),
        transport_protocol=ArtifactTransferProtocol.MEGA_BRIDGE,
        bridge_session=account_session,
        checksum=checksum,
    )
    mega_runner = _SuccessfulMegaRunner()
    executor = DirectAcquisitionExecutor(
        host_resolver=_FakeResolver(resolved),
        http_transport=object(),
        mega_runner=mega_runner,
        quarantine=DirectArtifactQuarantine(tmp_path / "quarantine"),
        post_processor=_successful_post_processor,
        now=lambda: NOW,
    )

    result = await executor.execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=mega_source,
    )

    assert result.state is DirectAcquisitionState.COMPLETED
    assert mega_runner.session == account_session
    assert mega_runner.checksum == checksum
    assert account_session not in repr(attempt.progress_snapshot)


@pytest.mark.asyncio
async def test_post_processing_failure_keeps_valid_artifact_for_intervention(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    session.add(attempt)
    await session.commit()
    artifact = attempt.artifact_attempts[0]

    async def fail_post_processing(*_args: Any, **_kwargs: Any) -> DirectPostProcessingResult:
        raise RuntimeError("synthetic post-processing failure")

    result = await _executor(
        tmp_path,
        transport=_SuccessfulTransport(),
        post_processor=fail_post_processing,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=_async_source,
    )

    assert result.state is DirectAcquisitionState.INTERVENTION
    assert artifact.state is DirectArtifactState.INTERVENTION
    assert attempt.failure_class is DirectArtifactFailureClass.POST_PROCESS
    assert artifact.quarantine_path is not None
    assert Path(artifact.quarantine_path).exists()
    pending = (
        await session.execute(select(PendingMatch).where(PendingMatch.issue_id == attempt.issue_id))
    ).scalar_one()
    assert pending.status == PendingMatchStatus.PENDING
    assert pending.download_url == f"pullbox-direct://attempt/{attempt.id}"
    assert pending.match_details["failure_code"] == "direct_post_processing_failed"
    history = (
        await session.execute(
            select(DownloadHistory).where(DownloadHistory.external_id == f"direct:{attempt.id}")
        )
    ).scalar_one()
    post_processing_operation = (
        await session.execute(
            select(OperationProgress).where(
                OperationProgress.operation_type == OperationProgressType.POST_PROCESSING,
                OperationProgress.operation_key == str(history.id),
            )
        )
    ).scalar_one()
    assert post_processing_operation.state is OperationProgressState.FAILED


@pytest.mark.asyncio
async def test_post_processing_database_failure_rolls_back_before_intervention(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    session.add(SystemConfig(key="direct-review-rollback", value="first", value_type="string"))
    attempt = _attempt()
    session.add(attempt)
    await session.commit()
    artifact = attempt.artifact_attempts[0]

    async def fail_flush(*_args: Any, **_kwargs: Any) -> DirectPostProcessingResult:
        session.add(
            SystemConfig(key="direct-review-rollback", value="duplicate", value_type="string")
        )
        await session.flush()
        raise AssertionError("The duplicate key flush must fail")

    result = await _executor(
        tmp_path,
        transport=_SuccessfulTransport(),
        post_processor=fail_flush,
    ).execute(
        session,
        acquisition_id=attempt.id,
        artifact_id=artifact.id,
        source_factory=_async_source,
    )

    assert result.state is DirectAcquisitionState.INTERVENTION
    pending = (
        await session.execute(select(PendingMatch).where(PendingMatch.issue_id == attempt.issue_id))
    ).scalar_one()
    assert pending.match_details["failure_code"] == "direct_post_processing_failed"


async def _async_source() -> HostResolutionRequest:
    await asyncio.sleep(0)
    return _source_request()

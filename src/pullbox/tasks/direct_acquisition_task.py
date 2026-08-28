"""Restart-safe background dispatch for direct acquisition attempts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
    DirectArtifactFailureClass,
    DirectArtifactState,
)
from pullbox.providers.artifact_hosts.contract import HostResolutionRequest
from pullbox.services.blocklist_service import BlocklistService
from pullbox.services.direct_acquisition_fallback import (
    queue_next_artifact_route,
    supports_route_fallback,
)
from pullbox.services.direct_acquisition_planner_service import (
    DirectAcquisitionPlanningError,
    DirectAcquisitionPlanningResult,
    plan_direct_acquisition_with_provider_fallback,
    resolve_planned_artifact_source,
)
from pullbox.services.direct_acquisition_recovery import (
    load_due_retry_acquisitions,
    load_recoverable_acquisitions,
)
from pullbox.services.direct_acquisition_state import (
    advance_acquisition_progress,
    reopen_terminal_acquisition_for_retry,
    transition_acquisition,
    transition_artifact,
)
from pullbox.services.direct_acquisition_switch import (
    DirectSourceSwitchError,
    DirectSourceSwitchOutcome,
    list_source_switch_options,
    queue_source_switch,
)
from pullbox.services.direct_download_history_adapter import (
    ensure_direct_download_history,
    sync_direct_download_history,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)


class DirectExecutor(Protocol):
    async def execute(
        self,
        session: AsyncSession,
        *,
        acquisition_id: int,
        artifact_id: int,
        source_factory: Callable[[], Awaitable[HostResolutionRequest]],
        cancel_event: asyncio.Event | None = None,
    ) -> object: ...

    async def cancel(
        self,
        session: AsyncSession,
        *,
        acquisition_id: int,
        artifact_id: int,
    ) -> bool: ...


SourceResolver = Callable[..., Awaitable[HostResolutionRequest]]
ProviderFallbackPlanner = Callable[..., Awaitable[DirectAcquisitionPlanningResult]]


@dataclass(frozen=True, slots=True)
class _ActiveRun:
    task: asyncio.Task[None]
    cancel_event: asyncio.Event


class DirectAcquisitionRunner:
    """Dispatch durable attempts once and reconstruct ephemeral URLs on recovery."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        executor: DirectExecutor,
        source_resolver: SourceResolver = resolve_planned_artifact_source,
        provider_fallback_planner: ProviderFallbackPlanner = (
            plan_direct_acquisition_with_provider_fallback
        ),
    ) -> None:
        self._session_factory = session_factory
        self._executor = executor
        self._source_resolver = source_resolver
        self._provider_fallback_planner = provider_fallback_planner
        self._active: dict[tuple[int, int], _ActiveRun] = {}
        self._lock = asyncio.Lock()

    async def dispatch(
        self,
        acquisition_id: int,
        artifact_id: int,
        *,
        initial_source: HostResolutionRequest | None = None,
    ) -> bool:
        """Queue one attempt, returning false when the same artifact is already active."""
        async with self._lock:
            return await self._dispatch_locked(
                acquisition_id,
                artifact_id,
                initial_source=initial_source,
            )

    async def cancel(self, acquisition_id: int) -> bool:
        """Signal cooperative cancellation for an active direct acquisition."""
        async with self._lock:
            active = [
                run
                for key, run in self._active.items()
                if key[0] == acquisition_id and not run.task.done()
            ]
            for run in active:
                run.cancel_event.set()
        if not active:
            async with self._session_factory() as session:
                attempt = (
                    await session.execute(
                        select(DirectAcquisitionAttempt)
                        .options(selectinload(DirectAcquisitionAttempt.artifact_attempts))
                        .where(DirectAcquisitionAttempt.id == acquisition_id)
                    )
                ).scalar_one_or_none()
                if attempt is None:
                    return False
                selected = [
                    artifact for artifact in attempt.artifact_attempts if artifact.is_selected
                ]
                if len(selected) != 1:
                    return False
                return await self._executor.cancel(
                    session,
                    acquisition_id=attempt.id,
                    artifact_id=selected[0].id,
                )
        await asyncio.gather(*(run.task for run in active), return_exceptions=True)
        return True

    async def retry(self, acquisition_id: int) -> bool:
        """Resume an intervention or explicitly reopen one terminal direct attempt."""
        async with self._lock:
            if any(
                key[0] == acquisition_id and not run.task.done()
                for key, run in self._active.items()
            ):
                return False

        async with self._session_factory() as session:
            attempt = (
                await session.execute(
                    select(DirectAcquisitionAttempt)
                    .options(selectinload(DirectAcquisitionAttempt.artifact_attempts))
                    .where(DirectAcquisitionAttempt.id == acquisition_id)
                )
            ).scalar_one_or_none()
            if attempt is None:
                return False
            selected = [artifact for artifact in attempt.artifact_attempts if artifact.is_selected]
            if len(selected) != 1:
                return False
            artifact = selected[0]
            blocked = await BlocklistService.get_blocked_direct_artifact_routes(
                session,
                {artifact.artifact_identity},
            )
            if attempt.state in {
                DirectAcquisitionState.FAILED,
                DirectAcquisitionState.CANCELLED,
            }:
                reopen_terminal_acquisition_for_retry(attempt, artifact)
            elif attempt.state is DirectAcquisitionState.INTERVENTION:
                advance_acquisition_progress(
                    attempt,
                    revision=attempt.progress_revision + 1,
                    snapshot={
                        "schema_version": 1,
                        "stage": "retry_requested",
                        "artifact_attempt_id": artifact.id,
                    },
                )
            else:
                return False
            if artifact.artifact_identity in blocked:
                fallback = await queue_next_artifact_route(
                    session,
                    attempt,
                    artifact,
                    at=datetime.now(UTC),
                )
                if fallback is None:
                    await session.rollback()
                    return False
                artifact_id = fallback.id
            else:
                await sync_direct_download_history(
                    session,
                    attempt,
                    artifact,
                    at=datetime.now(UTC),
                )
                await session.commit()
                artifact_id = artifact.id

        return await self.dispatch(acquisition_id, artifact_id)

    async def switch_source(
        self,
        acquisition_id: int,
        *,
        target_artifact_identity: str | None = None,
        block_current: bool = False,
    ) -> DirectSourceSwitchOutcome:
        """Stop one transfer, queue an equivalent route, and restart the acquisition."""
        async with self._lock:
            async with self._session_factory() as session:
                attempt = await self._load_attempt_with_artifacts(session, acquisition_id)
                current = [
                    artifact for artifact in attempt.artifact_attempts if artifact.is_selected
                ]
                if len(current) != 1:
                    raise DirectSourceSwitchError(
                        "source_switch_selection_invalid",
                        "This direct download does not have one active artifact source.",
                    )
                discarded_bytes = current[0].bytes_transferred
                options = await list_source_switch_options(session, attempt)
                if target_artifact_identity is None:
                    selected_identity = options[0].artifact_identity if options else None
                else:
                    selected_identity = next(
                        (
                            option.artifact_identity
                            for option in options
                            if option.artifact_identity == target_artifact_identity
                        ),
                        None,
                    )
                if selected_identity is None:
                    raise DirectSourceSwitchError(
                        "source_switch_route_unavailable",
                        "No other verified artifact source is available for this download.",
                    )

            active = [
                run
                for key, run in self._active.items()
                if key[0] == acquisition_id and not run.task.done()
            ]
            for run in active:
                run.cancel_event.set()
            if active:
                await asyncio.gather(*(run.task for run in active), return_exceptions=True)

            async with self._session_factory() as session:
                attempt = await self._load_attempt_with_artifacts(session, acquisition_id)
                current = [
                    artifact for artifact in attempt.artifact_attempts if artifact.is_selected
                ]
                if len(current) != 1:
                    raise DirectSourceSwitchError(
                        "source_switch_selection_invalid",
                        "This direct download does not have one active artifact source.",
                    )
                if attempt.state is not DirectAcquisitionState.CANCELLED:
                    cancelled = await self._executor.cancel(
                        session,
                        acquisition_id=attempt.id,
                        artifact_id=current[0].id,
                    )
                    if not cancelled:
                        raise DirectSourceSwitchError(
                            "source_switch_not_cancellable",
                            "This direct download can no longer change sources.",
                        )
                outcome = await queue_source_switch(
                    session,
                    attempt,
                    current[0],
                    target_artifact_identity=selected_identity,
                    block_current=block_current,
                    discarded_bytes=discarded_bytes,
                    at=datetime.now(UTC),
                )
                replacement_id = outcome.selected.id

            dispatched = await self._dispatch_locked(acquisition_id, replacement_id)
            if not dispatched:
                raise DirectSourceSwitchError(
                    "source_switch_dispatch_failed",
                    "The replacement source was queued but could not start right now.",
                )
            return outcome

    async def recover_and_dispatch(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        """Resume runnable persisted attempts without any previously signed URLs."""
        async with self._session_factory() as session:
            attempts = await load_recoverable_acquisitions(
                session,
                now=now or datetime.now(UTC),
                limit=limit,
            )
            recoverable = [
                (attempt.id, artifact.id)
                for attempt in attempts
                for artifact in attempt.artifact_attempts
                if artifact.is_selected
            ]
        recovered = 0
        for acquisition_id, artifact_id in recoverable:
            if await self.dispatch(acquisition_id, artifact_id):
                recovered += 1
        return recovered

    async def dispatch_due_retries(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        """Dispatch retries whose durable backoff deadline has elapsed."""
        async with self._session_factory() as session:
            attempts = await load_due_retry_acquisitions(
                session,
                now=now or datetime.now(UTC),
                limit=limit,
            )
            due = [
                (attempt.id, artifact.id)
                for attempt in attempts
                for artifact in attempt.artifact_attempts
                if artifact.is_selected
            ]
        dispatched = 0
        for acquisition_id, artifact_id in due:
            if await self.dispatch(acquisition_id, artifact_id):
                dispatched += 1
        return dispatched

    async def wait_idle(self) -> None:
        """Wait until all currently dispatched attempts reach a checkpoint."""
        while True:
            tasks = tuple(run.task for run in self._active.values() if not run.task.done())
            if not tasks:
                return
            await asyncio.gather(*tasks)

    async def aclose(self) -> None:
        """Cancel active workers, preserving executor restart checkpoints."""
        tasks = tuple(run.task for run in self._active.values() if not run.task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        close = getattr(self._executor, "aclose", None)
        if callable(close):
            await close()

    async def _mark_queued(self, acquisition_id: int, artifact_id: int) -> None:
        async with self._session_factory() as session:
            attempt = await session.get(DirectAcquisitionAttempt, acquisition_id)
            artifact = await session.get(DirectArtifactAttempt, artifact_id)
            if attempt is None or artifact is None or artifact.acquisition_attempt_id != attempt.id:
                raise ValueError("Direct acquisition attempt or artifact was not found.")
            at = datetime.now(UTC)
            await ensure_direct_download_history(
                session,
                attempt,
                artifact,
                at=at,
            )
            if attempt.state is DirectAcquisitionState.PLANNED:
                transition_acquisition(attempt, DirectAcquisitionState.QUEUED)
            await sync_direct_download_history(session, attempt, artifact, at=at)
            await session.commit()

    async def _dispatch_locked(
        self,
        acquisition_id: int,
        artifact_id: int,
        *,
        initial_source: HostResolutionRequest | None = None,
    ) -> bool:
        key = (acquisition_id, artifact_id)
        current = self._active.get(key)
        if current is not None and not current.task.done():
            return False
        await self._mark_queued(acquisition_id, artifact_id)
        cancel_event = asyncio.Event()
        task = asyncio.create_task(
            self._run(
                acquisition_id,
                artifact_id,
                initial_source=initial_source,
                cancel_event=cancel_event,
            ),
            name=f"direct-acquisition-{acquisition_id}-{artifact_id}",
        )
        self._active[key] = _ActiveRun(task=task, cancel_event=cancel_event)

        def finish(completed: asyncio.Task[None]) -> None:
            self._finish(key, completed)

        task.add_done_callback(finish)
        return True

    async def _load_attempt_with_artifacts(
        self,
        session: AsyncSession,
        acquisition_id: int,
    ) -> DirectAcquisitionAttempt:
        attempt = (
            await session.execute(
                select(DirectAcquisitionAttempt)
                .options(selectinload(DirectAcquisitionAttempt.artifact_attempts))
                .where(DirectAcquisitionAttempt.id == acquisition_id)
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise DirectSourceSwitchError(
                "source_switch_not_found",
                "The direct download could not be found.",
            )
        return attempt

    async def _run(
        self,
        acquisition_id: int,
        artifact_id: int,
        *,
        initial_source: HostResolutionRequest | None,
        cancel_event: asyncio.Event,
    ) -> None:
        first_source = initial_source
        current_artifact_id = artifact_id

        async with self._session_factory() as session:
            while True:
                artifact_id_for_source = current_artifact_id

                async def source_factory(
                    artifact_id: int = artifact_id_for_source,
                ) -> HostResolutionRequest:
                    nonlocal first_source
                    if first_source is not None:
                        source = first_source
                        first_source = None
                        return source
                    return await self._source_resolver(
                        session,
                        acquisition_id=acquisition_id,
                        artifact_id=artifact_id,
                    )

                try:
                    await self._executor.execute(
                        session,
                        acquisition_id=acquisition_id,
                        artifact_id=current_artifact_id,
                        source_factory=source_factory,
                        cancel_event=cancel_event,
                    )
                except Exception:
                    await session.rollback()
                    await self._mark_unexpected_failure(
                        session,
                        acquisition_id=acquisition_id,
                        artifact_id=current_artifact_id,
                    )
                    raise
                attempt = (
                    await session.execute(
                        select(DirectAcquisitionAttempt)
                        .options(selectinload(DirectAcquisitionAttempt.artifact_attempts))
                        .where(DirectAcquisitionAttempt.id == acquisition_id)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if _supports_provider_fallback(attempt):
                    try:
                        planned = await self._provider_fallback_planner(
                            session,
                            acquisition_id=attempt.id,
                            skip_selected_attempt=True,
                        )
                    except DirectAcquisitionPlanningError:
                        return
                    await session.commit()
                    await self.dispatch(
                        planned.attempt.id,
                        planned.selected_artifact.id,
                        initial_source=planned.initial_source,
                    )
                    return
                if attempt.state is not DirectAcquisitionState.QUEUED:
                    return
                selected = [
                    artifact for artifact in attempt.artifact_attempts if artifact.is_selected
                ]
                if len(selected) != 1 or selected[0].id == current_artifact_id:
                    raise RuntimeError("Queued direct fallback has no new selected artifact.")
                current_artifact_id = selected[0].id

    async def _mark_unexpected_failure(
        self,
        session: AsyncSession,
        *,
        acquisition_id: int,
        artifact_id: int,
    ) -> None:
        attempt = await session.get(DirectAcquisitionAttempt, acquisition_id)
        artifact = await session.get(DirectArtifactAttempt, artifact_id)
        if attempt is None or artifact is None:
            return
        if attempt.state in {
            DirectAcquisitionState.COMPLETED,
            DirectAcquisitionState.CANCELLED,
            DirectAcquisitionState.FAILED,
        }:
            return
        attempt.failure_class = DirectArtifactFailureClass.TRANSIENT_SOURCE
        attempt.failure_code = "direct_acquisition_worker_failed"
        attempt.error_message = "Direct acquisition stopped unexpectedly."
        artifact.failure_class = DirectArtifactFailureClass.TRANSIENT_SOURCE
        artifact.failure_code = "direct_acquisition_worker_failed"
        artifact.error_message = "Direct acquisition stopped unexpectedly."
        transition_artifact(artifact, DirectArtifactState.FAILED)
        transition_acquisition(attempt, DirectAcquisitionState.FAILED)
        advance_acquisition_progress(
            attempt,
            revision=attempt.progress_revision + 1,
            snapshot={
                "schema_version": 1,
                "stage": "failed",
                "artifact_attempt_id": artifact.id,
                "failure_code": "direct_acquisition_worker_failed",
            },
        )
        await sync_direct_download_history(
            session,
            attempt,
            artifact,
            at=datetime.now(UTC),
        )
        await session.commit()

    def _finish(self, key: tuple[int, int], task: asyncio.Task[None]) -> None:
        self._active.pop(key, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "direct_acquisition_worker_failed",
                acquisition_id=key[0],
                artifact_id=key[1],
                error_type=type(error).__name__,
            )


def _supports_provider_fallback(attempt: DirectAcquisitionAttempt) -> bool:
    """Return whether a failed attempt has a safe linked provider alternate."""
    raw_alternates = attempt.candidate_snapshot.get("alternate_attempt_ids", [])
    has_alternate = isinstance(raw_alternates, list) and any(
        isinstance(value, int) and value > 0 for value in raw_alternates
    )
    failure_class = attempt.failure_class
    if attempt.state is not DirectAcquisitionState.FAILED or not has_alternate:
        return False
    if failure_class is None:
        return False
    return supports_route_fallback(failure_class) or failure_class in {
        DirectArtifactFailureClass.PROVIDER_UNAVAILABLE,
        DirectArtifactFailureClass.CANDIDATE_INVALID,
    }


_runner: DirectAcquisitionRunner | None = None


def set_direct_acquisition_runner(runner: DirectAcquisitionRunner | None) -> None:
    """Set the process-local runner used by API and startup dispatch."""
    global _runner
    _runner = runner


def get_direct_acquisition_runner() -> DirectAcquisitionRunner:
    """Return the initialized direct runner."""
    if _runner is None:
        raise RuntimeError("Direct acquisition runner is not initialized.")
    return _runner

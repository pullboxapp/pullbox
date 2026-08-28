"""Route one unified search winner to its native acquisition adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pullbox.core.exceptions import ProviderError
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactFailureClass,
    DirectProviderConfig,
    DirectProviderState,
)
from pullbox.models.download import DownloadState
from pullbox.services.direct_acquisition_planner_service import (
    DirectAcquisitionPlanningError,
    DirectAcquisitionPlanningResult,
    plan_direct_acquisition,
)
from pullbox.services.direct_acquisition_state import (
    advance_acquisition_progress,
    transition_acquisition,
)
from pullbox.services.direct_provider_quota import (
    automatic_quota_available,
    automatic_quota_reserve,
    provider_quota_status,
    provider_supports_quota,
)
from pullbox.services.direct_search_coordinator import persist_direct_search_discoveries
from pullbox.services.search_runtime import should_auto_grab
from pullbox.services.search_source_selection import rank_search_sources

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.providers.artifact_hosts.contract import HostResolutionRequest
    from pullbox.providers.base import ReleaseResult
    from pullbox.services.direct_search_coordinator import (
        DirectSearchDiscovery,
        DirectValidatedCandidate,
    )
    from pullbox.services.release_validator import ValidationResult
    from pullbox.services.search_targets import IssueSearchOutcome
    from pullbox.services.search_types import SearchEvalKwargs


class DownloadServiceLike(Protocol):
    async def send_to_client(
        self,
        session: AsyncSession,
        release: ReleaseResult,
        issue_id: int,
    ) -> object: ...


class InterventionServiceLike(Protocol):
    async def has_pending_for_issue(self, session: AsyncSession, issue_id: int) -> bool: ...

    async def create_pending_match(
        self,
        session: AsyncSession,
        issue_id: int,
        release: ReleaseResult,
        validation: ValidationResult,
    ) -> object: ...

    async def create_direct_pending_match(
        self,
        session: AsyncSession,
        issue_id: int,
        attempt_id: int,
        result: DirectValidatedCandidate,
    ) -> object: ...


class DirectRunnerLike(Protocol):
    async def dispatch(
        self,
        acquisition_id: int,
        artifact_id: int,
        *,
        initial_source: HostResolutionRequest | None = None,
    ) -> bool: ...


DirectPlanner = Callable[..., Awaitable[DirectAcquisitionPlanningResult]]


@dataclass(frozen=True, slots=True)
class SearchAcquisitionRoutingResult:
    """Compact outcome consumed by search history and task accounting."""

    grabbed: int
    queued: int
    action_status: str
    best_confidence: str | None
    source_kind: Literal["indexer", "direct", "dc"] | None
    notices: tuple[str, ...] = ()
    download_id: int | None = None
    acquisition_id: int | None = None
    release_title: str | None = None


async def route_search_acquisition(
    session: AsyncSession,
    *,
    outcome: IssueSearchOutcome,
    search_log_id: int,
    eval_kwargs: SearchEvalKwargs,
    type_thresholds: dict[str, str],
    download_service: DownloadServiceLike,
    intervention_service: InterventionServiceLike,
    runner: DirectRunnerLike | None,
    source_priority: list[str] | None = None,
    planner: DirectPlanner = plan_direct_acquisition,
) -> SearchAcquisitionRoutingResult:
    """Persist all discoveries and route the best result through its own adapter."""
    target = outcome.target
    discoveries: tuple[DirectSearchDiscovery, ...] = ()
    if outcome.direct_outcome is not None:
        discoveries = await persist_direct_search_discoveries(
            session,
            target,
            outcome.direct_outcome,
            search_log_id=search_log_id,
        )

    ranked = rank_search_sources(
        outcome,
        eval_kwargs,
        source_priority=source_priority,
    )
    if not ranked:
        return SearchAcquisitionRoutingResult(0, 0, "no_match", None, None)
    notices: list[str] = []
    first_confidence = ranked[0].validation.confidence.value
    for selected in ranked:
        confidence = selected.validation.confidence.value
        auto_grab = should_auto_grab(
            selected.validation.confidence,
            target.issue_type,
            type_thresholds,
        )
        if selected.source_kind == "indexer":
            if auto_grab:
                try:
                    download = await download_service.send_to_client(
                        session,
                        selected.release,
                        target.issue_id,
                    )
                except ProviderError:
                    notices.append(_indexer_failure_notice(selected.release.indexer_name))
                    continue
                if getattr(download, "state", None) == DownloadState.FAILED:
                    notices.append(_indexer_failure_notice(selected.release.indexer_name))
                    continue
                return SearchAcquisitionRoutingResult(
                    1,
                    0,
                    "downloading",
                    confidence,
                    "indexer",
                    tuple(notices),
                    download_id=getattr(download, "id", None),
                    release_title=selected.release.title,
                )
            if not await intervention_service.has_pending_for_issue(session, target.issue_id):
                await intervention_service.create_pending_match(
                    session,
                    target.issue_id,
                    selected.release,
                    selected.validation,
                )
                return SearchAcquisitionRoutingResult(
                    0,
                    1,
                    "queued",
                    confidence,
                    "indexer",
                    tuple(notices),
                    release_title=selected.release.title,
                )
            return SearchAcquisitionRoutingResult(
                0,
                0,
                "pending_exists",
                confidence,
                "indexer",
                tuple(notices),
                release_title=selected.release.title,
            )

        if selected.source_kind == "dc":
            if selected.dc_result is None:
                raise RuntimeError("Selected DC result is unavailable.")
            # R5 evaluates automatic DC participation and records the winner,
            # while R6 owns durable provenance and queue mutation.
            return SearchAcquisitionRoutingResult(
                0,
                0,
                "dc_evaluation_only",
                confidence,
                "dc",
                tuple(notices),
                release_title=selected.release.title,
            )

        direct_result = selected.direct_result
        if direct_result is None:
            raise RuntimeError("Selected direct result is unavailable.")
        discovery = next(item for item in discoveries if item.result is direct_result)
        if not auto_grab:
            await _queue_direct_semantic_review(
                session,
                target.issue_id,
                discovery.attempt_id,
                direct_result,
                intervention_service,
            )
            return SearchAcquisitionRoutingResult(
                0,
                1,
                "intervention",
                confidence,
                "direct",
                tuple(notices),
                release_title=selected.release.title,
            )

        provider = await session.get(
            DirectProviderConfig,
            direct_result.provider.provider_config_id,
        )
        if provider is None:
            raise RuntimeError("Direct provider configuration was not found.")
        reserve_notice = _automatic_reserve_notice(provider)
        if reserve_notice is not None:
            notices.append(reserve_notice)
            await _fail_direct_attempt(
                session,
                discovery.attempt_id,
                code="automatic_quota_reserve",
                message="Automatic quota reserve reached; manual grabs remain available.",
            )
            continue

        # Make provenance restart-safe before provider resolution performs network I/O.
        await session.commit()
        try:
            planned = await planner(session, acquisition_id=discovery.attempt_id)
        except DirectAcquisitionPlanningError as exc:
            if exc.intervention:
                await intervention_service.create_direct_pending_match(
                    session,
                    target.issue_id,
                    discovery.attempt_id,
                    direct_result,
                )
                await session.commit()
                return SearchAcquisitionRoutingResult(
                    0,
                    1,
                    "intervention",
                    confidence,
                    "direct",
                    tuple(notices),
                    release_title=selected.release.title,
                )
            notices.append(_provider_failure_notice(direct_result.provider.display_name, exc))
            await session.commit()
            continue
        await session.commit()
        if runner is None:
            raise RuntimeError("Direct acquisition runner is not initialized.")
        await runner.dispatch(
            planned.attempt.id,
            planned.selected_artifact.id,
            initial_source=planned.initial_source,
        )
        return SearchAcquisitionRoutingResult(
            1,
            0,
            "downloading",
            confidence,
            "direct",
            tuple(notices),
            acquisition_id=planned.attempt.id,
            release_title=selected.release.title,
        )

    return SearchAcquisitionRoutingResult(
        0,
        0,
        "source_unavailable" if notices else "no_match",
        first_confidence,
        None,
        tuple(notices),
    )


def _indexer_failure_notice(indexer_name: str) -> str:
    """Describe an indexer queue failure without exposing provider details."""
    return f"{indexer_name} could not be queued; continuing with other sources."


async def _queue_direct_semantic_review(
    session: AsyncSession,
    issue_id: int,
    attempt_id: int,
    result: DirectValidatedCandidate | None,
    intervention_service: InterventionServiceLike,
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
    if attempt is None or result is None:
        raise RuntimeError("Selected direct result is unavailable.")
    transition_acquisition(attempt, DirectAcquisitionState.INTERVENTION)
    attempt.failure_class = DirectArtifactFailureClass.USER_ACTION
    attempt.failure_code = "semantic_review_required"
    attempt.error_message = "Review this direct result before downloading."
    advance_acquisition_progress(
        attempt,
        revision=attempt.progress_revision + 1,
        snapshot={
            "schema_version": 1,
            "stage": "intervention",
            "failure_code": "semantic_review_required",
        },
    )
    await intervention_service.create_direct_pending_match(session, issue_id, attempt_id, result)


async def _fail_direct_attempt(
    session: AsyncSession,
    attempt_id: int,
    *,
    code: str,
    message: str,
) -> None:
    attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
    if attempt is None:
        raise RuntimeError("Persisted direct acquisition attempt was not found.")
    transition_acquisition(attempt, DirectAcquisitionState.FAILED)
    attempt.failure_class = DirectArtifactFailureClass.PROVIDER_UNAVAILABLE
    attempt.failure_code = code
    attempt.error_message = message
    advance_acquisition_progress(
        attempt,
        revision=attempt.progress_revision + 1,
        snapshot={"schema_version": 1, "stage": "failed", "failure_code": code},
    )
    await session.flush()


def _automatic_reserve_notice(provider: DirectProviderConfig) -> str | None:
    if provider.state is DirectProviderState.RATE_LIMITED:
        return f"{provider.display_name} quota exhausted; continuing with other sources."
    if provider.state is DirectProviderState.AUTHENTICATION_REQUIRED:
        return (
            f"{provider.display_name} authentication needs attention; "
            "continuing with other sources."
        )
    if provider.state is DirectProviderState.UNAVAILABLE:
        return f"{provider.display_name} is temporarily unavailable; continuing with other sources."
    if not provider_supports_quota(provider):
        return None
    quota = provider_quota_status(provider)
    if automatic_quota_available(provider):
        return None
    if quota is None or quota.remaining is None:
        return f"{provider.display_name} is temporarily unavailable; continuing with other sources."
    reserve = automatic_quota_reserve(provider)
    return (
        f"{provider.display_name} automatic reserve reached; reserved slots remain available "
        f"for manual grabs."
        if quota.remaining <= reserve
        else f"{provider.display_name} is temporarily unavailable; continuing with other sources."
    )


def _provider_failure_notice(
    provider_name: str,
    error: DirectAcquisitionPlanningError,
) -> str:
    if error.code == "source_quota_limited":
        return f"{provider_name} quota exhausted; continuing with other sources."
    if error.code == "source_authentication_required":
        return f"{provider_name} authentication needs attention; continuing with other sources."
    if error.code == "candidate_not_found":
        return f"{provider_name} result is no longer available; continuing with other sources."
    return f"{provider_name} is temporarily unavailable; continuing with other sources."

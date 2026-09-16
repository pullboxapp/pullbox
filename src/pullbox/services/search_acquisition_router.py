"""Route one unified search winner to its native acquisition adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pullbox.core.exceptions import ProviderError
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactFailureClass,
    DirectArtifactState,
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
    transition_artifact,
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
    from pullbox.services.airdcpp_search_types import DcValidatedCandidate
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

    async def create_dc_pending_match(
        self,
        session: AsyncSession,
        issue_id: int,
        result: DcValidatedCandidate,
        search_log_id: int,
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
AcquisitionEligibilityCheck = Callable[[], Awaitable[bool]]


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
    eligibility_check: AcquisitionEligibilityCheck | None = None,
) -> SearchAcquisitionRoutingResult:
    """Route a winner, optionally rechecking read-only scope eligibility at each handoff."""
    if stopped := await _stop_if_ineligible(session, eligibility_check):
        return stopped
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
        if stopped := await _stop_if_ineligible(session, eligibility_check, discoveries):
            return stopped
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
                if stopped := await _stop_if_ineligible(session, eligibility_check, discoveries):
                    return stopped
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
            from pullbox.services.airdcpp_search_acquisition import (
                DcIssueAlreadyOwnedError,
                acquire_dc_candidate,
                ready_dc_client,
            )

            try:
                await ready_dc_client(session, selected.dc_result, automatic=True)
                if not auto_grab:
                    pending = None
                    if not await intervention_service.has_pending_for_issue(
                        session, target.issue_id
                    ):
                        pending = await intervention_service.create_dc_pending_match(
                            session,
                            target.issue_id,
                            selected.dc_result,
                            search_log_id,
                        )
                    return SearchAcquisitionRoutingResult(
                        0,
                        int(pending is not None),
                        "queued" if pending is not None else "pending_exists",
                        confidence,
                        "dc",
                        tuple(notices),
                        release_title=selected.release.title,
                    )
                route = selected.dc_result.route
                download, created = await acquire_dc_candidate(
                    session,
                    candidate=selected.dc_result,
                    issue_id=target.issue_id,
                    search_log_id=search_log_id,
                    request_key=f"dc-auto:{search_log_id}:{target.issue_id}:{route.client_config_id}:{route.tth}",
                    automatic=True,
                )
            except DcIssueAlreadyOwnedError:
                return SearchAcquisitionRoutingResult(0, 0, "already_owned", confidence, "dc")
            except ProviderError:
                await session.commit()
                notices.append(_indexer_failure_notice(selected.release.indexer_name))
                continue
            # An ambiguous mutation is already owned by reconciliation: do not
            # fall through and start a duplicate on another source.
            return SearchAcquisitionRoutingResult(
                int(created),
                0,
                (
                    "already_downloading"
                    if not created
                    else "retry_pending"
                    if download.state == DownloadState.RETRY_PENDING
                    else "downloading"
                ),
                confidence,
                "dc",
                tuple(notices),
                download_id=download.id,
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
        if stopped := await _stop_if_ineligible(session, eligibility_check, discoveries):
            return stopped
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
            if stopped := await _stop_if_ineligible(session, eligibility_check, discoveries):
                return stopped
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
        if stopped := await _stop_if_ineligible(session, eligibility_check, discoveries):
            return stopped
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


async def _stop_if_ineligible(
    session: AsyncSession,
    eligibility_check: AcquisitionEligibilityCheck | None,
    discoveries: tuple[DirectSearchDiscovery, ...] = (),
) -> SearchAcquisitionRoutingResult | None:
    """Re-query current scope without carrying its read snapshot across remote work."""
    if eligibility_check is None:
        return None
    # Resolution may have retained a read snapshot or produced durable planning
    # state. Preserve that state before the caller checks current eligibility.
    await session.commit()
    eligible = await eligibility_check()
    await session.commit()
    if eligible is True:
        return None
    if discoveries:
        attempts = await session.scalars(
            select(DirectAcquisitionAttempt)
            .where(DirectAcquisitionAttempt.id.in_(item.attempt_id for item in discoveries))
            .options(selectinload(DirectAcquisitionAttempt.artifact_attempts))
            .execution_options(populate_existing=True)
        )
        for attempt in attempts:
            _cancel_unsubmitted_attempt(attempt)
        await session.commit()
    return SearchAcquisitionRoutingResult(0, 0, "no_longer_eligible", None, None)


def _cancel_unsubmitted_attempt(attempt: DirectAcquisitionAttempt) -> None:
    """Cancel only this router's unsubmitted plans, never rewrite terminal history."""
    if attempt.state not in {
        DirectAcquisitionState.DISCOVERED,
        DirectAcquisitionState.PLANNED,
        DirectAcquisitionState.INTERVENTION,
    }:
        return
    transition_acquisition(attempt, DirectAcquisitionState.CANCELLED)
    attempt.failure_class = DirectArtifactFailureClass.USER_ACTION
    attempt.failure_code = "no_longer_eligible"
    attempt.error_message = "Search scope changed before acquisition could be submitted."
    attempt.next_retry_at = None
    for artifact in attempt.artifact_attempts:
        if artifact.state not in {DirectArtifactState.PLANNED, DirectArtifactState.INTERVENTION}:
            continue
        transition_artifact(artifact, DirectArtifactState.CANCELLED)
        artifact.failure_class = DirectArtifactFailureClass.USER_ACTION
        artifact.failure_code = attempt.failure_code
        artifact.error_message = attempt.error_message
        artifact.next_retry_at = None
    advance_acquisition_progress(
        attempt,
        revision=attempt.progress_revision + 1,
        snapshot={"schema_version": 1, "stage": "cancelled", "failure_code": attempt.failure_code},
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

"""Issue API routes — detail, status updates, search, download, and file import."""

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import inspect, select
from sqlalchemy.orm import joinedload, selectinload

from pullbox.api.deps import AuthenticatedUser, DbSession
from pullbox.composition.airdcpp import (
    get_airdcpp_supervisor_registry,
    load_airdcpp_search_clients,
)
from pullbox.config import get_settings
from pullbox.core.exceptions import ConfigurationError, NotFoundError, ValidationError
from pullbox.core.file_ops import register_library_file
from pullbox.core.file_safety import classify_resource_safety_exception
from pullbox.core.library_root_resolution import preferred_managed_root_id
from pullbox.models.client import DownloadClientConfig
from pullbox.models.direct_acquisition import DirectAcquisitionAttempt
from pullbox.models.download import DownloadClientType, DownloadState
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import LibraryFile, MatchConfidence
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.providers.airdcpp.supervisor import AirDcppSupervisorState
from pullbox.schemas.issue import (
    IssueFileDeleteResponse,
    IssueResponse,
    IssueUpdate,
    ManualFileImportProgressResponse,
    ManualFileImportRequest,
    ManualFileImportResponse,
)
from pullbox.schemas.search import (
    DcGrabRequest,
    DcGrabResponse,
    DirectGrabRequest,
    DirectGrabResponse,
    GrabReleaseRequest,
    GrabReleaseResponse,
    InteractiveSearchIssue,
    InteractiveSearchResponse,
    MatchDetails,
    RejectedResultItem,
    SearchResultItem,
)
from pullbox.services.airdcpp_acquisition import AirDcppQueueAcquisitionService
from pullbox.services.airdcpp_automatic_search import attach_automatic_airdcpp_search
from pullbox.services.airdcpp_route_tokens import get_airdcpp_route_token_store
from pullbox.services.direct_acquisition_planner_service import (
    DirectAcquisitionPlanningError,
    plan_direct_acquisition,
    plan_direct_acquisition_with_provider_fallback,
)
from pullbox.services.direct_search_coordinator import (
    DirectSearchDiscovery,
    persist_direct_search_discoveries,
)
from pullbox.services.issue_file_service import (
    delete_issue_library_file,
    resolve_configured_utility_trash_dir,
)
from pullbox.services.issue_import_service import (
    ManualIssueImportError,
    prepare_manual_issue_import,
)
from pullbox.services.issue_service import IssueService
from pullbox.services.release_validator import (
    ValidationResult,
)
from pullbox.services.search_acquisition_router import route_search_acquisition
from pullbox.services.search_scoring import (
    DIRECT_PROVIDER_NEUTRAL_PRIORITY,
    match_confidence_rank,
    normalize_source_priority,
)
from pullbox.services.search_service import (
    DEFAULT_TYPE_THRESHOLDS,
    IssueSearchOutcome,
    IssueSearchTarget,
    SearchRuntime,
    SearchService,
    build_search_runtime,
    load_issue_search_target,
    score_release,
    should_auto_grab,
    summarize_search_pass,
)
from pullbox.services.search_source_selection import select_search_source
from pullbox.services.search_types import SearchEvalKwargs
from pullbox.services.secondary_operation_progress import (
    project_issue_import_operation_progress,
)
from pullbox.tasks.direct_acquisition_task import get_direct_acquisition_runner
from pullbox.tasks.issue_import_task import (
    cancel_issue_import_run,
    get_issue_import_progress_state,
    start_issue_import_run,
)

logger = structlog.get_logger(__name__)


router = APIRouter(prefix="/issues", tags=["issues"])


# ── Helpers ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _IssueSearchBundle:
    """Shared search execution payload for issue-scoped search routes."""

    target: IssueSearchTarget
    issue: InteractiveSearchIssue
    runtime: SearchRuntime | None
    outcome: IssueSearchOutcome | None
    matched_items: list["SearchResultItem"]
    rejected_items: list["RejectedResultItem"]
    search_time_ms: int


async def _increment_search_log_grabbed(
    session: DbSession,
    *,
    issue_id: int,
    search_log_id: int | None,
) -> None:
    """Increment the grabbed counter for the originating manual search row."""
    if search_log_id is None:
        return

    search_log = await session.get(SearchLog, search_log_id)
    if search_log is None:
        logger.warning(
            "issue_manual_grab_search_log_missing",
            issue_id=issue_id,
            search_log_id=search_log_id,
        )
        return

    if search_log.issue_id != issue_id:
        logger.warning(
            "issue_manual_grab_search_log_issue_mismatch",
            issue_id=issue_id,
            search_log_id=search_log_id,
            search_log_issue_id=search_log.issue_id,
        )
        return

    search_log.results_grabbed = int(search_log.results_grabbed or 0) + 1


def _enrich_issue(issue: Issue) -> dict[str, object]:
    """Add computed fields (series_title, has_file) to an issue."""
    mapper = inspect(type(issue))
    data: dict[str, object] = {c.key: getattr(issue, c.key) for c in mapper.columns}
    data["issue_number_text"] = issue.effective_issue_number_text
    data["series_title"] = issue.series.title if issue.series else None
    data["has_file"] = issue.library_file is not None
    return data


async def _load_issue_response(session: DbSession, issue_id: int) -> IssueResponse:
    """Load an issue with relationships and return a validated response."""
    result = await session.execute(
        select(Issue)
        .options(joinedload(Issue.series), joinedload(Issue.library_file))
        .where(Issue.id == issue_id)
    )
    issue = result.unique().scalar_one_or_none()
    if issue is None:
        raise NotFoundError("Issue", issue_id)
    return IssueResponse.model_validate(_enrich_issue(issue))


_CONFIDENCE_BONUS = {
    MatchConfidence.HIGH: 100,
    MatchConfidence.MEDIUM: 70,
    MatchConfidence.LOW: 40,
}


def build_interactive_results(
    matched_vr: list[ValidationResult],
    rejected_vr: list[ValidationResult],
    eval_kwargs: SearchEvalKwargs,
    source_priority: list[str] | None = None,
    *,
    issue_type: IssueType = IssueType.ISSUE,
    type_thresholds: dict[str, str] | None = None,
    scoring_priority: int | None = None,
) -> tuple[list[SearchResultItem], list[RejectedResultItem]]:
    """Build schema objects from validation results with quality scoring.

    Extracts the scoring and schema-construction logic so it can be
    tested independently of the async endpoint / ASGI transport.

    Returns:
        A tuple of (matched_items, rejected_items) ready for the response.
    """
    min_score = eval_kwargs.get("min_score", 10.0)
    confidence_blend = eval_kwargs.get("confidence_blend", 0.40)
    quality_weight = 1.0 - confidence_blend
    score_weights = eval_kwargs.get("score_weights")
    min_size_mb = eval_kwargs.get("min_size_mb")
    max_size_mb = eval_kwargs.get("max_size_mb")
    preferred_format = eval_kwargs.get("preferred_format")
    seeder_tiers = eval_kwargs.get("seeder_tiers")

    matched_items: list[SearchResultItem] = []
    for vr in matched_vr:
        quality = score_release(
            vr.release,
            scoring_priority,
            min_size_mb=int(str(min_size_mb)) if min_size_mb else 50,
            max_size_mb=int(str(max_size_mb)) if max_size_mb else 2000,
            preferred_format=str(preferred_format) if preferred_format else None,
            seeder_tiers=seeder_tiers,
            score_weights=score_weights,
        )
        bonus = _CONFIDENCE_BONUS.get(vr.confidence, 0)
        final_score = (quality * quality_weight) + (bonus * confidence_blend)

        matched_items.append(
            SearchResultItem(
                title=vr.release.title,
                indexer_name=vr.release.indexer_name,
                indexer_id=vr.release.indexer_id,
                download_url=vr.release.download_url,
                info_url=vr.release.info_url,
                size_bytes=vr.release.size_bytes,
                age_days=vr.release.age_days,
                seeders=vr.release.seeders,
                leechers=vr.release.leechers,
                is_torrent=vr.release.is_torrent,
                category=vr.release.category,
                confidence=str(vr.confidence.value),
                quality_score=round(final_score, 1),
                auto_grabbable=final_score >= min_score
                and should_auto_grab(
                    vr.confidence,
                    issue_type,
                    DEFAULT_TYPE_THRESHOLDS if type_thresholds is None else type_thresholds,
                ),
                match_details=MatchDetails(
                    parsed_series=vr.parsed.series_name,
                    parsed_issue=vr.parsed.issue_number,
                    parsed_year=vr.parsed.year,
                    series_similarity=round(vr.series_similarity, 3),
                    match_type=vr.match_type,
                ),
                method="Torrent" if vr.release.is_torrent else "Usenet",
                ranking_priority=vr.release.ranking_priority,
            )
        )

    rejected_items: list[RejectedResultItem] = []
    for vr in rejected_vr:
        rejected_items.append(
            RejectedResultItem(
                title=vr.release.title,
                indexer_name=vr.release.indexer_name,
                indexer_id=vr.release.indexer_id,
                download_url=vr.release.download_url,
                info_url=vr.release.info_url,
                size_bytes=vr.release.size_bytes,
                age_days=vr.release.age_days,
                seeders=vr.release.seeders,
                leechers=vr.release.leechers,
                is_torrent=vr.release.is_torrent,
                category=vr.release.category,
                rejection_reason=vr.rejection_reason or "unknown",
                confidence=str(vr.confidence.value) if vr.is_match else None,
                method="Torrent" if vr.release.is_torrent else "Usenet",
                ranking_priority=vr.release.ranking_priority,
            )
        )

    matched_items = sort_interactive_results_by_source_priority(
        matched_items,
        source_priority,
    )

    return matched_items, rejected_items


def build_direct_interactive_results(
    discoveries: Sequence[DirectSearchDiscovery],
    *,
    eval_kwargs: SearchEvalKwargs,
    issue_type: IssueType,
    type_thresholds: dict[str, str] | None = None,
) -> tuple[list[SearchResultItem], list[RejectedResultItem]]:
    """Build direct rows around server identities without exposing artifact URLs."""
    matched_items: list[SearchResultItem] = []
    rejected_items: list[RejectedResultItem] = []
    for discovery in discoveries:
        if not discovery.visible:
            continue
        result = discovery.result
        candidate = result.candidate
        common = {
            "download_url": None,
            "source_kind": "direct",
            "method": "Direct",
            "direct_attempt_id": discovery.attempt_id,
            "coverage": list(candidate.parsed.issue_numbers),
            "format": candidate.parsed.format,
            "quality": candidate.parsed.quality,
            "preferred_route": "Automatic",
        }
        if result.validation.is_match:
            direct_matched, _ = build_interactive_results(
                [result.validation],
                [],
                eval_kwargs,
                issue_type=issue_type,
                type_thresholds=type_thresholds,
                scoring_priority=DIRECT_PROVIDER_NEUTRAL_PRIORITY,
            )
            matched_items.append(direct_matched[0].model_copy(update=common))
        else:
            _, direct_rejected = build_interactive_results(
                [],
                [result.validation],
                eval_kwargs,
                issue_type=issue_type,
                type_thresholds=type_thresholds,
                scoring_priority=DIRECT_PROVIDER_NEUTRAL_PRIORITY,
            )
            rejected_items.append(direct_rejected[0].model_copy(update=common))
    return matched_items, rejected_items


def sort_interactive_results_by_source_priority[
    InteractiveResultT: (SearchResultItem, RejectedResultItem),
](
    items: Sequence[InteractiveResultT],
    source_priority: list[str] | None,
) -> list[InteractiveResultT]:
    """Stable-sort combined indexer and direct rows by protocol preference."""
    normalized = normalize_source_priority(source_priority)
    priority_map = (
        {source: index for index, source in enumerate(normalized)} if normalized is not None else {}
    )

    def _source(item: SearchResultItem | RejectedResultItem) -> str:
        if item.source_kind == "direct":
            return "direct"
        return "torrent" if item.is_torrent else "usenet"

    def _rank(
        item: SearchResultItem | RejectedResultItem,
    ) -> tuple[int, int, float, float, int]:
        score = getattr(item, "quality_score", None)
        source_rank = priority_map.get(_source(item), 0)
        if item.source_kind != "direct" or normalized is None:
            return source_rank, 0, 0.0, -float(score) if score is not None else 0.0, 0
        similarity = (
            item.match_details.series_similarity if isinstance(item, SearchResultItem) else 0.0
        )
        return (
            source_rank,
            match_confidence_rank(item.confidence),
            -similarity,
            -float(score) if score is not None else 0.0,
            item.ranking_priority,
        )

    return sorted(items, key=_rank)


def _build_issue_context(target: IssueSearchTarget) -> InteractiveSearchIssue:
    """Build the public issue context returned by interactive search responses."""

    return InteractiveSearchIssue(
        id=target.issue_id,
        series_title=target.series_title,
        issue_number=target.issue_number,
        issue_type=target.issue_type.value,
        year=target.series_year,
    )


def _build_manual_file_import_response(
    *,
    issue_id: int,
    library_file: LibraryFile,
) -> ManualFileImportResponse:
    """Build the API response for a completed manual file import."""
    lf = library_file
    return ManualFileImportResponse(
        issue_id=issue_id,
        library_file_id=lf.id,
        file_name=lf.file_name,
        file_path=lf.file_path,
        file_size=lf.file_size,
        file_format=str(lf.file_format.value)
        if hasattr(lf.file_format, "value")
        else str(lf.file_format),
        match_confidence=str(lf.match_confidence.value)
        if isinstance(lf.match_confidence, MatchConfidence)
        else str(lf.match_confidence),
    )


async def _run_issue_search(
    session: DbSession,
    issue_id: int,
    *,
    include_download_clients: bool,
) -> _IssueSearchBundle:
    """Run the shared search pipeline for an issue and shape UI/API payloads."""

    started_at = time.monotonic()
    target = await load_issue_search_target(session, issue_id)
    if target is None:
        raise NotFoundError("Issue", issue_id)

    issue_ctx = _build_issue_context(target)
    has_automatic_dc = False
    dc_registry = get_airdcpp_supervisor_registry() if include_download_clients else None
    if dc_registry is not None:
        has_automatic_dc = bool(
            await load_airdcpp_search_clients(session, dc_registry, automatic=True)
        )
    runtime = await build_search_runtime(
        session,
        include_download_clients=include_download_clients,
        include_direct_providers=True,
        allow_empty_registry=has_automatic_dc,
    )
    # Release the read transaction before slow indexer/network work. Search log
    # persistence happens later in a short write transaction owned by the caller.
    await session.commit()
    if runtime is None:
        return _IssueSearchBundle(
            target=target,
            issue=issue_ctx,
            runtime=None,
            outcome=None,
            matched_items=[],
            rejected_items=[],
            search_time_ms=int((time.monotonic() - started_at) * 1000),
        )

    search_svc = SearchService(
        registry=runtime.registry,
        failure_threshold=runtime.failure_threshold,
        ignore_indexer_backoff=True,
        direct_providers=runtime.direct_providers,
    )
    outcome = await search_svc.search_issue_target(
        session,
        target,
        mode="fast",
        indexer_configs=runtime.indexer_configs,
        eval_kwargs=runtime.eval_kwargs,
        validator_kwargs=runtime.validator_kwargs,
        source_priority=runtime.source_priority,
    )
    # Blocklist filtering/config reads can open a transaction after the indexer
    # call. Release it before any deep fallback work or UI response building.
    await session.commit()
    has_direct_match = bool(outcome.direct_outcome and outcome.direct_outcome.matched)
    if outcome.matched or has_direct_match or not runtime.two_pass_enabled:
        outcome.search_details["search_strategy"] = (
            "quick_first" if outcome.matched else "quick_first_single_pass"
        )
    else:
        fast_summary = summarize_search_pass(outcome)
        outcome = await search_svc.search_issue_target(
            session,
            target,
            mode="deep",
            indexer_configs=runtime.indexer_configs,
            eval_kwargs=runtime.eval_kwargs,
            validator_kwargs=runtime.validator_kwargs,
            source_priority=runtime.source_priority,
            auto_fallback=True,
        )
        # Deep fallback performs the same DB-backed filtering after network IO.
        # The caller will persist search history separately.
        await session.commit()
        outcome.search_details["search_strategy"] = "quick_first_deep_fallback"
        outcome.search_details["fast_search"] = fast_summary
    outcome.search_details["manual_search_strategy"] = outcome.search_details.get("search_strategy")
    matched_items, rejected_items = build_interactive_results(
        outcome.matched,
        outcome.rejected,
        runtime.eval_kwargs,
        source_priority=runtime.source_priority,
        issue_type=target.issue_type,
        type_thresholds=runtime.type_thresholds,
    )
    return _IssueSearchBundle(
        target=target,
        issue=issue_ctx,
        runtime=runtime,
        outcome=outcome,
        matched_items=matched_items,
        rejected_items=rejected_items,
        search_time_ms=int((time.monotonic() - started_at) * 1000),
    )


async def _persist_direct_bundle_results(
    session: DbSession,
    bundle: _IssueSearchBundle,
    *,
    search_log_id: int,
) -> None:
    """Persist direct discoveries and append URL-free rows to one bundle."""
    if bundle.outcome is None or bundle.outcome.direct_outcome is None:
        return
    discoveries = await persist_direct_search_discoveries(
        session,
        bundle.target,
        bundle.outcome.direct_outcome,
        search_log_id=search_log_id,
    )
    direct_matched, direct_rejected = build_direct_interactive_results(
        discoveries,
        eval_kwargs=bundle.runtime.eval_kwargs if bundle.runtime else {},
        issue_type=bundle.target.issue_type,
        type_thresholds=bundle.runtime.type_thresholds if bundle.runtime else None,
    )
    bundle.matched_items.extend(direct_matched)
    bundle.rejected_items.extend(direct_rejected)
    source_priority = bundle.runtime.source_priority if bundle.runtime else None
    bundle.matched_items[:] = sort_interactive_results_by_source_priority(
        bundle.matched_items,
        source_priority,
    )
    bundle.rejected_items[:] = sort_interactive_results_by_source_priority(
        bundle.rejected_items,
        source_priority,
    )


def _build_issue_search_log(
    bundle: _IssueSearchBundle,
    *,
    search_type: SearchType = SearchType.MANUAL,
    results_grabbed: int = 0,
    results_queued: int = 0,
    results_rejected: int | None = None,
    action_status: str | None = None,
    run_state: str = "completed",
) -> SearchLog:
    """Create a search history row from a shared search bundle."""

    outcome = bundle.outcome
    if outcome is None:
        details: dict[str, object] = {"results_count": 0, "validated_count": 0}
        results_found = 0
        best_confidence = None
    else:
        details = dict(outcome.search_details)
        direct_outcome = outcome.direct_outcome
        direct_matched = len(direct_outcome.matched) if direct_outcome else 0
        direct_rejected = len(direct_outcome.rejected) if direct_outcome else 0
        dc_outcome = outcome.dc_outcome
        dc_matched = len(dc_outcome.matched) if dc_outcome else 0
        details["validated_count"] = len(outcome.matched) + direct_matched + dc_matched
        details["direct_results_count"] = direct_matched + direct_rejected
        details["direct_providers_searched"] = (
            direct_outcome.providers_searched if direct_outcome else 0
        )
        if direct_outcome and direct_outcome.failures:
            details["direct_provider_failures"] = [
                {
                    "provider": failure.provider_identity,
                    "code": failure.code,
                    "retryable": failure.retryable,
                }
                for failure in direct_outcome.failures
            ]
        results_found = outcome.results_found_count
        best_confidence = (
            outcome.best_validation.confidence.value
            if outcome.best_validation is not None
            else (
                direct_outcome.matched[0].validation.confidence.value
                if direct_outcome and direct_outcome.matched
                else None
            )
        )

    details["run_state"] = run_state
    details["search_time_ms"] = bundle.search_time_ms
    if action_status:
        details["action_status"] = action_status
    if results_rejected is None:
        results_rejected = outcome.results_rejected_count if outcome else 0

    return SearchLog(
        issue_id=bundle.target.issue_id,
        series_title=bundle.target.series_title,
        issue_number=bundle.target.issue_number,
        search_type=search_type,
        results_found=results_found,
        results_grabbed=results_grabbed,
        results_queued=results_queued,
        results_rejected=results_rejected,
        details=details,
        best_confidence=best_confidence,
    )


# ── Detail ────────────────────────────────────────────────────────────


@router.get("/{issue_id}", response_model=IssueResponse)
async def get_issue(
    issue_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> IssueResponse:
    """Get full issue detail by ID."""
    return await _load_issue_response(session, issue_id)


# ── Update ────────────────────────────────────────────────────────────


@router.put("/{issue_id}", response_model=IssueResponse)
async def update_issue(
    issue_id: int,
    body: IssueUpdate,
    _user: AuthenticatedUser,
    session: DbSession,
) -> IssueResponse:
    """Update an issue's status or metadata."""
    if body.status is not None:
        await IssueService.mark_status(session, issue_id, body.status)
    return await _load_issue_response(session, issue_id)


# ── Actions ───────────────────────────────────────────────────────────


@router.post("/{issue_id}/search", status_code=200)
async def search_issue(
    issue_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, object]:
    """Trigger a manual search for a specific issue.

    Returns validated results with confidence scoring. Uses the same
    validation pipeline as the interactive search endpoint.
    """
    bundle = await _run_issue_search(
        session,
        issue_id,
        include_download_clients=False,
    )
    if bundle.runtime is None:
        return {"issue_id": issue_id, "results": [], "error": "no indexers configured"}

    search_log = _build_issue_search_log(bundle)
    session.add(search_log)
    await session.flush()
    await _persist_direct_bundle_results(session, bundle, search_log_id=search_log.id)
    await session.commit()

    logger.info("issue_manual_search", issue_id=issue_id, results=len(bundle.matched_items))
    return {
        "issue_id": issue_id,
        "results": [
            {
                "title": item.title,
                "indexer_name": item.indexer_name,
                "size_bytes": item.size_bytes,
                "age_days": item.age_days,
                "is_torrent": item.is_torrent,
                "confidence": item.confidence,
            }
            for item in bundle.matched_items
        ],
    }


@router.get("/{issue_id}/search-results", response_model=InteractiveSearchResponse)
async def get_search_results(
    issue_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> InteractiveSearchResponse:
    """Return validated search results for interactive display.

    Searches all configured indexers for the issue and returns both matched
    and rejected results with full context (confidence, match details,
    rejection reasons) so the UI can present an interactive search results page.
    """
    bundle = await _run_issue_search(
        session,
        issue_id,
        include_download_clients=False,
    )
    if bundle.runtime is None:
        return InteractiveSearchResponse(
            issue=bundle.issue,
            matched=[],
            rejected=[],
            search_time_ms=bundle.search_time_ms,
            search_log_id=None,
        )

    search_log = _build_issue_search_log(bundle)
    session.add(search_log)
    await session.flush()
    await _persist_direct_bundle_results(session, bundle, search_log_id=search_log.id)
    await session.commit()

    logger.info(
        "issue_interactive_search",
        issue_id=issue_id,
        matched=len(bundle.matched_items),
        rejected=len(bundle.rejected_items),
        search_time_ms=bundle.search_time_ms,
    )

    return InteractiveSearchResponse(
        issue=bundle.issue,
        matched=bundle.matched_items,
        rejected=bundle.rejected_items,
        search_time_ms=bundle.search_time_ms,
        search_log_id=search_log.id,
    )


@router.post("/{issue_id}/grab", status_code=201)
async def grab_release(
    issue_id: int,
    body: GrabReleaseRequest,
    _user: AuthenticatedUser,
    session: DbSession,
) -> GrabReleaseResponse:
    """Grab a specific release for an issue.

    Bypasses automated matching — the user has already selected the release.
    Constructs a ReleaseResult and sends it directly to the download client.
    """
    from pullbox.composition.services import build_domain_download_service

    issue_result = await session.execute(
        select(Issue).options(joinedload(Issue.library_file)).where(Issue.id == issue_id)
    )
    issue = issue_result.unique().scalar_one_or_none()
    if not issue:
        raise NotFoundError("Issue", issue_id)
    replace_existing_file = issue.library_file is not None

    built = await build_domain_download_service(session)
    if built is None:
        from pullbox.core.exceptions import ProviderError

        raise ProviderError("download", "No download clients configured")

    download_svc, indexer_configs = built
    if body.indexer_id is not None and body.indexer_id not in indexer_configs:
        raise ValidationError(
            "The originating indexer is no longer available. Run the search again."
        )

    download = await download_svc.grab_release(
        session,
        issue_id=issue_id,
        download_url=body.download_url,
        title=body.title,
        indexer_name=body.indexer_name,
        indexer_id=body.indexer_id,
        is_torrent=body.is_torrent,
        file_size=body.file_size,
        replace_existing_file=replace_existing_file,
    )
    if download.state == DownloadState.FAILED:
        detail = download.error_message or "Download client rejected the release."
        await session.commit()
        logger.warning(
            "issue_manual_grab_failed",
            issue_id=issue_id,
            download_id=download.id,
            title=body.title,
            is_torrent=body.is_torrent,
            error=detail,
            manual=True,
        )
        raise HTTPException(status_code=502, detail=detail)

    await _increment_search_log_grabbed(
        session,
        issue_id=issue_id,
        search_log_id=body.search_log_id,
    )

    logger.info(
        "issue_manual_grab",
        issue_id=issue_id,
        download_id=download.id,
        title=body.title,
        is_torrent=body.is_torrent,
        manual=True,
    )

    return GrabReleaseResponse(
        issue_id=issue_id,
        download_id=download.id,
        title=body.title,
        status=str(download.state.value),
    )


@router.post(
    "/{issue_id}/direct-grab",
    status_code=201,
    response_model=DirectGrabResponse,
)
async def grab_direct_release(
    issue_id: int,
    body: DirectGrabRequest,
    _user: AuthenticatedUser,
    session: DbSession,
) -> DirectGrabResponse:
    """Plan and queue one URL-free direct discovery selected by the user."""
    attempt = await session.get(DirectAcquisitionAttempt, body.direct_attempt_id)
    if attempt is None or attempt.issue_id != issue_id:
        raise NotFoundError("Direct acquisition", body.direct_attempt_id)

    issue_result = await session.execute(
        select(Issue).options(joinedload(Issue.library_file)).where(Issue.id == issue_id)
    )
    issue = issue_result.unique().scalar_one_or_none()
    if issue is None:
        raise NotFoundError("Issue", issue_id)
    attempt.replace_existing_file = issue.library_file is not None

    try:
        planned = await plan_direct_acquisition_with_provider_fallback(
            session,
            acquisition_id=attempt.id,
            pinned_route_identity=body.pinned_route_identity,
            planner=plan_direct_acquisition,
        )
    except DirectAcquisitionPlanningError as exc:
        await session.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await _increment_search_log_grabbed(
        session,
        issue_id=issue_id,
        search_log_id=planned.attempt.search_log_id,
    )
    await session.commit()

    runner = get_direct_acquisition_runner()
    await runner.dispatch(
        planned.attempt.id,
        planned.selected_artifact.id,
        initial_source=planned.initial_source,
    )
    title = str(planned.attempt.candidate_snapshot.get("display_title") or "Direct download")
    logger.info(
        "issue_manual_direct_grab",
        issue_id=issue_id,
        acquisition_id=planned.attempt.id,
        artifact_id=planned.selected_artifact.id,
        provider_id=planned.attempt.provider_identity,
    )
    return DirectGrabResponse(
        issue_id=issue_id,
        acquisition_id=planned.attempt.id,
        artifact_id=planned.selected_artifact.id,
        title=title,
        status="queued",
    )


@router.post(
    "/{issue_id}/dc-grab",
    status_code=201,
    response_model=DcGrabResponse,
)
async def grab_direct_connect_release(
    issue_id: int,
    body: DcGrabRequest,
    user: AuthenticatedUser,
    session: DbSession,
) -> DcGrabResponse:
    """Persist and queue one opaque, user-bound Direct Connect result."""
    if not get_settings().airdcpp_enabled:
        raise NotFoundError("Direct Connect route", issue_id)
    try:
        grant = get_airdcpp_route_token_store().resolve(
            body.dc_route_token,
            issue_id=issue_id,
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    route = grant.candidate.route
    client = (
        await session.execute(
            select(DownloadClientConfig)
            .options(selectinload(DownloadClientConfig.airdcpp_settings))
            .where(
                DownloadClientConfig.id == route.client_config_id,
                DownloadClientConfig.client_type == DownloadClientType.AIRDCPP,
                DownloadClientConfig.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if (
        client is None
        or client.airdcpp_settings is None
        or not client.airdcpp_settings.search_enabled
    ):
        raise HTTPException(
            status_code=409,
            detail="The selected AirDC++ client is no longer enabled.",
        )
    registry = get_airdcpp_supervisor_registry()
    supervisor = registry.get(client.id) if registry is not None else None
    if supervisor is None or supervisor.state is not AirDcppSupervisorState.READY:
        raise HTTPException(
            status_code=409,
            detail="The selected AirDC++ client is not ready.",
        )
    issue_result = await session.execute(
        select(Issue).options(joinedload(Issue.library_file)).where(Issue.id == issue_id)
    )
    issue = issue_result.unique().scalar_one_or_none()
    if issue is None:
        raise NotFoundError("Issue", issue_id)
    result = await AirDcppQueueAcquisitionService().acquire(
        session,
        candidate=grant.candidate,
        issue_id=issue_id,
        request_key=grant.request_key,
        search_log_id=grant.search_log_id,
        api_client=supervisor.api_client,
        queue_priority=client.airdcpp_settings.queue_priority,
        replace_existing_file=issue.library_file is not None,
    )
    await _increment_search_log_grabbed(
        session,
        issue_id=issue_id,
        search_log_id=grant.search_log_id,
    )
    await session.commit()
    logger.info(
        "issue_manual_airdcpp_grab",
        issue_id=issue_id,
        acquisition_id=result.acquisition_id,
        download_id=result.download_history_id,
        bundle_id=result.bundle_id,
        client_config_id=route.client_config_id,
    )
    return DcGrabResponse(
        issue_id=issue_id,
        acquisition_id=result.acquisition_id,
        download_id=result.download_history_id,
        bundle_id=result.bundle_id,
        title=grant.candidate.release.title,
        status=result.state.value,
    )


@router.post("/{issue_id}/download", status_code=200)
async def download_issue(
    issue_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> dict[str, object]:
    """Search for and download the best available release for an issue.

    Respects per-type confidence thresholds: if the best match does not
    meet the auto-grab threshold for its issue type, it is queued to the
    intervention queue instead of being sent directly to the client.
    """
    from pullbox.composition.services import build_download_service
    from pullbox.services.intervention_service import InterventionService

    # Verify issue exists
    issue = await session.get(Issue, issue_id)
    if not issue:
        raise NotFoundError("Issue", issue_id)

    issue_was_skipped = issue.status == IssueStatus.SKIPPED

    bundle = await _run_issue_search(
        session,
        issue_id,
        include_download_clients=True,
    )
    if bundle.runtime is not None and bundle.outcome is not None:
        started_at = time.monotonic()
        outcome = await attach_automatic_airdcpp_search(
            session, bundle.outcome, validator_kwargs=bundle.runtime.validator_kwargs
        )
        bundle = replace(
            bundle,
            outcome=outcome,
            search_time_ms=bundle.search_time_ms + int((time.monotonic() - started_at) * 1000),
        )
    # Searching a skipped issue implicitly marks it as wanted. Do this after
    # the search setup transaction has been released so slow indexer calls do
    # not hold a write transaction open.
    if issue_was_skipped:
        issue.status = IssueStatus.WANTED
    if bundle.runtime is None:
        session.add(
            _build_issue_search_log(
                bundle,
                search_type=SearchType.AUTOMATED,
                action_status="no_clients",
                results_rejected=0,
            )
        )
        await session.commit()
        return {"issue_id": issue_id, "status": "no_clients", "error": "no indexers configured"}

    download_svc = build_download_service(bundle.runtime.registry)

    if bundle.outcome is None:
        session.add(
            _build_issue_search_log(
                bundle,
                search_type=SearchType.AUTOMATED,
                action_status="no_results",
                results_rejected=0,
            )
        )
        await session.commit()
        logger.info("issue_download_no_results", issue_id=issue_id)
        return {"issue_id": issue_id, "status": "no_results"}

    intervention_svc = InterventionService(download_service=download_svc)
    selection = select_search_source(
        bundle.outcome,
        bundle.runtime.eval_kwargs,
        source_priority=bundle.runtime.source_priority,
    )
    if selection is None:
        session.add(
            _build_issue_search_log(
                bundle,
                search_type=SearchType.AUTOMATED,
                action_status="no_results",
            )
        )
        await session.commit()
        logger.info("issue_download_no_results", issue_id=issue_id)
        return {"issue_id": issue_id, "status": "no_results"}

    search_log = _build_issue_search_log(bundle, search_type=SearchType.AUTOMATED)
    session.add(search_log)
    await session.flush()
    routed = await route_search_acquisition(
        session,
        outcome=bundle.outcome,
        search_log_id=search_log.id,
        eval_kwargs=bundle.runtime.eval_kwargs,
        type_thresholds=bundle.runtime.type_thresholds,
        download_service=download_svc,
        intervention_service=intervention_svc,
        runner=get_direct_acquisition_runner() if bundle.runtime.direct_providers else None,
        source_priority=bundle.runtime.source_priority,
    )

    search_log.results_grabbed = routed.grabbed
    search_log.results_queued = routed.queued
    search_log.results_rejected = bundle.outcome.results_rejected_count
    search_log.best_confidence = routed.best_confidence
    details = dict(search_log.details or {})
    details["action_status"] = routed.action_status
    if routed.notices:
        details["notices"] = list(routed.notices)
    search_log.details = details
    await session.commit()

    if routed.grabbed:
        payload: dict[str, object] = {
            "issue_id": issue_id,
            "status": "retry_pending" if routed.action_status == "retry_pending" else "downloading",
            "release_title": routed.release_title or selection.release.title,
            "source_kind": routed.source_kind,
        }
        if routed.download_id is not None:
            payload["download_id"] = routed.download_id
        if routed.acquisition_id is not None:
            payload["acquisition_id"] = routed.acquisition_id
        if routed.action_status == "retry_pending":
            payload["message"] = (
                "AirDC++ queue confirmation is pending; Pullbox will reconcile it automatically."
            )
        return payload

    if routed.queued or routed.action_status == "pending_exists":
        return {
            "issue_id": issue_id,
            "status": "queued",
            "release_title": routed.release_title or selection.release.title,
            "confidence": routed.best_confidence or selection.validation.confidence.value,
            "source_kind": routed.source_kind,
            **(
                {"message": "Already queued for review"}
                if routed.action_status == "pending_exists"
                else {}
            ),
        }

    messages = {
        "source_unavailable": "Matches found, but downloads could not be queued.",
        "already_downloading": "This issue already has an active download.",
        "already_owned": (
            "This issue already has a library file; use Find Alternative to replace it."
        ),
    }
    return {
        "issue_id": issue_id,
        "status": routed.action_status,
        "message": messages.get(routed.action_status, "No eligible match was queued."),
        "notices": list(routed.notices),
    }


@router.post(
    "/{issue_id}/import-file",
    status_code=201,
    response_model=ManualFileImportResponse,
)
async def import_file_for_issue(
    issue_id: int,
    body: ManualFileImportRequest,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ManualFileImportResponse:
    """Manually import a local file for a specific issue.

    Validates the file exists and has a supported comic format, then
    delegates to ``register_library_file()`` for move/rename/registration.
    """
    try:
        prepared = await prepare_manual_issue_import(
            session,
            issue_id=issue_id,
            file_path=body.file_path,
            move_to_library=body.move_to_library,
        )
        existing_library_file = getattr(prepared.issue, "__dict__", {}).get("library_file")
        library_file = await register_library_file(
            session,
            source_path=prepared.source_path,
            issue=prepared.issue,
            confidence=MatchConfidence.MANUAL,
            move_to_library=True,
            library_root_id=preferred_managed_root_id(prepared.issue.series),
            loaded_issue=prepared.issue,
            ingest_policy=prepared.ingest_policy,
            allow_resource_safety_exception=body.allow_resource_safety_exception,
            replace_existing_library_file=existing_library_file is not None,
            replacement_trash_dir=await resolve_configured_utility_trash_dir(session)
            if existing_library_file is not None
            else None,
        )
    except ManualIssueImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        resource_block = classify_resource_safety_exception(exc)
        if resource_block is not None:
            raise HTTPException(status_code=409, detail=resource_block.reason) from exc
        raise

    logger.info(
        "issue_file_imported",
        issue_id=issue_id,
        library_file_id=library_file.id,
        file_name=library_file.file_name,
        transfer_method=prepared.ingest_policy.post_processing_method,
    )

    return _build_manual_file_import_response(
        issue_id=issue_id,
        library_file=library_file,
    )


@router.delete(
    "/{issue_id}/file",
    response_model=IssueFileDeleteResponse,
)
async def delete_file_for_issue(
    issue_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> IssueFileDeleteResponse:
    """Delete or trash the library file linked to an issue."""
    result = await delete_issue_library_file(session, issue_id)
    logger.info(
        "issue_file_deleted",
        issue_id=result.issue_id,
        status=result.status.value,
        file_deleted=result.file_deleted,
        trashed=result.trashed,
    )
    return IssueFileDeleteResponse(
        issue_id=result.issue_id,
        status=result.status,
        file_deleted=result.file_deleted,
        trashed=result.trashed,
        trash_path=str(result.trash_path) if result.trash_path is not None else None,
    )


@router.post(
    "/{issue_id}/import-file/start",
    status_code=202,
    response_model=ManualFileImportProgressResponse,
)
async def start_import_file_for_issue(
    issue_id: int,
    body: ManualFileImportRequest,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ManualFileImportProgressResponse:
    """Start a background manual import for the issue-detail UI."""
    progress = await start_issue_import_run(issue_id, body)
    await project_issue_import_operation_progress(session, progress)
    await session.commit()
    return progress


@router.post(
    "/{issue_id}/import-file/cancel",
    response_model=ManualFileImportProgressResponse,
)
async def cancel_import_file_for_issue(
    issue_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ManualFileImportProgressResponse:
    """Cancel a background manual import for the issue-detail UI."""
    progress = await cancel_issue_import_run(issue_id)
    await project_issue_import_operation_progress(session, progress)
    await session.commit()
    return progress


@router.get(
    "/{issue_id}/import-file/progress",
    response_model=ManualFileImportProgressResponse,
)
async def get_import_file_for_issue_progress(
    issue_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> ManualFileImportProgressResponse:
    """Return the latest live progress snapshot for one manual issue import."""
    progress = get_issue_import_progress_state(issue_id)
    if progress is not None:
        await project_issue_import_operation_progress(session, progress)
        await session.commit()
        return progress
    return ManualFileImportProgressResponse(
        issue_id=issue_id,
    )


# ── File Download ────────────────────────────────────────────────────


@router.get("/{issue_id}/download-file")
async def download_issue_file(
    issue_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> FileResponse:
    """Download the comic file for an owned issue."""
    result = await session.execute(
        select(Issue).options(joinedload(Issue.library_file)).where(Issue.id == issue_id)
    )
    issue = result.unique().scalar_one_or_none()
    if issue is None:
        raise NotFoundError("Issue", issue_id)

    if issue.library_file is None:
        raise HTTPException(status_code=404, detail="No file available for this issue")

    file_path = Path(issue.library_file.file_path)
    if not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="File no longer exists on disk",
        )

    return FileResponse(
        path=file_path,
        filename=issue.library_file.file_name,
        media_type="application/octet-stream",
    )

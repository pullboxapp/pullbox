"""Intervention queue and history UI routes."""

from collections.abc import Callable, Mapping

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession
from pullbox.models.indexer import IndexerConfig
from pullbox.models.issue import Issue
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.series import Series
from pullbox.services.direct_acquisition_planner_service import DirectAcquisitionPlanningError
from pullbox.ui.intervention_context_loaders import (
    build_intervention_queue_filters,
    load_intervention_context,
    load_intervention_queue_context,
)
from pullbox.ui.intervention_context_loaders import (
    load_intervention_history_context as load_intervention_history_context,
)
from pullbox.ui.intervention_context_loaders import (
    load_intervention_source_options as load_intervention_source_options,
)
from pullbox.ui.intervention_filter_helpers import (
    build_intervention_item_meta,
)
from pullbox.ui.intervention_filter_helpers import (
    get_intervention_history_order_by as get_intervention_history_order_by,
)
from pullbox.ui.intervention_filter_helpers import (
    intervention_match_type_label as intervention_match_type_label,
)
from pullbox.ui.intervention_filter_helpers import (
    intervention_outcome_label as intervention_outcome_label,
)
from pullbox.ui.intervention_filter_helpers import (
    intervention_protocol_label as intervention_protocol_label,
)
from pullbox.ui.intervention_filter_helpers import (
    intervention_resolved_expr as intervention_resolved_expr,
)
from pullbox.ui.intervention_filter_helpers import (
    intervention_review_reason_clause as intervention_review_reason_clause,
)
from pullbox.ui.intervention_filter_helpers import (
    intervention_review_reason_codes as intervention_review_reason_codes,
)
from pullbox.ui.intervention_filter_helpers import (
    intervention_review_reason_summary as intervention_review_reason_summary,
)
from pullbox.ui.intervention_filter_helpers import (
    intervention_source_expr as intervention_source_expr,
)
from pullbox.ui.intervention_filter_helpers import (
    normalize_intervention_confidence_filter as normalize_intervention_confidence_filter,
)
from pullbox.ui.intervention_filter_helpers import (
    normalize_intervention_history_sort as normalize_intervention_history_sort,
)
from pullbox.ui.intervention_filter_helpers import (
    normalize_intervention_outcome_filter as normalize_intervention_outcome_filter,
)
from pullbox.ui.intervention_filter_helpers import (
    normalize_intervention_protocol_filter as normalize_intervention_protocol_filter,
)
from pullbox.ui.intervention_filter_helpers import (
    normalize_intervention_reason_filter as normalize_intervention_reason_filter,
)
from pullbox.ui.intervention_filter_helpers import (
    normalize_intervention_tab as normalize_intervention_tab,
)

logger = structlog.get_logger(__name__)

router = APIRouter()

__all__ = [
    "build_intervention_item_meta",
    "build_intervention_queue_filters",
    "get_intervention_history_order_by",
    "intervention_match_type_label",
    "intervention_outcome_label",
    "intervention_protocol_label",
    "intervention_resolved_expr",
    "intervention_review_reason_clause",
    "intervention_review_reason_codes",
    "intervention_review_reason_summary",
    "intervention_source_expr",
    "load_intervention_context",
    "load_intervention_history_context",
    "load_intervention_queue_context",
    "load_intervention_source_options",
    "normalize_intervention_confidence_filter",
    "normalize_intervention_history_sort",
    "normalize_intervention_outcome_filter",
    "normalize_intervention_protocol_filter",
    "normalize_intervention_reason_filter",
    "normalize_intervention_tab",
]

_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]
_SidebarBadgeResponse = Callable[..., Response]
_SetHxToast = Callable[[Response, str, str], Response]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None
_sidebar_badge_response: _SidebarBadgeResponse | None = None
_set_hx_toast: _SetHxToast | None = None
_sidebar_badge_no_store_headers: Mapping[str, str] = {}


def configure_intervention_routes(
    *,
    get_templates: _GetTemplates,
    build_context: _BuildContext,
    sidebar_badge_response: _SidebarBadgeResponse,
    set_hx_toast: _SetHxToast,
    sidebar_badge_no_store_headers: Mapping[str, str],
) -> None:
    """Provide shared UI runtime dependencies from the facade module."""
    global _get_templates
    global _build_context
    global _sidebar_badge_response
    global _set_hx_toast
    global _sidebar_badge_no_store_headers
    _get_templates = get_templates
    _build_context = build_context
    _sidebar_badge_response = sidebar_badge_response
    _set_hx_toast = set_hx_toast
    _sidebar_badge_no_store_headers = sidebar_badge_no_store_headers


def _templates() -> Jinja2Templates:
    if _get_templates is None:
        msg = "intervention routes have not been configured with templates"
        raise RuntimeError(msg)
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        msg = "intervention routes have not been configured with a context builder"
        raise RuntimeError(msg)
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


def _badge_response(
    request: Request,
    user: object | None,
    *,
    count: int,
    badge_classes: str,
) -> Response:
    if _sidebar_badge_response is None:
        msg = "intervention routes have not been configured with a sidebar badge renderer"
        raise RuntimeError(msg)
    return _sidebar_badge_response(
        request,
        user,
        count=count,
        badge_classes=badge_classes,
    )


def _toast_response(response: Response, message: str, level: str = "info") -> Response:
    if _set_hx_toast is None:
        msg = "intervention routes have not been configured with a toast renderer"
        raise RuntimeError(msg)
    return _set_hx_toast(response, message, level)


@router.get("/intervention", response_class=HTMLResponse, include_in_schema=False)
async def intervention_page(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    tab: str | None = Query(None),
    reason: str | None = Query(None),
    outcome: str | None = Query(None),
    confidence: str | None = Query(None),
    protocol: str | None = Query(None),
    search: str = Query(""),
    sort: str | None = Query(None),
    page: int = Query(1, ge=1),
) -> Response:
    """Render the intervention queue/history workspace page."""
    normalized_tab = tab if isinstance(tab, str) else None
    normalized_reason = reason if isinstance(reason, str) else None
    normalized_outcome = outcome if isinstance(outcome, str) else None
    normalized_confidence = confidence if isinstance(confidence, str) else None
    normalized_protocol = protocol if isinstance(protocol, str) else None
    normalized_search = search if isinstance(search, str) else ""
    normalized_sort = sort if isinstance(sort, str) else None
    normalized_page = page if isinstance(page, int) else 1

    ctx = _ctx(
        request,
        user,
        **await load_intervention_context(
            session,
            tab=normalized_tab,
            reason_filter=normalized_reason,
            outcome_filter=normalized_outcome,
            confidence_filter=normalized_confidence,
            protocol_filter=normalized_protocol,
            search_query=normalized_search,
            sort=normalized_sort,
            requested_page=normalized_page,
        ),
    )

    hx_target = request.headers.get("HX-Target")
    if request.headers.get("HX-Request"):
        if normalized_tab in {"queue", "recovery"} and hx_target == "intervention-queue-results":
            return _templates().TemplateResponse(
                request,
                "partials/intervention_queue_results_bundle.html",
                ctx,
            )
        if normalized_tab == "history" and hx_target == "intervention-history-results":
            return _templates().TemplateResponse(
                request,
                "partials/intervention_history_results_bundle.html",
                ctx,
            )
        return _templates().TemplateResponse(
            request,
            "partials/intervention_content_bundle.html",
            ctx,
        )

    return _templates().TemplateResponse(request, "pages/intervention.html", ctx)


@router.get(
    "/htmx/intervention/content",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_intervention_content(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    tab: str | None = Query(None),
    reason: str | None = Query(None),
    outcome: str | None = Query(None),
    confidence: str | None = Query(None),
    protocol: str | None = Query(None),
    search: str = Query(""),
    sort: str | None = Query(None),
    page: int = Query(1, ge=1),
) -> Response:
    """Return the integrated intervention content panel for HTMX refreshes."""
    normalized_tab = tab if isinstance(tab, str) else None
    normalized_reason = reason if isinstance(reason, str) else None
    normalized_outcome = outcome if isinstance(outcome, str) else None
    normalized_confidence = confidence if isinstance(confidence, str) else None
    normalized_protocol = protocol if isinstance(protocol, str) else None
    normalized_search = search if isinstance(search, str) else ""
    normalized_sort = sort if isinstance(sort, str) else None
    normalized_page = page if isinstance(page, int) else 1

    ctx = _ctx(
        request,
        user,
        **await load_intervention_context(
            session,
            tab=normalized_tab,
            reason_filter=normalized_reason,
            outcome_filter=normalized_outcome,
            confidence_filter=normalized_confidence,
            protocol_filter=normalized_protocol,
            search_query=normalized_search,
            sort=normalized_sort,
            requested_page=normalized_page,
        ),
    )
    hx_target = request.headers.get("HX-Target")
    if normalized_tab in {"queue", "recovery"} and hx_target == "intervention-queue-results":
        return _templates().TemplateResponse(
            request,
            "partials/intervention_queue_results_bundle.html",
            ctx,
            headers=_sidebar_badge_no_store_headers,
        )
    if normalized_tab == "history" and hx_target == "intervention-history-results":
        return _templates().TemplateResponse(
            request,
            "partials/intervention_history_results_bundle.html",
            ctx,
            headers=_sidebar_badge_no_store_headers,
        )
    return _templates().TemplateResponse(
        request,
        "partials/intervention_content_bundle.html",
        ctx,
        headers=_sidebar_badge_no_store_headers,
    )


@router.get(
    "/htmx/intervention/history/{pending_id}/detail",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_intervention_history_detail(
    request: Request,
    pending_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render resolved intervention history details only when expanded."""
    result = await session.execute(
        select(PendingMatch)
        .options(
            joinedload(PendingMatch.issue).joinedload(Issue.series),
            joinedload(PendingMatch.indexer),
        )
        .where(
            PendingMatch.id == pending_id,
            PendingMatch.status != PendingMatchStatus.PENDING,
        )
    )
    pending_match = result.unique().scalar_one_or_none()
    if pending_match is None:
        raise HTTPException(status_code=404, detail="Intervention history detail not found")

    return _templates().TemplateResponse(
        request,
        "partials/intervention_history_detail.html",
        _ctx(
            request,
            user,
            pm=pending_match,
            meta=build_intervention_item_meta(pending_match),
        ),
    )


@router.get(
    "/htmx/intervention/list",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_intervention_list(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    reason: str | None = Query(None),
    confidence: str | None = Query(None),
    protocol: str | None = Query(None),
    search: str = Query(""),
    page: int = Query(1, ge=1),
) -> Response:
    """Return the intervention queue list as an HTMX partial."""
    normalized_reason = reason if isinstance(reason, str) else None
    normalized_confidence = confidence if isinstance(confidence, str) else None
    normalized_protocol = protocol if isinstance(protocol, str) else None
    normalized_search = search if isinstance(search, str) else ""
    normalized_page = page if isinstance(page, int) else 1

    return _templates().TemplateResponse(
        request,
        "partials/intervention_list.html",
        _ctx(
            request,
            user,
            **await load_intervention_queue_context(
                session,
                reason_filter=normalized_reason,
                confidence_filter=normalized_confidence,
                protocol_filter=normalized_protocol,
                search_query=normalized_search,
                requested_page=normalized_page,
            ),
        ),
    )


@router.get("/intervention/selection-ids", include_in_schema=False)
async def intervention_selection_ids(
    _user: AuthenticatedUser,
    session: DbSession,
    reason: str | None = Query(None),
    confidence: str | None = Query(None),
    protocol: str | None = Query(None),
    search: str | None = Query(None),
) -> JSONResponse:
    """Return all pending-match IDs matching the current intervention queue filters."""
    filters, *_ = build_intervention_queue_filters(
        reason_filter=reason,
        confidence_filter=confidence,
        protocol_filter=protocol,
        search_query=search,
    )
    ids = list(
        (
            await session.execute(
                select(PendingMatch.id)
                .select_from(PendingMatch)
                .join(Issue, PendingMatch.issue_id == Issue.id)
                .join(Series, Issue.series_id == Series.id)
                .outerjoin(IndexerConfig, PendingMatch.indexer_id == IndexerConfig.id)
                .where(*filters)
                .order_by(PendingMatch.id)
            )
        )
        .scalars()
        .all()
    )
    return JSONResponse({"ids": ids, "total": len(ids)})


@router.get(
    "/htmx/intervention/count",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_intervention_count(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Return the pending match count as an HTML badge fragment."""
    pending_count: int = (
        await session.execute(
            select(func.count(PendingMatch.id)).where(
                PendingMatch.status == PendingMatchStatus.PENDING
            )
        )
    ).scalar_one()

    return _badge_response(
        request,
        user,
        count=pending_count,
        badge_classes="count-badge-warning",
    )


@router.get(
    "/htmx/intervention/count-text",
    include_in_schema=False,
)
async def htmx_intervention_count_text(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Return the pending match count as plain text for inline dashboard copy."""
    pending_count: int = (
        await session.execute(
            select(func.count(PendingMatch.id)).where(
                PendingMatch.status == PendingMatchStatus.PENDING
            )
        )
    ).scalar_one()

    return Response(
        content=str(pending_count),
        media_type="text/plain",
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/htmx/intervention/bulk-approve",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_intervention_bulk_approve(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Approve multiple pending matches via HTMX - returns refreshed list partial."""
    form = await request.form()
    ids_str = form.get("ids", "")
    reason = str(form.get("reason", "") or "")
    confidence = str(form.get("confidence", "") or "")
    protocol = str(form.get("protocol", "") or "")
    search = str(form.get("search", "") or "")
    requested_page = int(str(form.get("page", "1") or "1") or "1")
    if not ids_str:
        return Response(status_code=400, content="No IDs provided", media_type="text/html")

    pm_ids = [int(x.strip()) for x in str(ids_str).split(",") if x.strip().isdigit()]
    if not pm_ids:
        return Response(status_code=400, content="No valid IDs provided", media_type="text/html")

    from pullbox.composition.services import build_domain_download_service
    from pullbox.services.intervention_service import InterventionService

    built = await build_domain_download_service(session)
    download_svc = built[0] if built is not None else None
    svc = InterventionService(download_service=download_svc)

    for pm_id in pm_ids:
        try:
            await svc.approve_match(session, pm_id)
        except (ValueError, Exception):
            logger.warning("htmx_bulk_approve_item_failed", pending_id=pm_id)

    ctx = _ctx(
        request,
        user,
        **await load_intervention_context(
            session,
            tab="queue",
            reason_filter=reason,
            confidence_filter=confidence,
            protocol_filter=protocol,
            search_query=search,
            requested_page=requested_page,
        ),
    )
    if request.headers.get("HX-Target") == "intervention-queue-results":
        return _templates().TemplateResponse(
            request,
            "partials/intervention_queue_results_bundle.html",
            ctx,
        )
    return _templates().TemplateResponse(
        request,
        "partials/intervention_content_bundle.html",
        ctx,
    )


@router.post(
    "/htmx/intervention/bulk-reject",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_intervention_bulk_reject(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Reject multiple pending matches via HTMX - returns refreshed list partial."""
    form = await request.form()
    ids_str = form.get("ids", "")
    reason = str(form.get("reason", "") or "")
    confidence = str(form.get("confidence", "") or "")
    protocol = str(form.get("protocol", "") or "")
    search = str(form.get("search", "") or "")
    requested_page = int(str(form.get("page", "1") or "1") or "1")
    if not ids_str:
        return Response(status_code=400, content="No IDs provided", media_type="text/html")

    pm_ids = [int(x.strip()) for x in str(ids_str).split(",") if x.strip().isdigit()]
    if not pm_ids:
        return Response(status_code=400, content="No valid IDs provided", media_type="text/html")

    from pullbox.services.intervention_service import InterventionService

    svc = InterventionService()
    successful_rejects = 0
    blocklisted_rejects = 0
    for pm_id in pm_ids:
        try:
            blocklisted = await svc.reject_match(session, pm_id)
            successful_rejects += 1
            blocklisted_rejects += int(blocklisted)
        except (ValueError, Exception):
            logger.warning("htmx_bulk_reject_item_failed", pending_id=pm_id)

    ctx = _ctx(
        request,
        user,
        **await load_intervention_context(
            session,
            tab="queue",
            reason_filter=reason,
            confidence_filter=confidence,
            protocol_filter=protocol,
            search_query=search,
            requested_page=requested_page,
        ),
    )
    if request.headers.get("HX-Target") == "intervention-queue-results":
        response = _templates().TemplateResponse(
            request,
            "partials/intervention_queue_results_bundle.html",
            ctx,
        )
    else:
        response = _templates().TemplateResponse(
            request,
            "partials/intervention_content_bundle.html",
            ctx,
        )

    if successful_rejects > 0:
        noun = "release" if successful_rejects == 1 else "releases"
        dismissed_rejects = successful_rejects - blocklisted_rejects
        if blocklisted_rejects == successful_rejects:
            message = (
                f"Rejected {successful_rejects} {noun} and added "
                + ("it" if successful_rejects == 1 else "them")
                + " to the blocklist."
            )
        elif blocklisted_rejects == 0:
            message = f"Dismissed {successful_rejects} failed {noun} without blocklisting."
        else:
            message = (
                f"Resolved {successful_rejects} {noun}: {blocklisted_rejects} blocklisted "
                f"and {dismissed_rejects} failed reviews dismissed."
            )
        _toast_response(
            response,
            message,
            "success",
        )

    return response


def _direct_approval_failure_message(
    pending_match: PendingMatch,
    error: DirectAcquisitionPlanningError,
) -> str:
    provider_name = str((pending_match.match_details or {}).get("provider_name") or "Provider")
    if error.code == "candidate_not_found":
        return (
            f"{provider_name} no longer offers a downloadable file for this result. "
            "It was removed from the queue; run a new search to try another source."
        )
    if error.code == "source_quota_limited":
        return f"{provider_name} has no download quota available right now. Try again later."
    if error.code == "source_authentication_required":
        return f"{provider_name} authentication needs attention before this result can download."
    if error.retryable or error.code in {"source_unavailable", "provider_timed_out"}:
        return f"{provider_name} is temporarily unavailable. Try approving this result again soon."
    if error.code == "no_eligible_complete_plan":
        return (
            "Pullbox could not verify a complete, eligible download route for the requested "
            "issue. Check the result or artifact-host settings, then try again."
        )
    return "This direct result cannot be queued until its provider configuration is corrected."


@router.post(
    "/htmx/intervention/{pending_id}/approve",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_intervention_approve(
    request: Request,
    pending_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Approve a pending match via HTMX - returns a success card replacement."""
    pm = await session.get(PendingMatch, pending_id)
    if pm is None:
        return Response(status_code=404)

    release_title = pm.release_title
    form_getter = getattr(request, "form", None)
    form = await form_getter() if callable(form_getter) else {}
    reason = str(form.get("reason", "") or "")
    confidence = str(form.get("confidence", "") or "")
    protocol = str(form.get("protocol", "") or "")
    search = str(form.get("search", "") or "")
    requested_page = int(str(form.get("page", "1") or "1") or "1")

    from pullbox.services.intervention_service import (
        InterventionService,
        is_direct_pending_match,
    )

    if is_direct_pending_match(pm) or (pm.match_details or {}).get("source_kind") == "dc":
        svc = InterventionService()
    else:
        from pullbox.composition.services import build_domain_download_service

        built = await build_domain_download_service(session)
        if built is None:
            return _templates().TemplateResponse(
                request,
                "partials/intervention_action_result.html",
                _ctx(
                    request,
                    user,
                    pending_id=pending_id,
                    heading="Could Not Approve",
                    title=release_title,
                    message="No download clients are configured for this Pullbox instance.",
                    tone="error",
                ),
            )

        download_svc, _configs = built
        svc = InterventionService(download_service=download_svc)

    try:
        await svc.approve_match(session, pending_id)
    except DirectAcquisitionPlanningError as exc:
        logger.warning(
            "htmx_direct_intervention_approve_unavailable",
            pending_id=pending_id,
            failure_code=exc.code,
        )
        return _templates().TemplateResponse(
            request,
            "partials/intervention_action_result.html",
            _ctx(
                request,
                user,
                pending_id=pending_id,
                heading="Could Not Approve",
                title=release_title,
                message=_direct_approval_failure_message(pm, exc),
                tone="error",
            ),
        )
    except Exception:
        logger.exception("htmx_intervention_approve_failed", pending_id=pending_id)
        failure_message = (
            "Failed to queue this direct release for acquisition."
            if is_direct_pending_match(pm)
            else "Failed to send this release to the configured download client."
        )
        return _templates().TemplateResponse(
            request,
            "partials/intervention_action_result.html",
            _ctx(
                request,
                user,
                pending_id=pending_id,
                heading="Could Not Approve",
                title=release_title,
                message=failure_message,
                tone="error",
            ),
        )

    if request.headers.get("HX-Target") in {"intervention-queue-results", "intervention-page"}:
        ctx = _ctx(
            request,
            user,
            **await load_intervention_context(
                session,
                tab="queue",
                reason_filter=reason,
                confidence_filter=confidence,
                protocol_filter=protocol,
                search_query=search,
                requested_page=requested_page,
            ),
        )
        template_name = (
            "partials/intervention_queue_results_bundle.html"
            if request.headers.get("HX-Target") == "intervention-queue-results"
            else "partials/intervention_content_bundle.html"
        )
        return _templates().TemplateResponse(request, template_name, ctx)

    return _templates().TemplateResponse(
        request,
        "partials/intervention_action_result.html",
        _ctx(
            request,
            user,
            pending_id=pending_id,
            heading="Approved",
            title=release_title,
            message="Release sent to the download queue.",
            tone="success",
        ),
    )


@router.post(
    "/htmx/intervention/{pending_id}/reject",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_intervention_reject(
    request: Request,
    pending_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Reject a pending match via HTMX - returns a dismissed card replacement."""
    pm = await session.get(PendingMatch, pending_id)
    if pm is None:
        return Response(status_code=404)

    release_title = pm.release_title
    form_getter = getattr(request, "form", None)
    form = await form_getter() if callable(form_getter) else {}
    reason = str(form.get("reason", "") or "")
    confidence = str(form.get("confidence", "") or "")
    protocol = str(form.get("protocol", "") or "")
    search = str(form.get("search", "") or "")
    requested_page = int(str(form.get("page", "1") or "1") or "1")
    tab = str(form.get("tab", "queue") or "queue")

    from pullbox.services.intervention_service import InterventionService

    svc = InterventionService()
    try:
        blocklisted = await svc.reject_match(session, pending_id)
    except ValueError:
        logger.exception("htmx_intervention_reject_failed", pending_id=pending_id)
        return Response(status_code=404)

    if request.headers.get("HX-Target") in {"intervention-queue-results", "intervention-page"}:
        ctx = _ctx(
            request,
            user,
            **await load_intervention_context(
                session,
                tab=tab,
                reason_filter=reason,
                confidence_filter=confidence,
                protocol_filter=protocol,
                search_query=search,
                requested_page=requested_page,
            ),
        )
        template_name = (
            "partials/intervention_queue_results_bundle.html"
            if request.headers.get("HX-Target") == "intervention-queue-results"
            else "partials/intervention_content_bundle.html"
        )
        return _toast_response(
            _templates().TemplateResponse(request, template_name, ctx),
            (
                "Release rejected and added to the blocklist."
                if blocklisted
                else "Failed release dismissed without blocklisting."
            ),
            "success",
        )

    return _templates().TemplateResponse(
        request,
        "partials/intervention_action_result.html",
        _ctx(
            request,
            user,
            pending_id=pending_id,
            heading="Rejected" if blocklisted else "Dismissed",
            title=release_title,
            message=(
                "Release removed from the intervention queue and added to the blocklist."
                if blocklisted
                else "Failed release removed from the intervention queue without blocklisting."
            ),
            tone="neutral",
        ),
    )


@router.post(
    "/htmx/intervention/{pending_id}/retry-recovery",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_intervention_retry_recovery(
    request: Request,
    pending_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Refresh direct download routes after automatic fallback has been exhausted."""
    pending_match = await session.get(PendingMatch, pending_id)
    if pending_match is None:
        return Response(status_code=404)
    form_getter = getattr(request, "form", None)
    form = await form_getter() if callable(form_getter) else {}
    reason = str(form.get("reason", "") or "")
    confidence = str(form.get("confidence", "") or "")
    protocol = str(form.get("protocol", "") or "")
    search = str(form.get("search", "") or "")
    requested_page = int(str(form.get("page", "1") or "1") or "1")

    from pullbox.services.intervention_service import InterventionService

    try:
        await InterventionService().retry_direct_recovery(session, pending_id)
    except (DirectAcquisitionPlanningError, ValueError) as exc:
        logger.warning("htmx_direct_recovery_retry_failed", pending_id=pending_id, error=str(exc))
        return _templates().TemplateResponse(
            request,
            "partials/intervention_action_result.html",
            _ctx(
                request,
                user,
                pending_id=pending_id,
                heading="Could Not Refresh Download Routes",
                title=pending_match.release_title,
                message=(
                    "Pullbox could not find a fresh eligible route. Check provider or "
                    "artifact-host settings, then try again."
                ),
                tone="error",
            ),
        )

    ctx = _ctx(
        request,
        user,
        **await load_intervention_context(
            session,
            tab="recovery",
            reason_filter=reason,
            confidence_filter=confidence,
            protocol_filter=protocol,
            search_query=search,
            requested_page=requested_page,
        ),
    )
    return _toast_response(
        _templates().TemplateResponse(
            request,
            "partials/intervention_queue_results_bundle.html",
            ctx,
        ),
        "Download routes refreshed and acquisition queued.",
        "success",
    )

"""Import workspace UI routes and loaders."""

from collections.abc import Callable, Mapping
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession, InteractiveOperatorUser
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.import_job import (
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.library import LibraryRoot
from pullbox.services.audit_service import source_ip_from_request
from pullbox.services.import_safety_bulk_review import (
    IMPORT_SAFETY_BULK_CONFIRMATION,
    ImportSafetyBulkInterruptedError,
    ImportSafetyBulkPreview,
    allow_import_safety_category_once,
    preview_import_safety_category,
)
from pullbox.services.import_safety_diagnostics import ImportSafetyCategory
from pullbox.services.import_workflow_state import ACTIVE_IMPORT_JOB_STATUSES
from pullbox.tasks.import_task import trigger_import_safety_bulk_rematch
from pullbox.ui import import_orphaned_routes
from pullbox.ui.comicvine_series_search import (
    COMICVINE_SERIES_SEARCH_LIMIT,
    IMPORT_CV_MATCH_DISPLAY_LIMIT,
    format_comicvine_series_results,
    load_existing_series_by_cv_id,
    parse_comicvine_series_query,
    sort_comicvine_series_results,
    wrap_comicvine_provider_for_ui_cache,
)
from pullbox.ui.import_conflict_review import _load_import_conflict_review_context
from pullbox.ui.import_history import (
    _history_resume_step_for_job,
    _load_import_history_context,
)
from pullbox.ui.import_progress_snapshot import build_import_progress_snapshot
from pullbox.ui.import_results_context import load_import_results_context
from pullbox.ui.import_review_context import load_import_review_context
from pullbox.ui.import_review_summary import load_import_review_summary
from pullbox.ui.import_series_details_context import load_import_series_details_context
from pullbox.ui.import_story_arc_entry_review import StoryArcEntryResolutionFilter

router = APIRouter()

_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None


def configure_import_routes(
    *,
    get_templates: _GetTemplates,
    build_context: _BuildContext,
) -> None:
    """Provide shared UI runtime dependencies from the facade module."""
    global _get_templates, _build_context
    _get_templates = get_templates
    _build_context = build_context


def _templates() -> Jinja2Templates:
    if _get_templates is None:
        msg = "import routes have not been configured with templates"
        raise RuntimeError(msg)
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        msg = "import routes have not been configured with a context builder"
        raise RuntimeError(msg)
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


def _object_to_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


_load_import_orphaned_context = import_orphaned_routes.load_import_orphaned_context


def _normalize_progress_mode(
    job: ImportJob,
    *,
    next_step: int,
    mode: str | None,
) -> str:
    """Return the effective progress-shell mode for a job."""
    requested_mode = mode.strip().lower() if isinstance(mode, str) else ""
    if requested_mode in {"scan", "import", "rollback"}:
        return requested_mode
    if job.status in {ImportJobStatus.ROLLING_BACK, ImportJobStatus.ROLLED_BACK}:
        return "rollback"
    if next_step == 5:
        return "import"
    return "scan"


def _progress_completion_target_step(progress_mode: str) -> int:
    """Map a progress-shell mode to its next intentional destination step."""
    if progress_mode == "import":
        return 5
    return 3


def _can_resume_collection_job(job: ImportJob, requested_step: int | None) -> bool:
    """Return whether a collection workspace may explicitly hydrate this job."""
    step = requested_step or 0
    if step == 5:
        return job.status in {ImportJobStatus.COMPLETED, ImportJobStatus.FAILED}
    if step == 4:
        return job.status in {
            ImportJobStatus.IMPORTING,
            ImportJobStatus.PAUSED,
            ImportJobStatus.STALLED,
            ImportJobStatus.CANCELLING,
            ImportJobStatus.ROLLING_BACK,
            ImportJobStatus.COMPLETED,
            ImportJobStatus.FAILED,
        } and (job.status != ImportJobStatus.STALLED or job.import_started_at is not None)
    if step == 3:
        return job.status == ImportJobStatus.REVIEW and job.import_started_at is None
    if step == 2:
        return (
            job.status
            in {
                ImportJobStatus.PENDING,
                ImportJobStatus.SCANNING,
                ImportJobStatus.PAUSING,
                ImportJobStatus.PAUSED,
                ImportJobStatus.ANALYZING,
                ImportJobStatus.MATCHING,
                ImportJobStatus.FILE_MATCHING,
                ImportJobStatus.STALLED,
                ImportJobStatus.REVIEW,
            }
            and job.import_started_at is None
        )
    return False


def _build_collection_href(
    *,
    resume_job_id: int | None = None,
    resume_step: int | None = None,
) -> str:
    """Return the canonical collection-tab href, preserving resume context when present."""
    if resume_job_id is None or resume_step is None or resume_step <= 1:
        return "/import?tab=collection"
    return f"/import?tab=collection&resume_job_id={resume_job_id}&resume_step={resume_step}"


async def _load_collection_nav_context(
    session: AsyncSession,
    *,
    current_resume_job_id: int | None = None,
    current_resume_step: int | None = None,
) -> dict[str, object]:
    """Return shared collection-tab navigation state for the import workspace."""
    if current_resume_job_id is not None and current_resume_step is not None:
        return {
            "collection_href": _build_collection_href(
                resume_job_id=current_resume_job_id,
                resume_step=current_resume_step,
            )
        }

    active_job_result = await session.execute(
        select(ImportJob)
        .where(ImportJob.status.in_(tuple(ACTIVE_IMPORT_JOB_STATUSES)))
        .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
        .limit(1)
    )
    active_job = active_job_result.scalars().first()
    if active_job is None:
        return {"collection_href": _build_collection_href()}

    resume_step = _history_resume_step_for_job(active_job)
    return {
        "collection_href": _build_collection_href(
            resume_job_id=active_job.id,
            resume_step=resume_step,
        )
    }


async def _load_active_collection_resume_context(
    session: AsyncSession,
) -> dict[str, object] | None:
    """Return the latest resumable collection job context for plain `/import` entry."""
    active_job_result = await session.execute(
        select(ImportJob)
        .where(ImportJob.status.in_(tuple(ACTIVE_IMPORT_JOB_STATUSES)))
        .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
        .limit(1)
    )
    active_job = active_job_result.scalars().first()
    if active_job is None:
        return None

    resume_step = _history_resume_step_for_job(active_job)
    if resume_step is None:
        return None

    resume_ctx: dict[str, object] = {
        "resume_job_id": active_job.id,
        "resume_job_status": active_job.status.value,
        "resume_step": resume_step,
    }
    if resume_step in {2, 4}:
        resume_ctx["resume_progress_snapshot"] = await _load_import_progress_snapshot(
            session,
            active_job,
        )
    return resume_ctx


async def _load_import_collection_context(session: AsyncSession) -> dict[str, object]:
    """Load the collection import wizard context."""
    roots_result = await session.execute(select(LibraryRoot).order_by(LibraryRoot.name))
    library_roots = list(roots_result.scalars().all())
    from pullbox.services.library_root_management import list_library_roots

    enabled_root_options = [
        root for root in await list_library_roots(session) if bool(root["enabled"])
    ]
    enabled_root_options.sort(
        key=lambda root: (
            not bool(root["is_default_managed_destination"]),
            str(root["name"]).casefold(),
        )
    )

    jobs_result = await session.execute(
        select(ImportJob).order_by(ImportJob.created_at.desc()).limit(10)
    )
    recent_jobs = list(jobs_result.scalars().all())

    return {
        "library_roots": library_roots,
        "library_root_options": enabled_root_options,
        "recent_jobs": recent_jobs,
        "resume_step": None,
        "resume_job_id": None,
        "resume_job_status": "",
        "resume_progress_snapshot": None,
    }


async def _load_import_progress_snapshot(
    session: AsyncSession,
    job: ImportJob,
) -> dict[str, object]:
    """Build the durable Step 2/4 snapshot for progress hydration."""
    recent_logs_result = await session.execute(
        select(ImportJobLog)
        .where(ImportJobLog.import_job_id == job.id)
        .order_by(ImportJobLog.logged_at.desc())
        .limit(12)
    )
    recent_logs = list(reversed(recent_logs_result.scalars().all()))

    review_summary = await load_import_review_summary(session, job)

    snapshot: dict[str, object] = dict(job.progress_snapshot or {})
    progress_revision = _object_to_int(
        snapshot.get("progress_revision"),
        int(job.progress_revision or 0),
    )
    if job.status in {
        ImportJobStatus.REVIEW,
        ImportJobStatus.COMPLETED,
        ImportJobStatus.CANCELLED,
        ImportJobStatus.FAILED,
        ImportJobStatus.ROLLED_BACK,
    }:
        from pullbox.tasks.import_task import get_highest_visible_progress_revision

        visible_revision = get_highest_visible_progress_revision(job.id)
        if visible_revision >= progress_revision:
            progress_revision = visible_revision + 1

    return build_import_progress_snapshot(
        job,
        review_summary=review_summary,
        recent_logs=recent_logs,
        progress_revision=progress_revision,
    )


async def _load_import_workspace_counts(session: AsyncSession) -> dict[str, object]:
    """Load shared counts used by the unified Import workspace tabs."""
    from pullbox.composition.services import build_import_control_service

    svc = build_import_control_service()

    return {
        "unmatched_count": await svc.get_orphaned_count(session),
    }


@router.get("/import", response_class=HTMLResponse, include_in_schema=False)
async def import_page(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    tab: str = Query("collection"),
    view: str = Query("all"),
    search: str = Query(""),
    sort: str = Query(""),
    page: int = Query(1, ge=1),
    resume_job_id: int | None = Query(None),
    resume_step: int | None = Query(None),
) -> Response:
    """Render the unified Import workspace and its tab-scoped partials."""
    normalized_tab = tab if tab in {"collection", "unmatched", "history"} else "collection"

    workspace_ctx: dict[str, object]
    if normalized_tab == "history":
        workspace_ctx = await _load_import_history_context(
            session,
            search_query=search,
            sort=sort,
            requested_page=page,
        )
    elif normalized_tab == "unmatched":
        workspace_ctx = await _load_import_orphaned_context(
            session,
            view=view,
            requested_page=page,
        )
    else:
        workspace_ctx = await _load_import_collection_context(session)
        if resume_job_id is not None:
            resume_job = await session.get(ImportJob, resume_job_id)
            if resume_job is not None and _can_resume_collection_job(resume_job, resume_step):
                workspace_ctx["resume_job_id"] = resume_job.id
                workspace_ctx["resume_job_status"] = resume_job.status.value
                if resume_step is not None:
                    workspace_ctx["resume_step"] = resume_step
                if resume_step in {2, 4}:
                    workspace_ctx[
                        "resume_progress_snapshot"
                    ] = await _load_import_progress_snapshot(
                        session,
                        resume_job,
                    )
        else:
            active_resume_ctx = await _load_active_collection_resume_context(session)
            if active_resume_ctx is not None:
                workspace_ctx.update(active_resume_ctx)

    collection_nav_ctx = await _load_collection_nav_context(
        session,
        current_resume_job_id=(
            _object_to_int(workspace_ctx["resume_job_id"])
            if workspace_ctx.get("resume_job_id") is not None
            else None
        ),
        current_resume_step=(
            _object_to_int(workspace_ctx["resume_step"])
            if workspace_ctx.get("resume_step") is not None
            else None
        ),
    )

    ctx = _ctx(
        request,
        user,
        tab=normalized_tab,
        **await _load_import_workspace_counts(session),
        **collection_nav_ctx,
        **workspace_ctx,
    )

    hx_target = request.headers.get("HX-Target")
    if request.headers.get("HX-Request"):
        if hx_target == "import-history-results" and normalized_tab == "history":
            return _templates().TemplateResponse(
                request,
                "partials/import_history_content_bundle.html",
                ctx,
            )
        if hx_target == "import-history-page" and normalized_tab == "history":
            return _templates().TemplateResponse(
                request,
                "partials/import_history_panel_bundle.html",
                ctx,
            )
        if hx_target == "import-orphaned-results" and normalized_tab == "unmatched":
            return _templates().TemplateResponse(
                request,
                "partials/import_orphaned_content_bundle.html",
                ctx,
            )
        return _templates().TemplateResponse(request, "partials/import_content_bundle.html", ctx)

    return _templates().TemplateResponse(request, "pages/import.html", ctx)


@router.get(
    "/import/{job_id}/progress-partial",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_progress_partial(
    job_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    next_step: int = Query(3),
    mode: str | None = Query(None),
) -> Response:
    """Render the scan progress partial for an import job."""
    from pullbox.core.exceptions import NotFoundError

    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    progress_snapshot = await _load_import_progress_snapshot(session, job)
    progress_mode = _normalize_progress_mode(job, next_step=next_step, mode=mode)
    return _templates().TemplateResponse(
        request,
        "partials/import_step_progress.html",
        _ctx(
            request,
            user,
            job=job,
            next_step=next_step,
            progress_mode=progress_mode,
            completion_target_step=_progress_completion_target_step(progress_mode),
            progress_snapshot=progress_snapshot,
            resume_step=2 if progress_mode == "scan" else 4,
            resume_job_id=job.id,
            resume_progress_snapshot=progress_snapshot,
        ),
    )


@router.get(
    "/import/{job_id}/progress-state",
    response_class=JSONResponse,
    include_in_schema=False,
)
async def import_progress_state(
    job_id: int,
    _request: Request,
    _user: AuthenticatedUser,
    session: DbSession,
) -> JSONResponse:
    """Return the merged live progress snapshot for the import wizard."""
    from pullbox.core.exceptions import NotFoundError

    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    return JSONResponse(await _load_import_progress_snapshot(session, job))


@router.get(
    "/import/{job_id}/review-partial",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_review_partial(
    job_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    sort: str | None = Query(None),
    story_arc_id: int | None = Query(None, ge=1),
    arc_entry_state: StoryArcEntryResolutionFilter = StoryArcEntryResolutionFilter.ALL,
    arc_entry_page: int = Query(1, ge=1),
) -> Response:
    """Render the review table partial for an import job."""

    return await _render_import_review_partial(
        job_id,
        request,
        user,
        session,
        status=status,
        page=page,
        sort=sort,
        story_arc_id=story_arc_id,
        arc_entry_state=arc_entry_state,
        arc_entry_page=arc_entry_page,
    )


async def _render_import_review_partial(
    job_id: int,
    request: Request,
    user: object,
    session: AsyncSession,
    *,
    status: str | None,
    page: int,
    sort: str | None,
    story_arc_id: int | None = None,
    arc_entry_state: StoryArcEntryResolutionFilter = StoryArcEntryResolutionFilter.ALL,
    arc_entry_page: int = 1,
    extra_context: Mapping[str, object] | None = None,
) -> Response:
    """Render the canonical review partial with optional route-local state."""

    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    template_ctx = await load_import_review_context(
        session,
        job,
        status=status,
        page=page,
        sort=sort,
        story_arc_id=story_arc_id,
        arc_entry_state=arc_entry_state,
        arc_entry_page=arc_entry_page,
    )
    if extra_context:
        template_ctx.update(extra_context)

    return _templates().TemplateResponse(
        request,
        "partials/import_step_review.html",
        _ctx(
            request,
            user,
            **template_ctx,
        ),
    )


async def _load_import_safety_bulk_preview(
    session: AsyncSession,
    *,
    job_id: int,
    category: ImportSafetyCategory,
    actor_id: int,
) -> ImportSafetyBulkPreview:
    """Load an authoritative signed preview or hide unavailable actions."""
    try:
        preview = await preview_import_safety_category(
            session,
            job_id,
            category,
            actor_id=actor_id,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    if not preview.overrideable or preview.preview_token is None:
        raise HTTPException(status_code=404, detail="Bulk safety action not available.")
    return preview


@router.get(
    "/import/{job_id}/safety/categories/{category}/preview",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_review_preview_safety_category(
    job_id: int,
    category: ImportSafetyCategory,
    request: Request,
    user: InteractiveOperatorUser,
    session: DbSession,
    status: str | None = Query("safety_blocked"),
    page: int = Query(1, ge=1),
    sort: str | None = Query(None),
) -> Response:
    """Render a signed, exact, read-only category preview for Step 3."""
    preview = await _load_import_safety_bulk_preview(
        session,
        job_id=job_id,
        category=category,
        actor_id=user.id,
    )
    return await _render_import_review_partial(
        job_id,
        request,
        user,
        session,
        status=status,
        page=page,
        sort=sort,
        extra_context={"safety_bulk_preview": preview},
    )


@router.post(
    "/import/{job_id}/safety/categories/{category}/allow-once",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_review_allow_safety_category_once(
    job_id: int,
    category: ImportSafetyCategory,
    request: Request,
    user: InteractiveOperatorUser,
    session: DbSession,
    preview_token: Annotated[str, Form(min_length=1, max_length=4096)],
    confirmation: Annotated[str, Form(max_length=64)] = "",
    status: str | None = Query("safety_blocked"),
    page: int = Query(1, ge=1),
    sort: str | None = Query(None),
) -> Response:
    """Apply one exact signed category preview, then refresh Step 3."""
    if category is not ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT:
        raise HTTPException(status_code=404, detail="Bulk safety action not available.")
    if confirmation != IMPORT_SAFETY_BULK_CONFIRMATION:
        preview = await _load_import_safety_bulk_preview(
            session,
            job_id=job_id,
            category=category,
            actor_id=user.id,
        )
        return await _render_import_review_partial(
            job_id,
            request,
            user,
            session,
            status=status,
            page=page,
            sort=sort,
            extra_context={
                "safety_bulk_preview": preview,
                "safety_bulk_error": "Type ALLOW ONCE exactly to confirm this action.",
                "safety_bulk_error_category": category.value,
            },
        )

    try:
        await allow_import_safety_category_once(
            session,
            job_id,
            category,
            actor_id=user.id,
            actor_username=user.username,
            source_ip=source_ip_from_request(request),
            preview_token=preview_token,
        )
    except ImportSafetyBulkInterruptedError:
        trigger_import_safety_bulk_rematch(job_id)
        return await _render_import_review_partial(
            job_id,
            request,
            user,
            session,
            status=status,
            page=page,
            sort=sort,
            extra_context={
                "safety_bulk_error": (
                    "The bulk action stopped because the import job changed. "
                    "The review below shows the latest state."
                ),
                "safety_bulk_error_category": category.value,
            },
        )
    except ValidationError as exc:
        await session.rollback()
        return await _render_import_review_partial(
            job_id,
            request,
            user,
            session,
            status=status,
            page=page,
            sort=sort,
            extra_context={
                "safety_bulk_error": exc.message,
                "safety_bulk_error_category": category.value,
            },
        )

    trigger_import_safety_bulk_rematch(job_id)
    return await import_review_partial(
        job_id,
        request,
        user,
        session,
        status=status,
        page=page,
        sort=sort,
    )


@router.post(
    "/import/{job_id}/files/{file_id}/safety/allow-once",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_review_allow_safety_file_once(
    job_id: int,
    file_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    status: str | None = Query("safety_blocked"),
    page: int = Query(1, ge=1),
    sort: str | None = Query(None),
) -> Response:
    """Allow one safety-blocked import file and refresh Step 3 review."""
    from fastapi import HTTPException

    from pullbox.composition.services import build_import_control_service
    from pullbox.core.exceptions import ValidationError
    from pullbox.tasks.import_task import trigger_import_series_rematch

    service = build_import_control_service()
    try:
        imported_series = await service.allow_safety_blocked_file_once(session, job_id, file_id)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc

    should_rematch = imported_series.status in {
        ImportSeriesStatus.MATCHED,
        ImportSeriesStatus.DUPLICATE,
    }
    await session.commit()
    if should_rematch:
        trigger_import_series_rematch(job_id, imported_series.id)

    return await import_review_partial(
        job_id,
        request,
        user,
        session,
        status=status,
        page=page,
        sort=sort,
    )


@router.post(
    "/import/{job_id}/files/{file_id}/safety/skip",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_review_skip_safety_file(
    job_id: int,
    file_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    status: str | None = Query("safety_blocked"),
    page: int = Query(1, ge=1),
    sort: str | None = Query(None),
) -> Response:
    """Skip one safety-blocked import file and refresh Step 3 review."""
    from fastapi import HTTPException

    from pullbox.composition.services import build_import_control_service
    from pullbox.core.exceptions import ValidationError

    service = build_import_control_service()
    try:
        await service.skip_safety_blocked_file(session, job_id, file_id)
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc

    await session.commit()
    return await import_review_partial(
        job_id,
        request,
        user,
        session,
        status=status,
        page=page,
        sort=sort,
    )


@router.get(
    "/import/{job_id}/series/{series_id}/reconcile",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_series_reconcile(
    job_id: int,
    series_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render the Step 3 file reconciliation modal for an import review row."""
    from pullbox.composition.services import build_import_service

    service = await build_import_service(session)
    payload = await service.get_import_reconcile_context(session, job_id, series_id)

    return _templates().TemplateResponse(
        request,
        "partials/import_reconcile_modal.html",
        _ctx(
            request,
            user,
            imported_series=payload["imported_series"],
            issue_options=payload["issue_options"],
            files=payload["files"],
            files_remaining=payload["files_remaining"],
            files_completed=payload["files_completed"],
            csrf_token=getattr(getattr(request, "state", object()), "csrf_token", ""),
        ),
    )


@router.get(
    "/import/{job_id}/results-partial",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_results_partial(
    job_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render the import results partial."""
    from pullbox.core.exceptions import NotFoundError, ValidationError

    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status not in {ImportJobStatus.COMPLETED, ImportJobStatus.FAILED}:
        raise ValidationError("Results are only available for completed or failed imports.")
    progress_snapshot = await _load_import_progress_snapshot(session, job)
    results_context = await load_import_results_context(session, job)

    return _templates().TemplateResponse(
        request,
        "partials/import_results.html",
        _ctx(
            request,
            user,
            job=job,
            **results_context,
            resume_step=5,
            resume_job_id=job.id,
            resume_progress_snapshot=progress_snapshot,
        ),
    )


@router.get(
    "/import/{job_id}/series/{series_id}/details-partial",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_series_details_partial(
    job_id: int,
    series_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render the per-series review details modal."""
    details_context = await load_import_series_details_context(
        session,
        job_id=job_id,
        series_id=series_id,
    )

    return _templates().TemplateResponse(
        request,
        "partials/import_series_details_modal.html",
        _ctx(
            request,
            user,
            **details_context,
        ),
    )


@router.get(
    "/import/{job_id}/conflicts-partial",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_conflicts_partial(
    job_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    sort: str = Query("series"),
) -> Response:
    """Render the conflict resolution table partial for compatibility callers."""
    conflict_ctx = await _load_import_conflict_review_context(
        job_id,
        session,
        page=page,
        sort=sort,
    )
    return _templates().TemplateResponse(
        request,
        "partials/import_conflict_cards.html",
        _ctx(
            request,
            user,
            **conflict_ctx,
        ),
    )


@router.get(
    "/import/{job_id}/log-panel",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_log_panel(
    job_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    level: str | None = Query(None),
) -> Response:
    """Render the log panel partial for an import job."""
    from pullbox.core.exceptions import NotFoundError

    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)

    _ = (page, page_size, level)

    return _templates().TemplateResponse(
        request,
        "partials/import_job_log_panel.html",
        _ctx(
            request,
            user,
            job=job,
        ),
    )


@router.get(
    "/import/{job_id}/series/{series_id}/cv-search",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_cv_search(
    job_id: int,
    series_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    q: str = Query(""),
) -> Response:
    """Search ComicVine for matching series and return inline result partial."""
    from pullbox.core.comicvine_key import get_comicvine_api_key
    from pullbox.core.exceptions import NotFoundError
    from pullbox.models.import_job import ImportedSeries
    from pullbox.providers.metadata.comicvine import ComicVineProvider

    # Verify the imported series exists
    item = await session.get(ImportedSeries, series_id)
    if item is None or item.import_job_id != job_id:
        raise NotFoundError("ImportedSeries", series_id)

    results: list[dict[str, object]] = []
    search_error = ""
    query_text = q.strip()
    if query_text:
        parsed_query = parse_comicvine_series_query(query_text)
        api_key = await get_comicvine_api_key(session)
        if api_key:
            try:
                provider = wrap_comicvine_provider_for_ui_cache(
                    ComicVineProvider(api_key=api_key, rate_limit=10),
                    request,
                )
                cv_results, _total_results = await provider.search_series_globally(
                    parsed_query.title_query,
                    max_results=COMICVINE_SERIES_SEARCH_LIMIT,
                    suppress_errors=False,
                )
                sorted_results = sort_comicvine_series_results(
                    list(cv_results),
                    "relevance",
                    query=parsed_query.title_query,
                    year_hint=parsed_query.year_hint,
                )
                visible_results = sorted_results[:IMPORT_CV_MATCH_DISPLAY_LIMIT]
                existing_series_by_cv_id = await load_existing_series_by_cv_id(
                    session,
                    visible_results,
                )
                results = format_comicvine_series_results(
                    visible_results,
                    existing_series_by_cv_id=existing_series_by_cv_id,
                )
            except Exception as exc:
                search_error = str(exc)
        else:
            search_error = "No ComicVine API key configured"

    return _templates().TemplateResponse(
        request,
        "partials/import_cv_search_results.html",
        _ctx(
            request,
            user,
            job_id=job_id,
            series_id=series_id,
            query=q,
            results=results,
            results_limit=IMPORT_CV_MATCH_DISPLAY_LIMIT,
            search_error=search_error,
        ),
    )

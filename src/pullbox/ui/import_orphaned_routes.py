"""Import orphaned-series UI routes and loaders."""

from collections.abc import Callable, Mapping

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession
from pullbox.models.import_job import ImportedSeries, ImportJob, ImportJobStatus, ImportSeriesStatus
from pullbox.ui.comicvine_series_search import (
    COMICVINE_SERIES_SEARCH_LIMIT,
    IMPORT_CV_MATCH_DISPLAY_LIMIT,
    format_comicvine_series_results,
    load_existing_series_by_cv_id,
    parse_comicvine_series_query,
    sort_comicvine_series_results,
    wrap_comicvine_provider_for_ui_cache,
)

router = APIRouter()

_IMPORT_ORPHANED_PAGE_SIZE = 25

_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None


def configure_import_orphaned_routes(
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
        msg = "import orphaned routes have not been configured with templates"
        raise RuntimeError(msg)
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        msg = "import orphaned routes have not been configured with a context builder"
        raise RuntimeError(msg)
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


async def load_import_orphaned_context(
    session: AsyncSession,
    *,
    view: str,
    requested_page: int,
    job_id: int | None = None,
) -> dict[str, object]:
    """Load unmatched-series page data for the requested view and page."""
    from pullbox.composition.services import build_import_control_service

    svc = build_import_control_service()

    normalized_view = "dismissed" if view == "dismissed" else "all"

    if normalized_view == "dismissed":
        count_q = (
            select(func.count())
            .select_from(ImportedSeries)
            .join(ImportJob, ImportedSeries.import_job_id == ImportJob.id)
            .where(
                ImportedSeries.status == ImportSeriesStatus.SKIPPED,
                ImportJob.status == ImportJobStatus.COMPLETED,
            )
        )
        total = (await session.execute(count_q)).scalar() or 0
        total_pages = max(1, (total + _IMPORT_ORPHANED_PAGE_SIZE - 1) // _IMPORT_ORPHANED_PAGE_SIZE)
        page = min(requested_page, total_pages)

        query = (
            select(ImportedSeries)
            .join(ImportJob, ImportedSeries.import_job_id == ImportJob.id)
            .where(
                ImportedSeries.status == ImportSeriesStatus.SKIPPED,
                ImportJob.status == ImportJobStatus.COMPLETED,
            )
            .order_by(ImportedSeries.raw_series_name.asc())
            .offset((page - 1) * _IMPORT_ORPHANED_PAGE_SIZE)
            .limit(_IMPORT_ORPHANED_PAGE_SIZE)
        )
        result = await session.execute(query)
        items = list(result.scalars().all())
    else:
        items, total = await svc.get_orphaned_series(
            session,
            page=requested_page,
            page_size=_IMPORT_ORPHANED_PAGE_SIZE,
            job_id=job_id,
        )
        total_pages = max(1, (total + _IMPORT_ORPHANED_PAGE_SIZE - 1) // _IMPORT_ORPHANED_PAGE_SIZE)
        page = min(requested_page, total_pages)
        if page != requested_page:
            items, total = await svc.get_orphaned_series(
                session,
                page=page,
                page_size=_IMPORT_ORPHANED_PAGE_SIZE,
                job_id=job_id,
            )

    orphaned_count = await svc.get_orphaned_count(session, job_id=job_id)

    dismissed_q = (
        select(func.count())
        .select_from(ImportedSeries)
        .join(ImportJob, ImportedSeries.import_job_id == ImportJob.id)
        .where(
            ImportedSeries.status == ImportSeriesStatus.SKIPPED,
            ImportJob.status == ImportJobStatus.COMPLETED,
        )
    )
    dismissed_count = (await session.execute(dismissed_q)).scalar() or 0

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": _IMPORT_ORPHANED_PAGE_SIZE,
        "view": normalized_view,
        "orphaned_count": orphaned_count,
        "dismissed_count": dismissed_count,
        "total_pages": total_pages,
    }


@router.get(
    "/import/orphaned/{imported_series_id}/cv-search",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_orphaned_cv_search(
    imported_series_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    q: str = Query(""),
) -> Response:
    """CV search popover for an orphaned series row."""
    from pullbox.core.comicvine_key import get_comicvine_api_key
    from pullbox.core.exceptions import NotFoundError
    from pullbox.providers.metadata.comicvine import ComicVineProvider

    item = await session.get(ImportedSeries, imported_series_id)
    if item is None:
        raise NotFoundError("ImportedSeries", imported_series_id)

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
        "partials/import_orphaned_cv_search.html",
        _ctx(
            request,
            user,
            imported_series_id=imported_series_id,
            query=q,
            results=results,
            search_error=search_error,
        ),
    )


@router.get(
    "/import/orphaned/{imported_series_id}/recovery",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def import_orphaned_recovery(
    imported_series_id: int,
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Recovery drawer/modal for an unmatched series after CV identification."""
    from pullbox.composition.services import build_import_service
    from pullbox.tasks.import_orphan_recovery_task import get_orphan_recovery_progress_state

    service = await build_import_service(session)
    payload = await service.get_orphan_recovery_context(session, imported_series_id)
    recovery_progress = get_orphan_recovery_progress_state(imported_series_id)
    if recovery_progress is not None and recovery_progress.state != "running":
        recovery_progress = None

    return _templates().TemplateResponse(
        request,
        "partials/import_orphaned_recovery.html",
        _ctx(
            request,
            user,
            imported_series=payload["imported_series"],
            issue_options=payload["issue_options"],
            files=payload["files"],
            requires_library_root=payload["requires_library_root"],
            selected_library_root_id=payload["selected_library_root_id"],
            available_library_roots=payload["available_library_roots"],
            files_remaining=payload["files_remaining"],
            files_completed=payload["files_completed"],
            recovery_progress=(
                recovery_progress.model_dump(mode="json") if recovery_progress is not None else None
            ),
        ),
    )

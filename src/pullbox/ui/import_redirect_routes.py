"""Legacy import workspace redirect routes."""

from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession

router = APIRouter()


@router.get("/import/history", response_class=HTMLResponse, include_in_schema=False)
async def import_history_redirect(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Redirect legacy import history URLs to the unified Import workspace."""
    return RedirectResponse(url="/import?tab=history", status_code=307)


@router.get("/import/orphaned", response_class=HTMLResponse, include_in_schema=False)
async def import_orphaned_redirect(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    tab: str = Query("all"),
    page: int = Query(1, ge=1),
) -> Response:
    """Redirect legacy unmatched-series URLs to the Follow-up workspace."""
    params = {"tab": "follow-up", "view": "dismissed" if tab == "dismissed" else "all"}
    if page != 1:
        params["page"] = str(page)
    return RedirectResponse(url=f"/import?{urlencode(params)}", status_code=307)

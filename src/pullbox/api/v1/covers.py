"""Cover image serving API — resolves covers from series folders or legacy storage.

Series folder covers take priority over the legacy ``data/covers/`` directory.
Returns proper image responses with caching headers, or 404 if no cover exists.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import APIRouter
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from pullbox.api.deps import AuthenticatedUser, DbSession  # noqa: TC001
from pullbox.config import get_settings
from pullbox.core.exceptions import NotFoundError
from pullbox.core.library_root_resolution import resolve_path_inside_roots
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.models.story_arc import StoryArc
from pullbox.services.cover_cache_service import (
    cache_series_cover,
    cache_story_arc_cover,
    resolve_series_cover_file,
    resolve_story_arc_cover_file,
)
from pullbox.services.cover_url_service import story_arc_provider_cover_url

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["covers"], include_in_schema=False)

# Series cover URLs are authenticated and versioned by callers, so private
# browser caching keeps grid/list navigation snappy without shared caching.
_VERSIONED_CACHE_HEADERS = {"Cache-Control": "private, max-age=31536000, immutable"}
_REVALIDATING_CACHE_HEADERS = {"Cache-Control": "private, no-cache, max-age=0, must-revalidate"}


def _serve_image(
    path: Path,
    *,
    cache_headers: dict[str, str] | None = None,
) -> FileResponse:
    """Return a FileResponse for an image with caching headers."""
    response_headers = cache_headers or _VERSIONED_CACHE_HEADERS
    path = path.expanduser().resolve(strict=True)
    # Infer media type from extension
    suffix = path.suffix.lower()
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(suffix, "image/jpeg")

    return FileResponse(
        path=path,
        media_type=media_type,
        headers=response_headers,
    )


def _serve_issue_image(path: Path) -> FileResponse:
    """Return an issue image without immutable caching for unversioned callers."""
    return _serve_image(path, cache_headers=_REVALIDATING_CACHE_HEADERS)


def _find_cover_file(directory: Path, stem: str) -> Path | None:
    """Look for a cover file with any common image extension."""
    root = directory.expanduser().resolve(strict=False)
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        try:
            candidate = resolve_path_inside_roots(root / f"{stem}{ext}", [root], require_file=True)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


# ── Series Cover ──────────────────────────────────────────────────


@router.get("/series/{series_id}/cover")
async def get_series_cover(
    series_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Serve the series cover image.

    Resolution order:
    1. ``{series.path}/cover.{ext}`` (series folder)
    2. ``{covers_dir}/{series_id}/series.{ext}`` (legacy location)
    3. 404
    """
    series = await session.get(Series, series_id)
    if series is None:
        raise NotFoundError("Series", series_id)

    # 1-3. Try any existing local cover location
    cover = await resolve_series_cover_file(session, series)
    if cover:
        if not series.cover_path:
            series.cover_path = f"/api/v1/series/{series.id}/cover"
            await session.commit()
        return _serve_image(cover)

    # 4. Download and cache a remote cover on demand
    if series.cover_url:
        cover = await cache_series_cover(session, series)
        if cover:
            await session.commit()
            return _serve_image(cover)

    # 5. No cover found
    return Response(status_code=404)


# ── Story Arc Cover ───────────────────────────────────────────────


@router.get("/story-arcs/{story_arc_id}/cover")
async def get_story_arc_cover(
    story_arc_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Serve cached provider artwork for one Story Arc."""
    story_arc = await session.get(StoryArc, story_arc_id)
    if story_arc is None:
        raise NotFoundError("Story Arc", story_arc_id)
    cover = await resolve_story_arc_cover_file(session, story_arc)
    if cover:
        if not story_arc.cover_path:
            story_arc.cover_path = f"/api/v1/story-arcs/{story_arc.id}/cover"
            await session.commit()
        return _serve_image(cover)
    provider_url = story_arc_provider_cover_url(story_arc)
    if provider_url:
        if not story_arc.cover_url:
            story_arc.cover_url = provider_url
        cover = await cache_story_arc_cover(session, story_arc)
        if cover:
            await session.commit()
            return _serve_image(cover)
    return Response(status_code=404)


# ── Issue Cover ───────────────────────────────────────────────────


@router.get("/issues/{issue_id}/cover")
async def get_issue_cover(
    issue_id: int,
    _user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Serve an issue cover image.

    Resolution order:
    1. ``{series.path}/issue_{number}.{ext}`` (series folder, by issue number)
    2. ``{covers_dir}/{series_id}/issue_{issue_id}.{ext}`` (legacy, by DB ID)
    3. Series cover as fallback
    4. 404
    """
    result = await session.execute(
        select(Issue).options(joinedload(Issue.series)).where(Issue.id == issue_id)
    )
    issue = result.unique().scalar_one_or_none()
    if issue is None:
        raise NotFoundError("Issue", issue_id)

    # Format issue number for filename (e.g. 1.0 → "001", 1.5 → "001.5")
    num = issue.issue_number
    issue_num_str = f"{int(num):03d}" if num == int(num) else f"{num:06.1f}"

    # 1. Try series folder (by issue number — human-readable, survives DB rebuilds)
    if issue.series and issue.series.path:
        series_path = Path(issue.series.path)
        cover = _find_cover_file(series_path, f"issue_{issue_num_str}")
        if cover:
            return _serve_issue_image(cover)

    # 2. Try .covers/ directory (by issue number)
    from pullbox.services.cover_resolver import resolve_covers_dir

    covers_base = await resolve_covers_dir(session)
    if issue.series:
        covers_dir = covers_base / str(issue.series_id)
        cover = _find_cover_file(covers_dir, f"issue_{issue_num_str}")
        if cover:
            return _serve_issue_image(cover)

    # 3. Try legacy location (by DB ID)
    settings = get_settings()
    if issue.series and settings.covers_dir != covers_base:
        legacy_dir = settings.covers_dir / str(issue.series_id)
        cover = _find_cover_file(legacy_dir, f"issue_{issue_id}")
        if cover:
            return _serve_issue_image(cover)

    # 4. Fall back to series cover
    if issue.series:
        if issue.series.path:
            cover = _find_cover_file(Path(issue.series.path), "cover")
            if cover:
                return _serve_issue_image(cover)
        covers_dir = covers_base / str(issue.series_id)
        cover = _find_cover_file(covers_dir, "series")
        if cover:
            return _serve_issue_image(cover)
        if settings.covers_dir != covers_base:
            legacy_dir = settings.covers_dir / str(issue.series_id)
            cover = _find_cover_file(legacy_dir, "series")
            if cover:
                return _serve_issue_image(cover)

    # 5. No cover found
    return Response(status_code=404)

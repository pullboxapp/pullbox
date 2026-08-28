"""Local cover cache helpers for series and issue artwork."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx
import structlog

from pullbox.config import get_settings
from pullbox.services.cover_resolver import resolve_covers_dir

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.series import Series

logger = structlog.get_logger(__name__)
_SERIES_COVER_LOCKS: dict[int, asyncio.Lock] = {}
_SUPPORTED_COVER_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def find_cover_file(directory: Path, stem: str) -> Path | None:
    """Look for a cover file with any common image extension."""
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = directory / f"{stem}{ext}"
        if candidate.is_file():
            return candidate
    return None


def find_imported_series_cover(directory: Path) -> Path | None:
    """Return Mylar's full-size local cover or its thumbnail fallback."""
    for file_name in ("cover.jpg", "folder.jpg"):
        candidate = directory / file_name
        if candidate.is_file():
            return candidate
    return None


def suffix_for_cover(content_type: str | None, image_url: str | None) -> str:
    """Choose a stable local filename suffix for a downloaded cover."""
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type == "image/png":
        return ".png"
    if media_type == "image/webp":
        return ".webp"

    path = urlparse(image_url or "").path.lower()
    if path.endswith(".png"):
        return ".png"
    if path.endswith(".webp"):
        return ".webp"
    if path.endswith(".jpeg"):
        return ".jpeg"
    return ".jpg"


async def resolve_series_cover_file(session: AsyncSession, series: Series) -> Path | None:
    """Return the local cover file for a series if one exists."""
    if series.path:
        cover = find_cover_file(Path(series.path), "cover")
        if cover:
            return cover

    covers_base = await resolve_covers_dir(session)
    covers_dir = covers_base / str(series.id)
    cover = find_cover_file(covers_dir, "series")
    if cover:
        return cover

    settings = get_settings()
    if settings.covers_dir != covers_base:
        legacy_dir = settings.covers_dir / str(series.id)
        cover = find_cover_file(legacy_dir, "series")
        if cover:
            return cover

    return None


async def cache_imported_series_cover(
    session: AsyncSession,
    series: Series,
    source_path: Path,
) -> Path | None:
    """Copy discovered local artwork into Pullbox's managed cover cache."""
    try:
        source = source_path.expanduser().resolve(strict=True)
    except OSError:
        return None
    if not source.is_file() or source.suffix.lower() not in _SUPPORTED_COVER_SUFFIXES:
        return None

    covers_base = await resolve_covers_dir(session)
    covers_dir = covers_base / str(series.id)
    existing = find_cover_file(covers_dir, "series")
    if existing is not None:
        series.cover_path = f"/api/v1/series/{series.id}/cover"
        return existing

    destination = covers_dir / f"series{source.suffix.lower()}"
    temporary = covers_dir / f".{destination.name}.tmp"

    def _copy() -> None:
        covers_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, temporary)
        temporary.replace(destination)

    try:
        await asyncio.to_thread(_copy)
    except OSError:
        logger.exception(
            "imported_series_cover_cache_failed",
            series_id=series.id,
            source_path=str(source),
        )
        return None

    series.cover_path = f"/api/v1/series/{series.id}/cover"
    return destination


async def purge_series_cover_cache(
    session: AsyncSession,
    series_id: int,
    *,
    extra_base_dirs: tuple[Path, ...] = (),
) -> None:
    """Remove any cached cover directories for a series id.

    This is used when a series is newly created or deleted so a recycled DB id
    cannot inherit stale artwork from an older series record.
    """
    covers_base = await resolve_covers_dir(session)
    settings = get_settings()

    candidate_dirs = {
        covers_base / str(series_id),
        *(base / str(series_id) for base in extra_base_dirs),
    }
    if settings.covers_dir != covers_base:
        candidate_dirs.add(settings.covers_dir / str(series_id))

    for cover_dir in candidate_dirs:
        if not cover_dir.exists():
            continue
        try:
            await asyncio.to_thread(shutil.rmtree, cover_dir)
        except FileNotFoundError:
            continue
        except OSError:
            logger.exception(
                "series_cover_cache_purge_failed",
                series_id=series_id,
                path=str(cover_dir),
            )


async def cache_series_cover(session: AsyncSession, series: Series) -> Path | None:
    """Download a remote cover into local storage and update ``series.cover_path``."""
    if not series.cover_url:
        return None

    lock = _SERIES_COVER_LOCKS.setdefault(series.id, asyncio.Lock())
    async with lock:
        existing = await resolve_series_cover_file(session, series)
        if existing:
            series.cover_path = f"/api/v1/series/{series.id}/cover"
            return existing

        covers_base = await resolve_covers_dir(session)
        covers_dir = covers_base / str(series.id)

        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                headers={"User-Agent": "Pullbox/1.0"},
            ) as client:
                response = await client.get(series.cover_url)
                response.raise_for_status()
        except httpx.HTTPError:
            logger.exception("series_cover_download_failed", series_id=series.id)
            return None

        suffix = suffix_for_cover(response.headers.get("content-type"), series.cover_url)
        cover_dest = covers_dir / f"series{suffix}"
        covers_dir.mkdir(parents=True, exist_ok=True)
        cover_dest.write_bytes(response.content)
        series.cover_path = f"/api/v1/series/{series.id}/cover"
        return cover_dest

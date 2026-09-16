"""Local cover cache helpers for series and issue artwork."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx
import structlog

from pullbox.config import get_settings
from pullbox.core.exceptions import ConfigurationError
from pullbox.core.library_file_ownership import build_managed_placement_signature
from pullbox.services.cover_resolver import resolve_covers_dir

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.series import Series
    from pullbox.models.story_arc import StoryArc

logger = structlog.get_logger(__name__)
_SERIES_COVER_LOCKS: dict[int, asyncio.Lock] = {}
_STORY_ARC_COVER_LOCKS: dict[int, asyncio.Lock] = {}
_SUPPORTED_COVER_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})


@dataclass(frozen=True, slots=True)
class ImportedSeriesCoverCacheResult:
    """Cached local artwork plus exact directory ownership for import rollback."""

    path: Path
    covers_base: Path
    ownership_boundary_path: Path
    created_directory_paths: tuple[Path, ...]
    artifact_created: bool
    artifact_signature: dict[str, int | str] | None


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
) -> ImportedSeriesCoverCacheResult | None:
    """Copy discovered local artwork into Pullbox's managed cover cache."""
    try:
        source = source_path.expanduser().resolve(strict=True)
    except OSError:
        return None
    if not source.is_file() or source.suffix.lower() not in _SUPPORTED_COVER_SUFFIXES:
        return None

    covers_base = await resolve_covers_dir(session)
    covers_dir = covers_base / str(series.id)
    try:
        ownership_boundary_path = _nearest_existing_directory(covers_base)
    except OSError:
        logger.exception(
            "imported_series_cover_cache_boundary_invalid",
            series_id=series.id,
            path=str(covers_base),
        )
        return None
    existing = find_cover_file(covers_dir, "series")
    if existing is not None:
        series.cover_path = f"/api/v1/series/{series.id}/cover"
        return ImportedSeriesCoverCacheResult(
            path=existing,
            covers_base=covers_base,
            ownership_boundary_path=ownership_boundary_path,
            created_directory_paths=(),
            artifact_created=False,
            artifact_signature=None,
        )

    destination = covers_dir / f"series{source.suffix.lower()}"
    temporary = covers_dir / f".{destination.name}.tmp"

    def _copy() -> tuple[tuple[Path, ...], dict[str, int | str]]:
        created_directories = _create_cover_cache_directories(
            covers_dir,
            ownership_boundary_path,
        )
        try:
            shutil.copyfile(source, temporary)
            temporary.replace(destination)
            artifact_signature = build_managed_placement_signature(destination)
        except (ConfigurationError, OSError):
            temporary.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            _remove_owned_empty_directories(created_directories)
            raise
        return created_directories, artifact_signature

    try:
        created_directory_paths, artifact_signature = await asyncio.to_thread(_copy)
    except (ConfigurationError, OSError):
        logger.exception(
            "imported_series_cover_cache_failed",
            series_id=series.id,
            source_path=str(source),
        )
        return None

    series.cover_path = f"/api/v1/series/{series.id}/cover"
    return ImportedSeriesCoverCacheResult(
        path=destination,
        covers_base=covers_base,
        ownership_boundary_path=ownership_boundary_path,
        created_directory_paths=created_directory_paths,
        artifact_created=True,
        artifact_signature=artifact_signature,
    )


def _nearest_existing_directory(path: Path) -> Path:
    """Return the first existing directory above a possibly missing cache base."""
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    if not candidate.is_dir():
        raise NotADirectoryError(candidate)
    return candidate


def _create_cover_cache_directories(
    covers_dir: Path,
    ownership_boundary_path: Path,
) -> tuple[Path, ...]:
    """Create every missing cache segment and report exactly what was created."""
    try:
        relative = covers_dir.relative_to(ownership_boundary_path)
    except ValueError as exc:
        raise OSError("Cover cache directory is outside its ownership boundary") from exc

    created: list[Path] = []
    current = ownership_boundary_path
    try:
        for segment in relative.parts:
            current /= segment
            try:
                current.mkdir()
            except FileExistsError:
                if not current.is_dir():
                    raise
            else:
                created.append(current)
    except OSError:
        _remove_owned_empty_directories(created)
        raise
    return tuple(created)


def _remove_owned_empty_directories(paths: list[Path] | tuple[Path, ...]) -> None:
    for directory in reversed(paths):
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            continue


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


async def resolve_story_arc_cover_file(session: AsyncSession, story_arc: StoryArc) -> Path | None:
    """Return a locally cached Story Arc cover if one exists."""
    covers_base = await resolve_covers_dir(session)
    return find_cover_file(covers_base / "story_arcs" / str(story_arc.id), "story_arc")


async def cache_story_arc_cover(session: AsyncSession, story_arc: StoryArc) -> Path | None:
    """Download provider artwork into the managed Story Arc cover cache."""
    from pullbox.services.cover_url_service import story_arc_provider_cover_url

    source_url = story_arc_provider_cover_url(story_arc)
    if not source_url:
        return None
    if not story_arc.cover_url:
        story_arc.cover_url = source_url
    lock = _STORY_ARC_COVER_LOCKS.setdefault(story_arc.id, asyncio.Lock())
    async with lock:
        existing = await resolve_story_arc_cover_file(session, story_arc)
        if existing:
            story_arc.cover_path = f"/api/v1/story-arcs/{story_arc.id}/cover"
            return existing
        covers_base = await resolve_covers_dir(session)
        covers_dir = covers_base / "story_arcs" / str(story_arc.id)
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                headers={"User-Agent": "Pullbox/1.0"},
            ) as client:
                response = await client.get(source_url)
                response.raise_for_status()
        except httpx.HTTPError:
            logger.exception("story_arc_cover_download_failed", story_arc_id=story_arc.id)
            return None
        suffix = suffix_for_cover(response.headers.get("content-type"), source_url)
        cover_dest = covers_dir / f"story_arc{suffix}"
        covers_dir.mkdir(parents=True, exist_ok=True)
        cover_dest.write_bytes(response.content)
        story_arc.cover_path = f"/api/v1/story-arcs/{story_arc.id}/cover"
        return cover_dest

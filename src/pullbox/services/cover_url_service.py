"""Helpers for stable, cache-busted cover URLs."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime


def _series_cover_version_key(series: object) -> str:
    """Build a stable key that changes when the series identity/cover changes."""
    updated_at = getattr(series, "updated_at", None)
    updated_at_key = ""
    if isinstance(updated_at, datetime):
        normalized = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
        updated_at_key = normalized.astimezone(UTC).isoformat(timespec="microseconds")

    return "|".join(
        (
            str(getattr(series, "id", "") or ""),
            str(getattr(series, "comicvine_id", "") or ""),
            str(getattr(series, "title", "") or ""),
            str(getattr(series, "cover_url", "") or ""),
            updated_at_key,
        )
    )


def build_series_cover_url(series: object) -> str | None:
    """Return a versioned series-cover URL suitable for browser caches."""
    series_id = getattr(series, "id", None)
    if not series_id:
        return None

    cover_path = getattr(series, "cover_path", None)
    cover_url = getattr(series, "cover_url", None)
    if not (cover_path or cover_url):
        return None

    if cover_path and not str(cover_path).startswith("/api/v1/series/"):
        return str(cover_path)

    version = hashlib.sha256(_series_cover_version_key(series).encode("utf-8")).hexdigest()[:12]
    return f"/api/v1/series/{series_id}/cover?v={version}"


def story_arc_provider_cover_url(story_arc: object) -> str | None:
    """Return the first-class or legacy provider-snapshot cover URL for an arc."""
    cover_url = getattr(story_arc, "cover_url", None)
    if isinstance(cover_url, str) and cover_url:
        return cover_url
    diagnostics = getattr(story_arc, "diagnostics", None)
    if not isinstance(diagnostics, dict):
        return None
    catalog = diagnostics.get("provider_catalog")
    if not isinstance(catalog, dict):
        return None
    snapshot = catalog.get("snapshot")
    if not isinstance(snapshot, dict):
        return None
    legacy_url = snapshot.get("cover_url")
    return legacy_url if isinstance(legacy_url, str) and legacy_url else None


def _story_arc_cover_version_key(story_arc: object) -> str:
    """Build a stable key that changes when the arc identity or cover changes."""
    updated_at = getattr(story_arc, "updated_at", None)
    updated_at_key = ""
    if isinstance(updated_at, datetime):
        normalized = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
        updated_at_key = normalized.astimezone(UTC).isoformat(timespec="microseconds")
    return "|".join(
        (
            str(getattr(story_arc, "id", "") or ""),
            str(getattr(story_arc, "comicvine_id", "") or ""),
            str(getattr(story_arc, "name", "") or ""),
            story_arc_provider_cover_url(story_arc) or "",
            updated_at_key,
        )
    )


def build_story_arc_cover_url(story_arc: object) -> str | None:
    """Return a versioned Story Arc cover URL suitable for private browser caches."""
    story_arc_id = getattr(story_arc, "id", None)
    if not story_arc_id:
        return None
    cover_path = getattr(story_arc, "cover_path", None)
    if not (cover_path or story_arc_provider_cover_url(story_arc)):
        return None
    if cover_path and not str(cover_path).startswith("/api/v1/story-arcs/"):
        return str(cover_path)
    version = hashlib.sha256(_story_arc_cover_version_key(story_arc).encode("utf-8")).hexdigest()[
        :12
    ]
    return f"/api/v1/story-arcs/{story_arc_id}/cover?v={version}"

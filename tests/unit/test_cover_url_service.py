from __future__ import annotations

from datetime import UTC, datetime

from pullbox.services.cover_url_service import build_series_cover_url, build_story_arc_cover_url


class _SeriesStub:
    def __init__(
        self,
        *,
        id: int = 1,
        comicvine_id: int | None = None,
        title: str = "Example",
        cover_path: str | None = "/api/v1/series/1/cover",
        cover_url: str | None = "https://example.test/cover.jpg",
        updated_at: datetime | None = None,
    ) -> None:
        self.id = id
        self.comicvine_id = comicvine_id
        self.title = title
        self.cover_path = cover_path
        self.cover_url = cover_url
        self.updated_at = updated_at or datetime(2026, 5, 23, 12, 0, tzinfo=UTC)


class _StoryArcStub:
    def __init__(
        self,
        *,
        id: int = 1,
        comicvine_id: int | None = 31,
        name: str = "Remote Arc",
        cover_path: str | None = "/api/v1/story-arcs/1/cover",
        cover_url: str | None = "https://example.test/arc.jpg",
        updated_at: datetime | None = None,
    ) -> None:
        self.id = id
        self.comicvine_id = comicvine_id
        self.name = name
        self.cover_path = cover_path
        self.cover_url = cover_url
        self.updated_at = updated_at or datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def test_build_series_cover_url_adds_version_for_local_series_cover() -> None:
    series = _SeriesStub(id=42, comicvine_id=1234)

    url = build_series_cover_url(series)

    assert url is not None
    assert url.startswith("/api/v1/series/42/cover?v=")


def test_build_series_cover_url_returns_none_without_series_id() -> None:
    series = _SeriesStub(id=0, comicvine_id=1234)

    assert build_series_cover_url(series) is None


def test_build_series_cover_url_changes_when_series_identity_changes() -> None:
    first = _SeriesStub(id=42, comicvine_id=1234, title="Alpha")
    second = _SeriesStub(id=42, comicvine_id=5678, title="Beta")

    assert build_series_cover_url(first) != build_series_cover_url(second)


def test_build_series_cover_url_preserves_non_api_cover_paths() -> None:
    series = _SeriesStub(
        id=42,
        cover_path="https://cdn.example.test/series.jpg",
        cover_url=None,
    )

    assert build_series_cover_url(series) == "https://cdn.example.test/series.jpg"


def test_build_story_arc_cover_url_uses_versioned_authenticated_route() -> None:
    arc = _StoryArcStub(id=42)

    url = build_story_arc_cover_url(arc)

    assert url is not None
    assert url.startswith("/api/v1/story-arcs/42/cover?v=")


def test_build_story_arc_cover_url_returns_none_without_cover_source() -> None:
    arc = _StoryArcStub(cover_path=None, cover_url=None)

    assert build_story_arc_cover_url(arc) is None

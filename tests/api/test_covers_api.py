"""Direct route-function coverage for cover image API contracts."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fastapi.responses import FileResponse, Response

from pullbox.api.v1 import covers as covers_api
from pullbox.core.exceptions import NotFoundError
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.models.story_arc import StoryArc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest_plugins = ["conftest_security"]


def _write_cover(path: Path, payload: bytes = b"\xff\xd8\xff") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


async def _seed_series(
    session: AsyncSession,
    *,
    title: str = "Batman",
    path: Path | None = None,
    cover_url: str | None = None,
) -> Series:
    series = Series(
        title=title,
        sort_title=title.lower(),
        path=str(path) if path is not None else None,
        cover_url=cover_url,
    )
    session.add(series)
    await session.flush()
    return series


async def _seed_issue(
    session: AsyncSession,
    *,
    series_path: Path | None = None,
    issue_number: float = 1.0,
) -> Issue:
    series = await _seed_series(session, path=series_path)
    issue = Issue(series_id=series.id, issue_number=issue_number)
    session.add(issue)
    await session.flush()
    return issue


async def _patch_cover_dirs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    covers_base: Path,
    legacy_base: Path | None = None,
) -> None:
    async def _resolve_covers_dir(_session: AsyncSession) -> Path:
        return covers_base

    monkeypatch.setattr("pullbox.services.cover_resolver.resolve_covers_dir", _resolve_covers_dir)
    monkeypatch.setattr(
        covers_api,
        "get_settings",
        lambda: SimpleNamespace(covers_dir=legacy_base or covers_base),
    )


@pytest.mark.asyncio
class TestSeriesCoverRoutes:
    async def test_missing_series_raises_not_found(
        self,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        async with sec_db() as session:
            with pytest.raises(NotFoundError):
                await covers_api.get_series_cover(
                    999,
                    object(),  # type: ignore[arg-type]
                    session,
                )

    async def test_serves_resolved_local_series_cover_and_sets_api_cover_path(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cover = _write_cover(tmp_path / "series-cover.png", b"\x89PNG")

        async def _local_cover(_session: AsyncSession, _series: Series) -> Path:
            return cover

        async def _unexpected_cache(_session: AsyncSession, _series: Series) -> None:
            pytest.fail("remote cache should not run when a local cover exists")

        monkeypatch.setattr(covers_api, "resolve_series_cover_file", _local_cover)
        monkeypatch.setattr(covers_api, "cache_series_cover", _unexpected_cache)

        async with sec_db() as session:
            series = await _seed_series(session)

            response = await covers_api.get_series_cover(
                series.id,
                object(),  # type: ignore[arg-type]
                session,
            )

        assert isinstance(response, FileResponse)
        assert response.media_type == "image/png"
        assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
        assert series.cover_path == f"/api/v1/series/{series.id}/cover"

    async def test_fetches_remote_series_cover_when_no_local_cover_exists(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cached = _write_cover(tmp_path / "cached-cover.webp", b"RIFF")

        async def _no_local_cover(_session: AsyncSession, _series: Series) -> None:
            return None

        async def _cached_cover(_session: AsyncSession, _series: Series) -> Path:
            return cached

        monkeypatch.setattr(covers_api, "resolve_series_cover_file", _no_local_cover)
        monkeypatch.setattr(covers_api, "cache_series_cover", _cached_cover)

        async with sec_db() as session:
            series = await _seed_series(session, cover_url="https://example.com/cover.webp")

            response = await covers_api.get_series_cover(
                series.id,
                object(),  # type: ignore[arg-type]
                session,
            )

        assert isinstance(response, FileResponse)
        assert response.media_type == "image/webp"
        assert response.headers["cache-control"] == "private, max-age=31536000, immutable"

    async def test_returns_404_when_series_has_no_cover_source(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _no_local_cover(_session: AsyncSession, _series: Series) -> None:
            return None

        monkeypatch.setattr(covers_api, "resolve_series_cover_file", _no_local_cover)

        async with sec_db() as session:
            series = await _seed_series(session)

            response = await covers_api.get_series_cover(
                series.id,
                object(),  # type: ignore[arg-type]
                session,
            )

        assert isinstance(response, Response)
        assert response.status_code == 404


@pytest.mark.asyncio
class TestStoryArcCoverRoutes:
    async def test_fetches_and_serves_remote_story_arc_cover(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cached = _write_cover(tmp_path / "story-arc-cover.webp", b"RIFF")

        async def _no_local_cover(_session: AsyncSession, _arc: StoryArc) -> None:
            return None

        async def _cached_cover(_session: AsyncSession, arc: StoryArc) -> Path:
            arc.cover_path = f"/api/v1/story-arcs/{arc.id}/cover"
            return cached

        monkeypatch.setattr(covers_api, "resolve_story_arc_cover_file", _no_local_cover)
        monkeypatch.setattr(covers_api, "cache_story_arc_cover", _cached_cover)

        async with sec_db() as session:
            arc = StoryArc(
                name="Remote Arc",
                normalized_name="remote arc",
                cover_url="https://example.test/arc.webp",
            )
            session.add(arc)
            await session.flush()

            response = await covers_api.get_story_arc_cover(
                arc.id,
                object(),  # type: ignore[arg-type]
                session,
            )

        assert isinstance(response, FileResponse)
        assert response.media_type == "image/webp"
        assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
        assert arc.cover_path == f"/api/v1/story-arcs/{arc.id}/cover"

    async def test_existing_provider_snapshot_supplies_legacy_story_arc_cover(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cached = _write_cover(tmp_path / "legacy-arc-cover.jpg")

        async def _no_local_cover(_session: AsyncSession, _arc: StoryArc) -> None:
            return None

        async def _cached_cover(_session: AsyncSession, arc: StoryArc) -> Path:
            assert arc.cover_url == "https://example.test/legacy-arc.jpg"
            return cached

        monkeypatch.setattr(covers_api, "resolve_story_arc_cover_file", _no_local_cover)
        monkeypatch.setattr(covers_api, "cache_story_arc_cover", _cached_cover)

        async with sec_db() as session:
            arc = StoryArc(
                name="Legacy Provider Arc",
                normalized_name="legacy provider arc",
                diagnostics={
                    "provider_catalog": {
                        "snapshot": {"cover_url": "https://example.test/legacy-arc.jpg"}
                    }
                },
            )
            session.add(arc)
            await session.flush()

            response = await covers_api.get_story_arc_cover(
                arc.id,
                object(),  # type: ignore[arg-type]
                session,
            )

        assert isinstance(response, FileResponse)
        assert arc.cover_url == "https://example.test/legacy-arc.jpg"


@pytest.mark.asyncio
class TestIssueCoverRoutes:
    async def test_missing_issue_raises_not_found(
        self,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        async with sec_db() as session:
            with pytest.raises(NotFoundError):
                await covers_api.get_issue_cover(
                    999,
                    object(),  # type: ignore[arg-type]
                    session,
                )

    async def test_serves_issue_cover_from_series_folder_by_issue_number(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        series_path = tmp_path / "series"
        _write_cover(series_path / "issue_001.jpg")

        async with sec_db() as session:
            issue = await _seed_issue(session, series_path=series_path)

            response = await covers_api.get_issue_cover(
                issue.id,
                object(),  # type: ignore[arg-type]
                session,
            )

        assert isinstance(response, FileResponse)
        assert response.media_type == "image/jpeg"
        assert response.headers["cache-control"] == "private, no-cache, max-age=0, must-revalidate"

    async def test_serves_issue_cover_from_configured_covers_directory(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        covers_base = tmp_path / "covers"
        await _patch_cover_dirs(monkeypatch, covers_base=covers_base)

        async with sec_db() as session:
            issue = await _seed_issue(session)
            _write_cover(covers_base / str(issue.series_id) / "issue_001.png", b"\x89PNG")

            response = await covers_api.get_issue_cover(
                issue.id,
                object(),  # type: ignore[arg-type]
                session,
            )

        assert isinstance(response, FileResponse)
        assert response.media_type == "image/png"

    async def test_serves_legacy_issue_cover_by_database_id(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        covers_base = tmp_path / "covers"
        legacy_base = tmp_path / "legacy-covers"
        await _patch_cover_dirs(monkeypatch, covers_base=covers_base, legacy_base=legacy_base)

        async with sec_db() as session:
            issue = await _seed_issue(session)
            _write_cover(legacy_base / str(issue.series_id) / f"issue_{issue.id}.jpg")

            response = await covers_api.get_issue_cover(
                issue.id,
                object(),  # type: ignore[arg-type]
                session,
            )

        assert isinstance(response, FileResponse)
        assert response.media_type == "image/jpeg"

    async def test_falls_back_to_series_folder_cover(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        covers_base = tmp_path / "covers"
        await _patch_cover_dirs(monkeypatch, covers_base=covers_base)
        series_path = tmp_path / "series"
        _write_cover(series_path / "cover.jpg")

        async with sec_db() as session:
            issue = await _seed_issue(session, series_path=series_path)

            response = await covers_api.get_issue_cover(
                issue.id,
                object(),  # type: ignore[arg-type]
                session,
            )

        assert isinstance(response, FileResponse)
        assert response.media_type == "image/jpeg"

    async def test_falls_back_to_configured_and_legacy_series_covers(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        covers_base = tmp_path / "covers"
        legacy_base = tmp_path / "legacy-covers"
        await _patch_cover_dirs(monkeypatch, covers_base=covers_base, legacy_base=legacy_base)

        async with sec_db() as session:
            configured_issue = await _seed_issue(session)
            _write_cover(covers_base / str(configured_issue.series_id) / "series.webp", b"RIFF")

            configured_response = await covers_api.get_issue_cover(
                configured_issue.id,
                object(),  # type: ignore[arg-type]
                session,
            )

            legacy_issue = await _seed_issue(session, issue_number=2.0)
            _write_cover(legacy_base / str(legacy_issue.series_id) / "series.jpg")

            legacy_response = await covers_api.get_issue_cover(
                legacy_issue.id,
                object(),  # type: ignore[arg-type]
                session,
            )

        assert isinstance(configured_response, FileResponse)
        assert configured_response.media_type == "image/webp"
        assert isinstance(legacy_response, FileResponse)
        assert legacy_response.media_type == "image/jpeg"

    async def test_returns_404_when_no_issue_or_series_cover_exists(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _patch_cover_dirs(monkeypatch, covers_base=tmp_path / "covers")

        async with sec_db() as session:
            issue = await _seed_issue(session)

            response = await covers_api.get_issue_cover(
                issue.id,
                object(),  # type: ignore[arg-type]
                session,
            )

        assert isinstance(response, Response)
        assert response.status_code == 404

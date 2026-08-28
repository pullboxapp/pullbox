"""Tests for local cover cache service helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pullbox.services import cover_cache_service

if TYPE_CHECKING:
    from pathlib import Path


class _FakeResponse:
    def __init__(self, *, content_type: str | None = None, content: bytes = b"image") -> None:
        self.headers = {}
        if content_type is not None:
            self.headers["content-type"] = content_type
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse | Exception, **kwargs: object) -> None:
        self.response = response
        self.kwargs = kwargs

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get(self, url: str) -> _FakeResponse:
        if isinstance(self.response, Exception):
            raise self.response
        self.requested_url = url
        return self.response


@pytest.mark.parametrize(
    ("content_type", "url", "expected"),
    [
        ("image/png", "https://example.test/cover.jpg", ".png"),
        ("image/webp; charset=binary", "https://example.test/cover.jpg", ".webp"),
        (None, "https://example.test/cover.png", ".png"),
        (None, "https://example.test/cover.webp", ".webp"),
        (None, "https://example.test/cover.jpeg", ".jpeg"),
        ("application/octet-stream", "https://example.test/cover", ".jpg"),
    ],
)
def test_suffix_for_cover_uses_content_type_then_url(
    content_type: str | None,
    url: str,
    expected: str,
) -> None:
    assert cover_cache_service.suffix_for_cover(content_type, url) == expected


def test_find_cover_file_returns_first_supported_existing_extension(tmp_path: Path) -> None:
    (tmp_path / "series.png").write_bytes(b"png")
    (tmp_path / "series.webp").write_bytes(b"webp")

    assert cover_cache_service.find_cover_file(tmp_path, "series") == tmp_path / "series.png"
    assert cover_cache_service.find_cover_file(tmp_path, "missing") is None


def test_find_imported_series_cover_prefers_mylar_cover_then_thumbnail(tmp_path: Path) -> None:
    thumbnail = tmp_path / "folder.jpg"
    thumbnail.write_bytes(b"thumbnail")

    assert cover_cache_service.find_imported_series_cover(tmp_path) == thumbnail

    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"full-size")

    assert cover_cache_service.find_imported_series_cover(tmp_path) == cover


@pytest.mark.asyncio
async def test_resolve_series_cover_file_prefers_series_folder_cover(tmp_path: Path) -> None:
    series_dir = tmp_path / "series"
    series_dir.mkdir()
    (series_dir / "cover.jpg").write_bytes(b"cover")
    series = SimpleNamespace(id=42, path=str(series_dir))

    resolved = await cover_cache_service.resolve_series_cover_file(AsyncMock(), series)

    assert resolved == series_dir / "cover.jpg"


@pytest.mark.asyncio
async def test_resolve_series_cover_file_uses_active_cover_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_base = tmp_path / "active"
    active_series_dir = active_base / "42"
    active_series_dir.mkdir(parents=True)
    (active_series_dir / "series.webp").write_bytes(b"cover")
    series = SimpleNamespace(id=42, path=None)

    monkeypatch.setattr(
        cover_cache_service,
        "resolve_covers_dir",
        AsyncMock(return_value=active_base),
    )
    monkeypatch.setattr(
        cover_cache_service,
        "get_settings",
        lambda: SimpleNamespace(covers_dir=active_base),
    )

    resolved = await cover_cache_service.resolve_series_cover_file(AsyncMock(), series)

    assert resolved == active_series_dir / "series.webp"


@pytest.mark.asyncio
async def test_resolve_series_cover_file_checks_legacy_cover_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_base = tmp_path / "active"
    legacy_base = tmp_path / "legacy"
    legacy_series_dir = legacy_base / "42"
    legacy_series_dir.mkdir(parents=True)
    (legacy_series_dir / "series.jpeg").write_bytes(b"cover")
    series = SimpleNamespace(id=42, path=None)

    monkeypatch.setattr(
        cover_cache_service,
        "resolve_covers_dir",
        AsyncMock(return_value=active_base),
    )
    monkeypatch.setattr(
        cover_cache_service,
        "get_settings",
        lambda: SimpleNamespace(covers_dir=legacy_base),
    )

    resolved = await cover_cache_service.resolve_series_cover_file(AsyncMock(), series)

    assert resolved == legacy_series_dir / "series.jpeg"


@pytest.mark.asyncio
async def test_resolve_series_cover_file_returns_none_when_no_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_base = tmp_path / "active"
    legacy_base = tmp_path / "legacy"
    series = SimpleNamespace(id=42, path=None)

    monkeypatch.setattr(
        cover_cache_service,
        "resolve_covers_dir",
        AsyncMock(return_value=active_base),
    )
    monkeypatch.setattr(
        cover_cache_service,
        "get_settings",
        lambda: SimpleNamespace(covers_dir=legacy_base),
    )

    resolved = await cover_cache_service.resolve_series_cover_file(AsyncMock(), series)

    assert resolved is None


@pytest.mark.asyncio
async def test_cache_imported_series_cover_copies_source_without_modifying_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mylar" / "cover.jpg"
    source.parent.mkdir()
    source.write_bytes(b"mylar-cover")
    covers_base = tmp_path / "covers"
    series = SimpleNamespace(id=42, cover_path=None)
    monkeypatch.setattr(
        cover_cache_service,
        "resolve_covers_dir",
        AsyncMock(return_value=covers_base),
    )

    resolved = await cover_cache_service.cache_imported_series_cover(
        AsyncMock(),
        series,
        source,
    )

    assert resolved == covers_base / "42" / "series.jpg"
    assert resolved.read_bytes() == b"mylar-cover"
    assert source.read_bytes() == b"mylar-cover"
    assert series.cover_path == "/api/v1/series/42/cover"


@pytest.mark.asyncio
async def test_cache_imported_series_cover_ignores_missing_source(
    tmp_path: Path,
) -> None:
    series = SimpleNamespace(id=42, cover_path=None)

    resolved = await cover_cache_service.cache_imported_series_cover(
        AsyncMock(),
        series,
        tmp_path / "missing.jpg",
    )

    assert resolved is None
    assert series.cover_path is None


@pytest.mark.asyncio
async def test_purge_series_cover_cache_skips_missing_dirs_and_ignores_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_base = tmp_path / "active"
    extra_base = tmp_path / "extra"
    existing_dir = active_base / "42"
    existing_dir.mkdir(parents=True)

    monkeypatch.setattr(
        cover_cache_service,
        "resolve_covers_dir",
        AsyncMock(return_value=active_base),
    )
    monkeypatch.setattr(
        cover_cache_service,
        "get_settings",
        lambda: SimpleNamespace(covers_dir=active_base),
    )
    monkeypatch.setattr(
        cover_cache_service.asyncio,
        "to_thread",
        AsyncMock(side_effect=FileNotFoundError),
    )

    await cover_cache_service.purge_series_cover_cache(
        AsyncMock(),
        42,
        extra_base_dirs=(extra_base,),
    )

    cover_cache_service.asyncio.to_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_purge_series_cover_cache_logs_os_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_base = tmp_path / "active"
    existing_dir = active_base / "42"
    existing_dir.mkdir(parents=True)
    logger = SimpleNamespace(exception=MagicMock())

    monkeypatch.setattr(
        cover_cache_service,
        "resolve_covers_dir",
        AsyncMock(return_value=active_base),
    )
    monkeypatch.setattr(
        cover_cache_service,
        "get_settings",
        lambda: SimpleNamespace(covers_dir=active_base),
    )
    monkeypatch.setattr(
        cover_cache_service.asyncio,
        "to_thread",
        AsyncMock(side_effect=OSError("permission denied")),
    )
    monkeypatch.setattr(cover_cache_service, "logger", logger)

    await cover_cache_service.purge_series_cover_cache(AsyncMock(), 42)

    cover_cache_service.asyncio.to_thread.assert_awaited_once()
    logger.exception.assert_called_once()


@pytest.mark.asyncio
async def test_purge_series_cover_cache_includes_legacy_cache_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_base = tmp_path / "active"
    legacy_base = tmp_path / "legacy"
    for base in (active_base, legacy_base):
        cover_dir = base / "42"
        cover_dir.mkdir(parents=True)
        (cover_dir / "series.jpg").write_bytes(b"cover")

    monkeypatch.setattr(
        cover_cache_service,
        "resolve_covers_dir",
        AsyncMock(return_value=active_base),
    )
    monkeypatch.setattr(
        cover_cache_service,
        "get_settings",
        lambda: SimpleNamespace(covers_dir=legacy_base),
    )

    await cover_cache_service.purge_series_cover_cache(AsyncMock(), 42)

    assert not (active_base / "42").exists()
    assert not (legacy_base / "42").exists()


@pytest.mark.asyncio
async def test_cache_series_cover_returns_none_without_remote_url() -> None:
    series = SimpleNamespace(id=42, cover_url=None, cover_path=None)

    assert await cover_cache_service.cache_series_cover(AsyncMock(), series) is None
    assert series.cover_path is None


@pytest.mark.asyncio
async def test_cache_series_cover_reuses_existing_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = tmp_path / "series.jpg"
    existing.write_bytes(b"cover")
    series = SimpleNamespace(id=42, cover_url="https://example.test/cover.jpg", cover_path=None)

    monkeypatch.setattr(
        cover_cache_service,
        "resolve_series_cover_file",
        AsyncMock(return_value=existing),
    )

    resolved = await cover_cache_service.cache_series_cover(AsyncMock(), series)

    assert resolved == existing
    assert series.cover_path == "/api/v1/series/42/cover"


@pytest.mark.asyncio
async def test_cache_series_cover_returns_none_on_download_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series = SimpleNamespace(id=42, cover_url="https://example.test/cover.jpg", cover_path=None)

    monkeypatch.setattr(
        cover_cache_service,
        "resolve_series_cover_file",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        cover_cache_service,
        "resolve_covers_dir",
        AsyncMock(return_value=tmp_path / "covers"),
    )
    monkeypatch.setattr(
        cover_cache_service.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(httpx.ConnectError("boom"), **kwargs),
    )

    resolved = await cover_cache_service.cache_series_cover(AsyncMock(), series)

    assert resolved is None
    assert series.cover_path is None


@pytest.mark.asyncio
async def test_cache_series_cover_downloads_and_persists_local_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    covers_base = tmp_path / "covers"
    series = SimpleNamespace(id=42, cover_url="https://example.test/cover.png", cover_path=None)

    monkeypatch.setattr(
        cover_cache_service,
        "resolve_series_cover_file",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        cover_cache_service,
        "resolve_covers_dir",
        AsyncMock(return_value=covers_base),
    )
    monkeypatch.setattr(
        cover_cache_service.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(
            _FakeResponse(content_type="image/webp", content=b"cached-cover"),
            **kwargs,
        ),
    )

    resolved = await cover_cache_service.cache_series_cover(AsyncMock(), series)

    assert resolved == covers_base / "42" / "series.webp"
    assert resolved.read_bytes() == b"cached-cover"
    assert series.cover_path == "/api/v1/series/42/cover"

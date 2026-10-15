"""Focused API coverage for the shared filesystem browser."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.api.v1 import filesystem as filesystem_api
from pullbox.models import Base
from pullbox.models.config import SystemConfig
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-filesystem")


@pytest.fixture
async def _db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def _session_token(
    _db_factory: async_sessionmaker[AsyncSession], seeded_user_password_hash: str
) -> str:
    async with _db_factory() as session:
        from pullbox.models.user import User

        user = User(
            username="filesystemuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return AuthService.create_session_token(user.id, user.session_version)


@pytest.fixture
async def client(_db_factory: async_sessionmaker[AsyncSession], _session_token: str):
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()

    async def _override_db():
        async with _db_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_dep] = _override_db
    reset_setup_cache()

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        cookies={SESSION_COOKIE_NAME: _session_token},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


@pytest.mark.asyncio
async def test_browse_uses_configured_allowed_extensions_by_default(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """When no extensions override is provided, browse uses the configured import allowlist."""
    (tmp_path / "comic.cbz").write_text("zip")
    (tmp_path / "guide.pdf").write_text("pdf")
    (tmp_path / "notes.txt").write_text("text")
    (tmp_path / "subdir").mkdir()

    async with _db_factory() as session:
        session.add(
            SystemConfig(
                key="allowed_import_extensions",
                value=".pdf",
                value_type="string",
            )
        )
        await session.commit()

    resp = await client.get("/api/v1/filesystem/browse", params={"path": str(tmp_path)})

    assert resp.status_code == 200
    data = resp.json()
    assert [entry["name"] for entry in data["files"]] == ["guide.pdf"]
    assert [entry["name"] for entry in data["directories"]] == ["subdir"]


@pytest.mark.asyncio
async def test_browse_explicit_extensions_override_configured_default(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """A browser-specific extensions override wins over the configured default list."""
    (tmp_path / "comic.cbz").write_text("zip")
    (tmp_path / "guide.pdf").write_text("pdf")

    async with _db_factory() as session:
        session.add(
            SystemConfig(
                key="allowed_import_extensions",
                value=".pdf",
                value_type="string",
            )
        )
        await session.commit()

    resp = await client.get(
        "/api/v1/filesystem/browse",
        params={"path": str(tmp_path), "extensions": "cbz"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert [entry["name"] for entry in data["files"]] == ["comic.cbz"]


@pytest.mark.asyncio
async def test_browse_clamps_navigation_to_allowed_roots(
    client: AsyncClient,
    tmp_path,
) -> None:
    """Constrained roots should pin the browser inside the allowed subtree."""
    allowed_root = tmp_path / "library"
    disallowed_root = tmp_path / "outside"
    allowed_root.mkdir()
    disallowed_root.mkdir()
    (allowed_root / "batman.cbz").write_text("zip")
    (disallowed_root / "other.cbz").write_text("zip")

    resp = await client.get(
        "/api/v1/filesystem/browse",
        params={"path": str(disallowed_root), "roots": str(allowed_root)},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["path"] == str(allowed_root)
    assert data["parent"] is None
    assert [entry["path"] for entry in data["quick_links"]] == [str(allowed_root)]


@pytest.mark.asyncio
async def test_browse_with_only_invalid_allowed_roots_fails_closed(
    client: AsyncClient,
    tmp_path,
) -> None:
    """Supplying only invalid constrained roots should not expand to unrestricted browsing."""
    allowed_root = tmp_path / "missing-library-root"
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    (outside_root / "other.cbz").write_text("zip")

    resp = await client.get(
        "/api/v1/filesystem/browse",
        params={"path": str(outside_root), "roots": str(allowed_root)},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["path"] == "/"
    assert data["parent"] is None
    assert data["directories"] == []
    assert data["files"] == []
    assert data["quick_links"] == []


@pytest.mark.asyncio
async def test_directories_with_only_invalid_allowed_roots_fails_closed(
    client: AsyncClient,
    tmp_path,
) -> None:
    """Directory-only constrained browsing should also fail closed when all roots are invalid."""
    allowed_root = tmp_path / "missing-library-root"
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    (outside_root / "subdir").mkdir()

    resp = await client.get(
        "/api/v1/filesystem/directories",
        params={"path": str(outside_root), "roots": str(allowed_root)},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["path"] == "/"
    assert data["parent"] is None
    assert data["directories"] == []
    assert data["quick_links"] == []


def test_allowed_root_helpers_handle_unconstrained_duplicates_and_blanks(tmp_path: Path) -> None:
    """Allowed-root helpers should be permissive only when no root constraint exists."""
    child = tmp_path / "library" / "Series"
    child.mkdir(parents=True)

    assert filesystem_api._is_within_allowed_roots(child, []) is True
    assert filesystem_api._is_within_allowed_roots(child, [tmp_path]) is True
    assert filesystem_api._is_within_allowed_roots(tmp_path / "outside", [child]) is False

    roots = filesystem_api._parse_allowed_roots(f" , {tmp_path}, {tmp_path}, /etc, ")
    assert roots == [tmp_path.resolve()]


def test_validate_browsable_path_defensively_blocks_unresolved_parent_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defensive ``..`` guard should fail closed if a path provider preserves traversal."""
    real_path = Path

    class _ResolvedPath:
        parts = ("tmp", "..", "secret")

        def __str__(self) -> str:
            return "/tmp/../secret"

    class _PathFactory:
        def __init__(self, value: str) -> None:
            self.value = value

        def resolve(self) -> Path | _ResolvedPath:
            if self.value == "/":
                return real_path("/").resolve()
            return _ResolvedPath()

    monkeypatch.setattr(filesystem_api, "Path", _PathFactory)

    assert filesystem_api._validate_browsable_path("/tmp/../secret") == real_path("/").resolve()


def test_darwin_quick_links_ignore_unreadable_volumes(tmp_path: Path) -> None:
    """Unreadable macOS volume listings should not break default quick links."""
    original_is_dir = Path.is_dir

    def _fake_is_dir(self: Path) -> bool:
        if str(self) == "/Volumes":
            return True
        return original_is_dir(self)

    def _fake_iterdir(self: Path):
        if str(self) == "/Volumes":
            raise PermissionError
        return iter(())

    with (
        patch("pullbox.api.v1.filesystem.platform.system", return_value="Darwin"),
        patch("pullbox.api.v1.filesystem.Path.home", return_value=tmp_path),
        patch.object(Path, "is_dir", _fake_is_dir),
        patch.object(Path, "iterdir", _fake_iterdir),
    ):
        links = filesystem_api._discover_quick_links()

    assert [link.label for link in links] == ["Home", "/", "Volumes"]


def test_linux_quick_links_ignore_unreadable_mount_children(tmp_path: Path) -> None:
    """Unreadable Linux mount child listings should still expose the mount roots."""
    original_is_dir = Path.is_dir

    def _fake_is_dir(self: Path) -> bool:
        if str(self) in {"/mnt", "/media"}:
            return True
        return original_is_dir(self)

    def _fake_iterdir(self: Path):
        if str(self) in {"/mnt", "/media"}:
            raise PermissionError
        return iter(())

    with (
        patch("pullbox.api.v1.filesystem.platform.system", return_value="Linux"),
        patch("pullbox.api.v1.filesystem.Path.home", return_value=tmp_path),
        patch.object(Path, "is_dir", _fake_is_dir),
        patch.object(Path, "iterdir", _fake_iterdir),
    ):
        links = filesystem_api._discover_quick_links()

    assert [link.label for link in links] == ["Home", "/", "Mounts", "Media"]


@pytest.mark.asyncio
async def test_browse_files_direct_handles_hidden_dirs_and_stat_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct browse helper should skip hidden entries and report size 0 on stat failure."""

    class _Entry:
        def __init__(
            self,
            name: str,
            *,
            is_dir: bool = False,
            is_file: bool = False,
            stat_raises: bool = False,
        ) -> None:
            self.name = name
            self.suffix = Path(name).suffix
            self._is_dir = is_dir
            self._is_file = is_file
            self._stat_raises = stat_raises

        def __lt__(self, other: object) -> bool:
            return isinstance(other, _Entry) and self.name < other.name

        def __str__(self) -> str:
            return f"/virtual/{self.name}"

        def is_dir(self) -> bool:
            return self._is_dir

        def is_file(self) -> bool:
            return self._is_file

        def stat(self) -> SimpleNamespace:
            if self._stat_raises:
                raise OSError
            return SimpleNamespace(st_size=123)

    entries = [
        _Entry(".hidden", is_dir=True),
        _Entry("Alpha", is_dir=True),
        _Entry("broken.cbz", is_file=True, stat_raises=True),
        _Entry("notes.txt", is_file=True),
    ]
    monkeypatch.setattr(filesystem_api, "_validate_browsable_path", lambda *_: Path("/virtual"))
    monkeypatch.setattr(filesystem_api, "_build_quick_links", lambda *_: [])
    monkeypatch.setattr(Path, "iterdir", lambda *_: iter(entries))

    listing = await filesystem_api.browse_files(
        object(),
        object(),
        path="/unused",
        roots=None,
        extensions="cbz",
    )

    assert [entry.name for entry in listing.directories] == ["Alpha"]
    assert [(entry.name, entry.size) for entry in listing.files] == [("broken.cbz", 0)]
    assert listing.quick_links == []


@pytest.mark.asyncio
async def test_browse_files_direct_handles_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PermissionError during file browsing should return an empty listing."""
    monkeypatch.setattr(filesystem_api, "_validate_browsable_path", lambda *_: Path("/virtual"))
    monkeypatch.setattr(filesystem_api, "_build_quick_links", lambda *_: [])

    def _raise_permission_error(*_args: object, **_kwargs: object):
        raise PermissionError

    monkeypatch.setattr(Path, "iterdir", _raise_permission_error)

    listing = await filesystem_api.browse_files(
        object(),
        object(),
        path="/unused",
        roots=None,
        extensions="cbz",
    )

    assert listing.directories == []
    assert listing.files == []

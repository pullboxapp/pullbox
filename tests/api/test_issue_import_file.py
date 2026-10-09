"""Tests for the manual file import endpoint POST /api/v1/issues/{id}/import-file.

Verifies:
- Happy path: valid file → LibraryFile created, issue marked OWNED
- move_to_library=true (default) → file moved to comics dir
- move_to_library=false → file stays in original location
- 404 when issue doesn't exist
- 400 when file path doesn't exist on disk
- 400 when file extension is not a supported comic format
- 409 when issue already has a LibraryFile (already OWNED)
- 400 when file_path is empty string
- 400 when file_path is a directory, not a file
- 400 when file_path is relative (not absolute)
- Issue status transitions from various states to OWNED
- Response contains correct LibraryFile data
- Series relationship is preserved after import
- Concurrent import of same issue returns 409

Run:
    pytest tests/api/test_issue_import_file.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models.config import SystemConfig
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.schemas.issue import ManualFileImportProgressResponse
from tests.sqlite_schema import create_test_schema

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-import-file")


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await create_test_schema(conn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def _api_key_header(
    _db_factory: async_sessionmaker[AsyncSession],
    seeded_user_password_hash: str,
) -> str:
    """Create a test user + API key, return the raw key string."""
    raw_key = "pb_k1_" + "a" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        from pullbox.models.user import APIKey, User

        user = User(
            username="importfileuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="import-file-test"))
        await session.commit()
    return raw_key


@pytest.fixture
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
    _api_key_header: str,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated via API key (bypasses CSRF)."""
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
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
        headers={"X-Api-Key": _api_key_header},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


# ── Helpers ────────────────────────────────────────────────────────────


async def _create_series_and_issue(
    factory: async_sessionmaker[AsyncSession],
    *,
    series_title: str = "Batman",
    year_start: int = 2016,
    issue_number: float = 1.0,
    issue_status: IssueStatus = IssueStatus.WANTED,
) -> tuple[int, int, int]:
    """Seed a library root + series + issue, return (root_id, series_id, issue_id)."""
    async with factory() as session:
        root = LibraryRoot(
            name="Test Comics",
            path="/tmp/test-comics",
        )
        session.add(root)
        await session.flush()

        series = Series(
            comicvine_id=99900,
            title=series_title,
            sort_title=series_title.lower(),
            year_start=year_start,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        session.add(series)
        await session.flush()

        issue = Issue(
            series_id=series.id,
            comicvine_id=50001,
            issue_number=issue_number,
            title=f"Issue #{int(issue_number)}",
            status=issue_status,
        )
        session.add(issue)
        await session.commit()
        return root.id, series.id, issue.id


async def _create_owned_issue(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int]:
    """Seed an issue that already has a LibraryFile (OWNED status)."""
    async with factory() as session:
        root = LibraryRoot(
            name="Test Comics",
            path="/tmp/test-comics",
        )
        session.add(root)
        await session.flush()

        series = Series(
            comicvine_id=88800,
            title="Superman",
            sort_title="superman",
            year_start=2018,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        session.add(series)
        await session.flush()

        issue = Issue(
            series_id=series.id,
            comicvine_id=60001,
            issue_number=1.0,
            title="Issue #1",
            status=IssueStatus.OWNED,
        )
        session.add(issue)
        await session.flush()

        from datetime import datetime

        lf = LibraryFile(
            file_path="/tmp/test-comics/Superman (2018)/Superman 001.cbz",
            file_name="Superman 001.cbz",
            file_size=50_000_000,
            file_format="cbz",
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=issue.id,
            library_root_id=root.id,
        )
        session.add(lf)
        await session.commit()
        return root.id, series.id, issue.id


# ── Happy Path Tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_file_success_move_to_library(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Valid file with move_to_library=true creates LibraryFile and marks OWNED."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory)

    # Create a test comic file
    comic_file = tmp_path / "Batman 001 (2016).cbz"
    comic_file.write_bytes(b"PK" + b"\x00" * 1000)

    mock_lf = AsyncMock()
    mock_lf.id = 42
    mock_lf.file_path = "/tmp/test-comics/Batman (2016)/Batman 001 (2016).cbz"
    mock_lf.file_name = "Batman 001 (2016).cbz"
    mock_lf.file_size = 1002
    mock_lf.file_format = "cbz"
    mock_lf.match_confidence = MatchConfidence.MANUAL

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        return_value=mock_lf,
    ) as mock_register:
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={
                "file_path": str(comic_file),
                "move_to_library": True,
            },
        )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["issue_id"] == issue_id
    assert data["library_file_id"] == 42
    assert data["file_name"] == "Batman 001 (2016).cbz"
    assert data["match_confidence"] == "manual"

    # Verify register_library_file was called correctly
    mock_register.assert_called_once()
    call_kwargs = mock_register.call_args
    assert call_kwargs[1]["move_to_library"] is True


@pytest.mark.asyncio
async def test_import_file_leave_in_place(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Deprecated move_to_library=false requests are rejected."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory)

    comic_file = tmp_path / "Batman 001.cbz"
    comic_file.write_bytes(b"PK" + b"\x00" * 500)

    mock_lf = AsyncMock()
    mock_lf.id = 43
    mock_lf.file_path = str(comic_file)
    mock_lf.file_name = "Batman 001.cbz"
    mock_lf.file_size = 502
    mock_lf.file_format = "cbz"
    mock_lf.match_confidence = MatchConfidence.MANUAL

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        return_value=mock_lf,
    ) as mock_register:
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={
                "file_path": str(comic_file),
                "move_to_library": False,
            },
        )

    assert resp.status_code == 422, resp.text
    assert "no longer supported" in resp.json()["detail"].lower()
    mock_register.assert_not_called()


# ── Error Cases ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_file_issue_not_found(
    client: AsyncClient,
) -> None:
    """404 when issue_id doesn't exist."""
    resp = await client.post(
        "/api/v1/issues/99999/import-file",
        json={"file_path": "/some/file.cbz"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_import_file_path_not_found(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """400 when the file path doesn't exist on disk."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory)

    resp = await client.post(
        f"/api/v1/issues/{issue_id}/import-file",
        json={"file_path": "/nonexistent/path/to/comic.cbz"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"].lower()
    assert "not found" in detail or "not exist" in detail


@pytest.mark.asyncio
async def test_import_file_unsupported_format(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """400 when file extension is not a supported comic format."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory)

    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("not a comic")

    resp = await client.post(
        f"/api/v1/issues/{issue_id}/import-file",
        json={"file_path": str(txt_file)},
    )
    assert resp.status_code == 400
    assert "format" in resp.json()["detail"].lower() or "supported" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_import_file_honors_configured_allowed_extensions(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Configured allowed_import_extensions become the manual import allowlist."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory)

    async with _db_factory() as session:
        session.add(
            SystemConfig(
                key="allowed_import_extensions",
                value=".pdf",
                value_type="string",
            )
        )
        await session.commit()

    cbz_file = tmp_path / "Batman 001.cbz"
    cbz_file.write_bytes(b"PK" + b"\x00" * 100)

    resp = await client.post(
        f"/api/v1/issues/{issue_id}/import-file",
        json={"file_path": str(cbz_file)},
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"].lower()
    assert "unsupported format '.cbz'" in detail
    assert ".pdf" in detail


@pytest.mark.asyncio
async def test_import_file_replaces_already_owned_issue(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Owned issues can be replaced by explicit manual import."""
    _root_id, _series_id, issue_id = await _create_owned_issue(_db_factory)

    comic_file = tmp_path / "Superman 001.cbz"
    comic_file.write_bytes(b"PK" + b"\x00" * 500)

    mock_lf = AsyncMock()
    mock_lf.id = 84
    mock_lf.file_path = "/tmp/test-comics/Superman (2018)/Superman 001.cbz"
    mock_lf.file_name = "Superman 001.cbz"
    mock_lf.file_size = 502
    mock_lf.file_format = "cbz"
    mock_lf.match_confidence = MatchConfidence.MANUAL

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        return_value=mock_lf,
    ) as mock_register:
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={"file_path": str(comic_file)},
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["library_file_id"] == 84
    mock_register.assert_called_once()
    assert mock_register.call_args.kwargs["replace_existing_library_file"] is True


@pytest.mark.asyncio
async def test_import_file_empty_path(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """400 when file_path is empty string."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory)

    resp = await client.post(
        f"/api/v1/issues/{issue_id}/import-file",
        json={"file_path": ""},
    )
    assert resp.status_code == 400 or resp.status_code == 422


@pytest.mark.asyncio
async def test_import_file_path_is_directory(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """400 when file_path points to a directory, not a file."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory)

    resp = await client.post(
        f"/api/v1/issues/{issue_id}/import-file",
        json={"file_path": str(tmp_path)},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_import_file_missing_file_path_field(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """422 when file_path field is missing from request body."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory)

    resp = await client.post(
        f"/api/v1/issues/{issue_id}/import-file",
        json={},
    )
    assert resp.status_code == 422


# ── Supported Format Edge Cases ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extension",
    ["cbz", "cbr", "cb7", "cbt", "pdf", "epub"],
)
async def test_import_file_all_supported_formats(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    extension: str,
) -> None:
    """All supported comic formats are accepted."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(
        _db_factory, issue_number=float(hash(extension) % 100 + 1)
    )

    comic_file = tmp_path / f"Comic.{extension}"
    comic_file.write_bytes(b"\x00" * 100)

    mock_lf = AsyncMock()
    mock_lf.id = 100
    mock_lf.file_path = str(comic_file)
    mock_lf.file_name = f"Comic.{extension}"
    mock_lf.file_size = 100
    mock_lf.file_format = extension
    mock_lf.match_confidence = MatchConfidence.MANUAL

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        return_value=mock_lf,
    ):
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={"file_path": str(comic_file)},
        )

    assert resp.status_code == 201, f"Failed for .{extension}: {resp.text}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extension",
    ["txt", "jpg", "png", "zip", "rar", "doc", "html", "mp4", "exe"],
)
async def test_import_file_rejects_unsupported_formats(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    extension: str,
) -> None:
    """Unsupported file formats are rejected with 400."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(
        _db_factory, issue_number=float(hash(extension) % 100 + 200)
    )

    bad_file = tmp_path / f"file.{extension}"
    bad_file.write_bytes(b"\x00" * 100)

    resp = await client.post(
        f"/api/v1/issues/{issue_id}/import-file",
        json={"file_path": str(bad_file)},
    )
    assert resp.status_code == 400, f"Should reject .{extension}: {resp.text}"


# ── Case sensitivity ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_file_uppercase_extension(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Uppercase extensions like .CBZ are accepted."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory)

    comic_file = tmp_path / "Batman 001.CBZ"
    comic_file.write_bytes(b"PK" + b"\x00" * 100)

    mock_lf = AsyncMock()
    mock_lf.id = 101
    mock_lf.file_path = str(comic_file)
    mock_lf.file_name = "Batman 001.CBZ"
    mock_lf.file_size = 102
    mock_lf.file_format = "cbz"
    mock_lf.match_confidence = MatchConfidence.MANUAL

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        return_value=mock_lf,
    ):
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={"file_path": str(comic_file)},
        )

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_import_file_mixed_case_extension(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Mixed case extensions like .CbZ are accepted."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory, issue_number=99.0)

    comic_file = tmp_path / "Batman 099.CbZ"
    comic_file.write_bytes(b"PK" + b"\x00" * 100)

    mock_lf = AsyncMock()
    mock_lf.id = 102
    mock_lf.file_path = str(comic_file)
    mock_lf.file_name = "Batman 099.CbZ"
    mock_lf.file_size = 102
    mock_lf.file_format = "cbz"
    mock_lf.match_confidence = MatchConfidence.MANUAL

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        return_value=mock_lf,
    ):
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={"file_path": str(comic_file)},
        )

    assert resp.status_code == 201


# ── Issue Status Transition Tests ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_status",
    [IssueStatus.WANTED, IssueStatus.SKIPPED, IssueStatus.UNKNOWN],
)
async def test_import_file_from_various_statuses(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    initial_status: IssueStatus,
) -> None:
    """Import succeeds from WANTED, SKIPPED, and UNKNOWN statuses."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(
        _db_factory,
        issue_status=initial_status,
        issue_number=float(hash(initial_status.value) % 100 + 300),
    )

    comic_file = tmp_path / f"Comic_{initial_status.value}.cbz"
    comic_file.write_bytes(b"PK" + b"\x00" * 100)

    mock_lf = AsyncMock()
    mock_lf.id = 200
    mock_lf.file_path = str(comic_file)
    mock_lf.file_name = comic_file.name
    mock_lf.file_size = 102
    mock_lf.file_format = "cbz"
    mock_lf.match_confidence = MatchConfidence.MANUAL

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        return_value=mock_lf,
    ):
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={"file_path": str(comic_file)},
        )

    assert resp.status_code == 201, f"Failed for status {initial_status.value}: {resp.text}"


# ── Response Data Validation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_file_response_shape(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Response contains all expected fields."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory)

    comic_file = tmp_path / "Batman 001 (2016).cbz"
    comic_file.write_bytes(b"PK" + b"\x00" * 2000)

    mock_lf = AsyncMock()
    mock_lf.id = 300
    mock_lf.file_path = "/tmp/test-comics/Batman (2016)/Batman 001 (2016).cbz"
    mock_lf.file_name = "Batman 001 (2016).cbz"
    mock_lf.file_size = 2002
    mock_lf.file_format = "cbz"
    mock_lf.match_confidence = MatchConfidence.MANUAL

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        return_value=mock_lf,
    ):
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={"file_path": str(comic_file)},
        )

    assert resp.status_code == 201
    data = resp.json()
    assert "issue_id" in data
    assert "library_file_id" in data
    assert "file_name" in data
    assert "file_path" in data
    assert "file_size" in data
    assert "file_format" in data
    assert "match_confidence" in data
    assert data["match_confidence"] == "manual"
    assert data["file_size"] == 2002


# ── register_library_file Error Propagation ───────────────────────────


@pytest.mark.asyncio
async def test_import_file_register_raises_file_not_found(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """FileNotFoundError from register_library_file is handled gracefully."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory)

    comic_file = tmp_path / "Batman 001.cbz"
    comic_file.write_bytes(b"PK" + b"\x00" * 100)

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        side_effect=FileNotFoundError("Source file vanished"),
    ):
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={"file_path": str(comic_file)},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_import_file_register_raises_config_error(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """ConfigurationError from register_library_file returns 400."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory)

    comic_file = tmp_path / "Batman 001.cbz"
    comic_file.write_bytes(b"PK" + b"\x00" * 100)

    from pullbox.core.exceptions import ConfigurationError

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        side_effect=ConfigurationError("No comics directory configured"),
    ):
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={"file_path": str(comic_file)},
        )

    assert resp.status_code == 400
    assert "comics directory" in resp.json()["detail"].lower()


# ── Default Values ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_file_default_move_to_library_true(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """move_to_library defaults to true when not specified."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory, issue_number=50.0)

    comic_file = tmp_path / "Batman 050.cbz"
    comic_file.write_bytes(b"PK" + b"\x00" * 100)

    mock_lf = AsyncMock()
    mock_lf.id = 400
    mock_lf.file_path = str(comic_file)
    mock_lf.file_name = "Batman 050.cbz"
    mock_lf.file_size = 102
    mock_lf.file_format = "cbz"
    mock_lf.match_confidence = MatchConfidence.MANUAL

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        return_value=mock_lf,
    ) as mock_register:
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={"file_path": str(comic_file)},
        )

    assert resp.status_code == 201
    # move_to_library should default to True
    assert mock_register.call_args[1]["move_to_library"] is True


# ── Path Traversal / Security Edge Cases ──────────────────────────────


@pytest.mark.asyncio
async def test_import_file_path_with_null_bytes(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Path with null bytes is rejected."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory, issue_number=60.0)

    resp = await client.post(
        f"/api/v1/issues/{issue_id}/import-file",
        json={"file_path": "/tmp/comic\x00.cbz"},
    )
    # Should be rejected (400 or 422)
    assert resp.status_code in (400, 422)


@pytest.mark.asyncio
async def test_import_file_symlink(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Symlinks to valid comic files are accepted."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory, issue_number=70.0)

    real_file = tmp_path / "real_comic.cbz"
    real_file.write_bytes(b"PK" + b"\x00" * 100)
    link = tmp_path / "link_comic.cbz"
    link.symlink_to(real_file)

    mock_lf = AsyncMock()
    mock_lf.id = 500
    mock_lf.file_path = str(link)
    mock_lf.file_name = "link_comic.cbz"
    mock_lf.file_size = 102
    mock_lf.file_format = "cbz"
    mock_lf.match_confidence = MatchConfidence.MANUAL

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        return_value=mock_lf,
    ):
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={"file_path": str(link)},
        )

    assert resp.status_code == 201


# ── Double Extension / Unusual Filenames ──────────────────────────────


@pytest.mark.asyncio
async def test_import_file_double_extension(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """File with double extension like .tar.cbz uses last extension."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory, issue_number=80.0)

    comic_file = tmp_path / "archive.tar.cbz"
    comic_file.write_bytes(b"PK" + b"\x00" * 100)

    mock_lf = AsyncMock()
    mock_lf.id = 600
    mock_lf.file_path = str(comic_file)
    mock_lf.file_name = "archive.tar.cbz"
    mock_lf.file_size = 102
    mock_lf.file_format = "cbz"
    mock_lf.match_confidence = MatchConfidence.MANUAL

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        return_value=mock_lf,
    ):
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={"file_path": str(comic_file)},
        )

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_import_file_no_extension(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """File with no extension is rejected."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory, issue_number=81.0)

    no_ext_file = tmp_path / "comic_without_extension"
    no_ext_file.write_bytes(b"\x00" * 100)

    resp = await client.post(
        f"/api/v1/issues/{issue_id}/import-file",
        json={"file_path": str(no_ext_file)},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_import_file_hidden_file(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Hidden files (starting with .) with valid extension are accepted."""
    _root_id, _series_id, issue_id = await _create_series_and_issue(_db_factory, issue_number=82.0)

    hidden_file = tmp_path / ".hidden_comic.cbz"
    hidden_file.write_bytes(b"PK" + b"\x00" * 100)

    mock_lf = AsyncMock()
    mock_lf.id = 700
    mock_lf.file_path = str(hidden_file)
    mock_lf.file_name = ".hidden_comic.cbz"
    mock_lf.file_size = 102
    mock_lf.file_format = "cbz"
    mock_lf.match_confidence = MatchConfidence.MANUAL

    with patch(
        "pullbox.services.issue_import_service.register_library_file",
        new_callable=AsyncMock,
        return_value=mock_lf,
    ):
        resp = await client.post(
            f"/api/v1/issues/{issue_id}/import-file",
            json={"file_path": str(hidden_file)},
        )

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_start_manual_issue_import_returns_initial_progress(
    client: AsyncClient,
) -> None:
    """UI start endpoint returns the background run snapshot."""
    with patch(
        "pullbox.api.v1.issues.start_issue_import_run",
        new_callable=AsyncMock,
    ) as mock_start:
        mock_start.return_value = ManualFileImportProgressResponse(
            issue_id=2,
            state="running",
            message="Preparing import...",
            current_file_name="Batman 002.cbz",
            current_file_stage="preparing",
            current_file_progress_current=0,
            current_file_progress_total=1,
            current_file_progress_pct=0,
            current_file_progress_unit="steps",
            file_index=1,
            total_files=1,
        )

        resp = await client.post(
            "/api/v1/issues/2/import-file/start",
            json={
                "file_path": "/tmp/imports/Batman 002.cbz",
                "move_to_library": True,
            },
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data["state"] == "running"
    assert data["current_file_name"] == "Batman 002.cbz"
    assert data["current_file_stage"] == "preparing"


@pytest.mark.asyncio
async def test_cancel_manual_issue_import_returns_cancelled_progress(
    client: AsyncClient,
) -> None:
    """UI cancel endpoint returns the cancelled background run snapshot."""
    with patch(
        "pullbox.api.v1.issues.cancel_issue_import_run",
        new_callable=AsyncMock,
    ) as mock_cancel:
        mock_cancel.return_value = ManualFileImportProgressResponse(
            issue_id=2,
            state="cancelled",
            message="Import cancelled.",
        )

        resp = await client.post("/api/v1/issues/2/import-file/cancel")

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "cancelled"
    assert data["message"] == "Import cancelled."
    mock_cancel.assert_awaited_once_with(2)


@pytest.mark.asyncio
async def test_get_manual_issue_import_progress_returns_snapshot(
    client: AsyncClient,
) -> None:
    """UI progress endpoint returns the current import snapshot."""
    with patch("pullbox.api.v1.issues.get_issue_import_progress_state") as mock_get:
        mock_get.return_value = ManualFileImportProgressResponse(
            issue_id=2,
            state="running",
            message="Importing selected file...",
            current_file_name="Batman 002.cbz",
            current_file_stage="transferring",
            current_file_progress_current=26214400,
            current_file_progress_total=52428800,
            current_file_progress_pct=68,
            current_file_progress_unit="bytes",
            file_index=1,
            total_files=1,
        )

        resp = await client.get("/api/v1/issues/2/import-file/progress")

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "running"
    assert data["current_file_stage"] == "transferring"
    assert data["current_file_progress_pct"] == 68

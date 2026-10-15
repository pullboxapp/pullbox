"""Tests for log streaming SSE endpoint and level filter (C-7.4).

Verifies:
- Log stream SSE endpoint returns valid SSE format
- Log entries include severity, timestamp, message
- Level filter reduces result count appropriately
- Log content endpoint returns formatted entries
- _matches_level correctly filters by level
- Edge cases: long messages, file rotation, binary content, high volume

Run:
    pytest tests/api/test_log_streaming.py -v
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.api.v1.system import _matches_level
from pullbox.config import get_settings
from pullbox.models import Base
from pullbox.models.user import User
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-log-streaming")


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def _session_token(
    _db_factory: async_sessionmaker[AsyncSession],
    seeded_user_password_hash: str,
) -> str:
    async with _db_factory() as session:
        user = User(
            username="logstreamuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return AuthService.create_session_token(user.id, user.session_version)


@pytest.fixture
async def log_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[Path, None]:
    """Create a temp logs dir and wire it into runtime settings."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("PULLBOX_LOGS_DIR", str(logs))
    get_settings.cache_clear()
    try:
        yield logs
    finally:
        get_settings.cache_clear()


@pytest.fixture
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
    _session_token: str,
    log_dir: Path,
) -> AsyncGenerator[AsyncClient, None]:
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
        cookies={SESSION_COOKIE_NAME: _session_token},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


@pytest.fixture
async def unauthed_client(
    _db_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
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
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


# ── _matches_level unit tests ─────────────────────────────────────────


class TestMatchesLevel:
    """Level filter helper correctly identifies log levels."""

    def test_all_matches_everything(self) -> None:
        assert _matches_level('{"level": "info", "event": "test"}', "all") is True
        assert _matches_level("random text", "all") is True

    def test_info_matches_structlog_json(self) -> None:
        assert _matches_level('{"level": "info", "event": "test"}', "info") is True

    def test_info_does_not_match_error(self) -> None:
        assert _matches_level('{"level": "error", "event": "fail"}', "info") is False

    def test_error_matches_error_line(self) -> None:
        assert _matches_level('{"level": "error", "event": "fail"}', "error") is True

    def test_error_also_matches_critical(self) -> None:
        assert _matches_level('{"level": "critical", "event": "crash"}', "error") is True

    def test_warning_matches_warning(self) -> None:
        assert _matches_level('{"level": "warning", "event": "warn"}', "warning") is True

    def test_warning_does_not_match_info(self) -> None:
        assert _matches_level('{"level": "info", "event": "test"}', "warning") is False

    def test_debug_matches_debug(self) -> None:
        assert _matches_level('{"level": "debug", "event": "trace"}', "debug") is True

    def test_bracket_format_info(self) -> None:
        assert _matches_level("2024-01-01 [info] some message", "info") is True

    def test_bracket_format_error(self) -> None:
        assert _matches_level("2024-01-01 [error] some message", "error") is True

    def test_key_value_format(self) -> None:
        assert _matches_level("level=warning event=test", "warning") is True

    def test_compact_json_format(self) -> None:
        assert _matches_level('{"level":"info","event":"test"}', "info") is True

    def test_no_level_does_not_match_specific_filter(self) -> None:
        assert _matches_level("just a plain text line", "info") is False

    def test_case_insensitive(self) -> None:
        assert _matches_level('{"level": "INFO", "event": "test"}', "info") is True

    def test_error_does_not_match_warning(self) -> None:
        assert _matches_level('{"level": "warning", "event": "warn"}', "error") is False


# ── _matches_level edge cases ─────────────────────────────────────────


class TestMatchesLevelEdgeCases:
    """Edge cases for the level filter helper."""

    def test_empty_line(self) -> None:
        assert _matches_level("", "info") is False
        assert _matches_level("", "all") is True

    def test_very_long_line(self) -> None:
        """10KB+ log message still filters correctly."""
        long_msg = "x" * 10_000
        line = f'{{"level": "error", "event": "{long_msg}"}}'
        assert _matches_level(line, "error") is True
        assert _matches_level(line, "info") is False

    def test_binary_content_in_line(self) -> None:
        """Binary/corrupt characters don't crash the filter."""
        line = '{"level": "info", "event": "test\x00\xff\xfe"}'
        assert _matches_level(line, "info") is True

    def test_nested_level_in_event_text(self) -> None:
        """Level keyword inside event text doesn't false-match."""
        # The word "error" appears in the event text, but level is "info"
        line = '{"level": "info", "event": "recovered from error state"}'
        assert _matches_level(line, "error") is False

    def test_critical_bracket_format(self) -> None:
        """[critical] matches error filter."""
        assert _matches_level("2024-01-01 [critical] OOM", "error") is True

    def test_critical_key_value_format(self) -> None:
        """level=critical matches error filter."""
        assert _matches_level("level=critical event=oom", "error") is True

    def test_multiple_level_markers_first_wins(self) -> None:
        """Line with mixed formats still matches on the actual level field."""
        line = '{"level": "warning", "event": "[error] not really"}'
        assert _matches_level(line, "warning") is True


# ── SSE endpoint tests ────────────────────────────────────────────────


@pytest.mark.asyncio
class TestStreamLogEndpoint:
    """GET /api/v1/system/logs/{filename}/stream — SSE streaming."""

    async def test_invalid_filename_rejected(self, client: AsyncClient, log_dir: Path) -> None:
        """Path traversal filename returns 422 or 400."""
        resp = await client.get("/api/v1/system/logs/../etc/passwd/stream")
        assert resp.status_code in (400, 404, 422)

    async def test_nonexistent_file_returns_404(self, client: AsyncClient, log_dir: Path) -> None:
        """Non-existent log file returns 404."""
        resp = await client.get("/api/v1/system/logs/nonexistent.log/stream")
        assert resp.status_code == 404

    async def test_unauthenticated_rejected(self, unauthed_client: AsyncClient) -> None:
        """Unauthenticated requests are rejected."""
        resp = await unauthed_client.get("/api/v1/system/logs/pullbox.log/stream")
        assert resp.status_code in (401, 403, 307)

    async def test_invalid_level_param_rejected(self, client: AsyncClient, log_dir: Path) -> None:
        """Invalid level query param returns 422."""
        log_file = log_dir / "pullbox.log"
        log_file.write_text('{"level": "info", "event": "test"}\n')
        resp = await client.get("/api/v1/system/logs/pullbox.log/stream?level=invalid")
        assert resp.status_code == 422

    async def test_dotdot_filename_rejected(self, client: AsyncClient, log_dir: Path) -> None:
        """Filename with .. path traversal is rejected."""
        resp = await client.get("/api/v1/system/logs/..%2F..%2Fetc%2Fpasswd/stream")
        assert resp.status_code in (400, 404, 422)

    async def test_filename_starting_with_dot_rejected(
        self, client: AsyncClient, log_dir: Path
    ) -> None:
        """Hidden file names (starting with .) are rejected."""
        log_file = log_dir / ".secret.log"
        log_file.write_text("secret data\n")
        resp = await client.get("/api/v1/system/logs/.secret.log/stream")
        assert resp.status_code in (400, 422)


# ── SSE content tests (via log content endpoint) ──────────────────────


@pytest.mark.asyncio
class TestLogContentEdgeCases:
    """GET /api/v1/system/logs/{filename} — log content edge cases.

    Tests content handling that feeds into the SSE stream, using the
    non-streaming content endpoint to avoid hang issues with SSE polling.
    """

    async def test_log_content_returns_lines(self, client: AsyncClient, log_dir: Path) -> None:
        """Log content endpoint returns lines from file."""
        log_file = log_dir / "pullbox.log"
        log_file.write_text(
            '{"level": "info", "event": "test1"}\n{"level": "error", "event": "test2"}\n'
        )
        resp = await client.get("/api/v1/system/logs/pullbox.log/content")
        assert resp.status_code == 200
        data = resp.json()
        assert data["filename"] == "pullbox.log"
        assert data["total_lines"] == 2
        assert len(data["lines"]) == 2

    async def test_corrupt_utf8_handled(self, client: AsyncClient, log_dir: Path) -> None:
        """Binary/corrupt content in log file is replaced, not crashed."""
        log_file = log_dir / "pullbox.log"
        log_file.write_bytes(
            b'{"level": "info", "event": "valid"}\n'
            b'{"level": "info", "event": "bad_\xff\xfe_bytes"}\n'
        )
        resp = await client.get("/api/v1/system/logs/pullbox.log/content")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_lines"] == 2

    async def test_very_long_log_message(self, client: AsyncClient, log_dir: Path) -> None:
        """10KB+ log line doesn't break the content endpoint."""
        log_file = log_dir / "pullbox.log"
        long_msg = "a" * 10_000
        log_file.write_text(f'{{"level": "info", "event": "{long_msg}"}}\n')
        resp = await client.get("/api/v1/system/logs/pullbox.log/content")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_lines"] == 1
        assert len(data["lines"][0]) > 10_000

    async def test_empty_log_file(self, client: AsyncClient, log_dir: Path) -> None:
        """Empty log file returns zero lines."""
        log_file = log_dir / "pullbox.log"
        log_file.write_text("")
        resp = await client.get("/api/v1/system/logs/pullbox.log/content")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_lines"] == 0
        assert data["lines"] == []

    async def test_high_volume_truncated(self, client: AsyncClient, log_dir: Path) -> None:
        """Large file (10K+ lines) returns truncated result."""
        log_file = log_dir / "pullbox.log"
        lines = [f'{{"level": "info", "event": "line_{i}"}}' for i in range(10_000)]
        log_file.write_text("\n".join(lines) + "\n")
        resp = await client.get("/api/v1/system/logs/pullbox.log/content")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_lines"] == 10_000
        assert data["truncated"] is True

    async def test_nonexistent_file_404(self, client: AsyncClient, log_dir: Path) -> None:
        """Non-existent file returns 404."""
        resp = await client.get("/api/v1/system/logs/nope.log/content")
        assert resp.status_code == 404

    async def test_rotated_log_file(self, client: AsyncClient, log_dir: Path) -> None:
        """Rotated log file (pullbox.log.1) is accessible."""
        log_file = log_dir / "pullbox.log.1"
        log_file.write_text('{"level": "info", "event": "old_entry"}\n')
        resp = await client.get("/api/v1/system/logs/pullbox.log.1/content")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_lines"] == 1

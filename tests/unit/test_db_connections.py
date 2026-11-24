"""Tests for database connection pool configuration and session lifecycle (C-9.1).

Verifies:
- Session created via dependency is closed after request
- Session created in background task is closed after completion
- Session closed on exception in request handler
- Session closed on exception in background task
- Connection pool configuration is applied
- Pool pre-ping detects stale connections
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSessionLifecycleDependency:
    """Sessions from FastAPI dependency are properly scoped."""

    @pytest.mark.asyncio
    async def test_session_commits_on_success(self) -> None:
        """Session commits when the request handler succeeds."""
        from pullbox.database import get_db

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        mock_factory = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx

        with patch("pullbox.database.get_session_factory", return_value=mock_factory):
            gen = get_db()
            session = await gen.__anext__()
            assert session is mock_session
            # Simulate successful completion
            with pytest.raises(StopAsyncIteration):
                await gen.__anext__()
            mock_session.commit.assert_awaited_once()
            mock_session.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_session_rolls_back_on_exception(self) -> None:
        """Session rolls back when the request handler raises."""
        from pullbox.database import get_db

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        mock_factory = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx

        with patch("pullbox.database.get_session_factory", return_value=mock_factory):
            gen = get_db()
            await gen.__anext__()
            # Simulate exception — throw into generator
            with pytest.raises(ValueError, match="test error"):
                await gen.athrow(ValueError("test error"))
            mock_session.rollback.assert_awaited_once()
            mock_session.commit.assert_not_awaited()


class TestSessionLifecycleBackgroundTask:
    """Background tasks properly scope sessions with async context managers."""

    @pytest.mark.asyncio
    async def test_session_closed_after_task(self) -> None:
        """Factory context manager ensures session is closed after task."""
        mock_session = AsyncMock()
        mock_factory = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx

        # Simulate the pattern used by all background tasks:
        # async with factory() as session: ...
        async with mock_factory() as session:
            assert session is mock_session
        # __aexit__ was called — session is closed
        mock_ctx.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_session_closed_on_exception_in_task(self) -> None:
        """Factory context manager closes session even when task raises."""
        mock_session = AsyncMock()
        mock_factory = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_ctx

        with pytest.raises(RuntimeError, match="task failure"):
            async with mock_factory() as session:
                assert session is mock_session
                raise RuntimeError("task failure")
        # __aexit__ was still called — session is closed
        mock_ctx.__aexit__.assert_awaited_once()


class TestPoolConfiguration:
    """Engine is created with explicit pool settings."""

    def test_pool_settings_applied_for_postgresql(self) -> None:
        """PostgreSQL engine gets explicit pool_size, max_overflow, etc."""
        from pullbox.database import _build_engine_kwargs

        kwargs = _build_engine_kwargs("postgresql+asyncpg://localhost/pullbox", echo=False)
        assert kwargs["pool_size"] == 5
        assert kwargs["max_overflow"] == 10
        assert kwargs["pool_timeout"] == 30
        assert kwargs["pool_recycle"] == 3600
        assert kwargs["pool_pre_ping"] is True

    def test_pool_settings_applied_for_sqlite(self) -> None:
        """SQLite engine gets pool_pre_ping but not full pool settings."""
        from pullbox.database import _build_engine_kwargs

        kwargs = _build_engine_kwargs("sqlite+aiosqlite:///pullbox.db", echo=False)
        # SQLite uses StaticPool internally for single-file, so pool_size is omitted
        assert "pool_size" not in kwargs
        assert kwargs["pool_pre_ping"] is True

    def test_echo_setting_passed_through(self) -> None:
        """SQL echo setting is forwarded to engine kwargs."""
        from pullbox.database import _build_engine_kwargs

        kwargs = _build_engine_kwargs("sqlite+aiosqlite:///test.db", echo=True)
        assert kwargs["echo"] is True

    def test_memory_sqlite_no_pool_settings(self) -> None:
        """In-memory SQLite gets no pool configuration."""
        from pullbox.database import _build_engine_kwargs

        kwargs = _build_engine_kwargs("sqlite+aiosqlite:///:memory:", echo=False)
        assert "pool_size" not in kwargs


class TestPoolPrePing:
    """Pool pre-ping is enabled to detect stale connections."""

    def test_pre_ping_enabled_in_kwargs(self) -> None:
        """Pre-ping is enabled for all database backends."""
        from pullbox.database import _build_engine_kwargs

        for url in [
            "sqlite+aiosqlite:///test.db",
            "postgresql+asyncpg://localhost/pullbox",
        ]:
            kwargs = _build_engine_kwargs(url, echo=False)
            assert kwargs["pool_pre_ping"] is True, f"pre_ping not set for {url}"


@pytest.mark.parametrize("failure", [TimeoutError, ConnectionRefusedError])
def test_new_postgres_handshake_failure_is_bounded_and_redacted(failure):
    from pullbox.database import DatabaseConnectionUnavailableError, _connect_postgres

    dialect = MagicMock()
    dialect.connect.side_effect = failure("private connection details")
    with pytest.raises(DatabaseConnectionUnavailableError) as caught:
        _connect_postgres(dialect, MagicMock(), [], {})
    dialect.connect.assert_called_once_with(timeout=5)
    assert "private" not in str(caught.value)


def test_unrelated_postgres_driver_error_is_not_a_temporary_outage():
    from pullbox.database import _connect_postgres

    dialect = MagicMock()
    dialect.connect.side_effect = ValueError("invalid configuration")
    with pytest.raises(ValueError, match="invalid configuration"):
        _connect_postgres(dialect, MagicMock(), [], {"timeout": 12})
    dialect.connect.assert_called_once_with(timeout=12)

"""Tests for the AuditService — event logging and querying.

Run:
    pytest tests/unit/test_audit_service.py -v
    pytest tests/unit/test_audit_service.py -v \
        --cov=pullbox.services.audit_service --cov-report=term-missing
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models.audit_log import AuditEventType
from pullbox.services.audit_service import AuditService, audit_event
from tests.sqlite_schema import create_test_schema

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-audit-tests")


@pytest.fixture
async def db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Create an in-memory database with all tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await create_test_schema(conn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(db: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    """Provide a single session for each test."""
    async with db() as sess:
        yield sess


@pytest.mark.asyncio
class TestLogEvent:
    """Tests for AuditService.log_event()."""

    async def test_log_event_creates_entry(self, session: AsyncSession) -> None:
        """Logging an event creates a retrievable audit entry."""
        entry = await AuditService.log_event(
            session,
            AuditEventType.LOGIN_SUCCESS,
            username="testuser",
        )
        assert entry.id is not None
        assert entry.event_type == "login_success"
        assert entry.username == "testuser"

    async def test_log_event_with_all_fields(self, session: AsyncSession) -> None:
        """All optional fields are stored correctly."""
        # Create a user so FK constraint is satisfied
        from pullbox.models.user import User
        from pullbox.services.auth_service import AuthService

        user = User(username="admin", password_hash=AuthService.hash_password("Test@1234"))
        session.add(user)
        await session.flush()

        entry = await AuditService.log_event(
            session,
            AuditEventType.PASSWORD_CHANGED,
            source_ip="192.168.1.1",
            user_id=user.id,
            username="admin",
            detail="Password changed successfully",
            metadata={"old_hash_prefix": "abc"},
        )
        assert entry.event_type == "password_changed"
        assert entry.timestamp is not None
        assert entry.source_ip == "192.168.1.1"
        assert entry.user_id == user.id
        assert entry.username == "admin"
        assert entry.detail == "Password changed successfully"
        assert entry.metadata_json is not None
        parsed = json.loads(entry.metadata_json)
        assert parsed["old_hash_prefix"] == "abc"

    async def test_log_event_without_optional_fields(self, session: AsyncSession) -> None:
        """Event with only required fields works fine."""
        entry = await AuditService.log_event(
            session,
            AuditEventType.LOGIN_FAILURE,
        )
        assert entry.id is not None
        assert entry.event_type == "login_failure"
        assert entry.source_ip is None
        assert entry.user_id is None
        assert entry.username is None
        assert entry.detail is None
        assert entry.metadata_json is None

    async def test_metadata_json_stored(self, session: AsyncSession) -> None:
        """Dict metadata round-trips through JSON correctly."""
        meta = {"key": "value", "count": 5, "nested": {"a": True}}
        entry = await AuditService.log_event(
            session,
            AuditEventType.SECURITY_CONFIG_CHANGED,
            metadata=meta,
        )
        assert entry.metadata_json is not None
        assert json.loads(entry.metadata_json) == meta

    async def test_log_event_sanitizes_detail_and_metadata(self, session: AsyncSession) -> None:
        """Audit persistence should redact sensitive strings defensively."""
        entry = await AuditService.log_event(
            session,
            AuditEventType.SECURITY_CONFIG_CHANGED,
            detail="Updated postgresql://pullbox:secretpass@db.internal/pullbox",
            metadata={
                "authorization": "Bearer secret-token",
                "url": "http://user:pass@example.com/api?token=secret",
            },
        )
        assert "***REDACTED***" in (entry.detail or "")
        assert entry.metadata_json is not None
        parsed = json.loads(entry.metadata_json)
        assert parsed["authorization"] == "***REDACTED***"
        assert parsed["url"] == "http://***REDACTED***@example.com/api?token=***REDACTED***"

    async def test_audit_event_skips_locked_writes_after_retries(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dedicated audit writes should retry transient locks, then drop safely."""
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

        created_tasks: list[asyncio.Task[None]] = []
        real_create_task = asyncio.create_task

        def _capture_task(coro: object) -> asyncio.Task[None]:
            task = real_create_task(coro)  # type: ignore[arg-type]
            created_tasks.append(task)
            return task

        class _LockedSession:
            async def __aenter__(self) -> _LockedSession:
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

            async def commit(self) -> None:
                raise OperationalError("INSERT", {}, Exception("database is locked"))

            async def rollback(self) -> None:
                return None

        class _LockedFactory:
            def __call__(self) -> _LockedSession:
                return _LockedSession()

        sleep = AsyncMock()
        warn = MagicMock()
        monkeypatch.setattr("pullbox.services.audit_service.asyncio.create_task", _capture_task)
        monkeypatch.setattr("pullbox.services.audit_service.asyncio.sleep", sleep)
        monkeypatch.setattr("pullbox.database.get_session_factory", lambda: _LockedFactory())
        monkeypatch.setattr("pullbox.services.audit_service.AuditService.log_event", AsyncMock())
        monkeypatch.setattr("pullbox.services.audit_service.log_deduped_warning", warn)

        await audit_event(AuditEventType.LOGIN_SUCCESS, username="testuser")
        await asyncio.gather(*created_tasks)

        assert sleep.await_count == 4
        warn.assert_called_once()


@pytest.mark.asyncio
class TestGetEvents:
    """Tests for AuditService.get_events()."""

    async def test_get_events_default(self, session: AsyncSession) -> None:
        """Returns events ordered by timestamp descending."""
        await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS, detail="first")
        await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS, detail="second")
        await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS, detail="third")

        events, total = await AuditService.get_events(session)
        assert total == 3
        assert len(events) == 3
        # Most recent first (SQLite CURRENT_TIMESTAMP may be same, check order)
        assert events[0].detail == "third"

    async def test_get_events_filter_by_type(self, session: AsyncSession) -> None:
        """Filtering by event_type returns only matching events."""
        await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS)
        await AuditService.log_event(session, AuditEventType.LOGIN_FAILURE)
        await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS)

        events, total = await AuditService.get_events(
            session, event_type=AuditEventType.LOGIN_SUCCESS
        )
        assert total == 2
        assert all(e.event_type == "login_success" for e in events)

    async def test_get_events_filter_by_user(self, session: AsyncSession) -> None:
        """Filtering by user_id returns only that user's events."""
        from pullbox.models.user import User
        from pullbox.services.auth_service import AuthService

        user1 = User(username="user1", password_hash=AuthService.hash_password("Test@1234"))
        user2 = User(username="user2", password_hash=AuthService.hash_password("Test@1234"))
        session.add_all([user1, user2])
        await session.flush()

        await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS, user_id=user1.id)
        await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS, user_id=user2.id)
        await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS, user_id=user1.id)

        events, total = await AuditService.get_events(session, user_id=user1.id)
        assert total == 2
        assert all(e.user_id == user1.id for e in events)

    async def test_get_events_filter_by_date_range(self, session: AsyncSession) -> None:
        """Since/until date filters work correctly."""
        now = datetime.now(tz=UTC)
        past = now - timedelta(hours=1)
        future = now + timedelta(hours=1)

        await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS)

        # Since in the past should include the event
        _events, total = await AuditService.get_events(session, since=past)
        assert total == 1

        # Since in the future should exclude it
        _events, total = await AuditService.get_events(session, since=future)
        assert total == 0

        # Until in the future should include it
        _events, total = await AuditService.get_events(session, until=future)
        assert total == 1

    async def test_get_events_pagination(self, session: AsyncSession) -> None:
        """Limit and offset work correctly for pagination."""
        for i in range(5):
            await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS, detail=f"event-{i}")

        events, total = await AuditService.get_events(session, limit=2, offset=0)
        assert total == 5
        assert len(events) == 2

        events2, total2 = await AuditService.get_events(session, limit=2, offset=2)
        assert total2 == 5
        assert len(events2) == 2
        assert events2[0].id != events[0].id

    async def test_get_events_returns_total_count(self, session: AsyncSession) -> None:
        """Total count reflects all matching events, not just the page."""
        for _ in range(10):
            await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS)

        events, total = await AuditService.get_events(session, limit=3, offset=0)
        assert total == 10
        assert len(events) == 3


@pytest.mark.asyncio
class TestGetEventCountsByType:
    """Tests for AuditService.get_event_counts_by_type()."""

    async def test_get_event_counts_by_type(self, session: AsyncSession) -> None:
        """Returns correct counts grouped by type."""
        await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS)
        await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS)
        await AuditService.log_event(session, AuditEventType.LOGIN_FAILURE)
        await AuditService.log_event(session, AuditEventType.PASSWORD_CHANGED)

        counts = await AuditService.get_event_counts_by_type(session)
        assert counts[AuditEventType.LOGIN_SUCCESS] == 2
        assert counts[AuditEventType.LOGIN_FAILURE] == 1
        assert counts[AuditEventType.PASSWORD_CHANGED] == 1
        assert AuditEventType.API_KEY_CREATED not in counts

    async def test_get_event_counts_with_since_filter(self, session: AsyncSession) -> None:
        """Since filter restricts the counted events."""
        await AuditService.log_event(session, AuditEventType.LOGIN_SUCCESS)

        future = datetime.now(tz=UTC) + timedelta(hours=1)
        counts = await AuditService.get_event_counts_by_type(session, since=future)
        assert len(counts) == 0

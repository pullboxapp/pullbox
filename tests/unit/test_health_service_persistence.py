"""Health-result persistence retries should use fresh sessions."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError, PendingRollbackError

from pullbox.models.health import HealthStatus
from pullbox.services import health_service
from pullbox.services.health_service import CheckOutcome, HealthService


class _FakeHealthSession:
    def __init__(self, factory: _FakeHealthSessionFactory) -> None:
        self._factory = factory
        self.rows = []

    async def __aenter__(self) -> _FakeHealthSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def add(self, row) -> None:  # type: ignore[no-untyped-def]
        self.rows.append(row)

    async def execute(self, _stmt):  # type: ignore[no-untyped-def]
        return _FakeExecuteResult()

    async def flush(self) -> None:
        self._factory.flush_attempts += 1
        if self._factory.flush_attempts == 1:
            raise OperationalError("INSERT", {}, Exception("database is locked"))

    async def commit(self) -> None:
        self._factory.commit_attempts += 1

    async def rollback(self) -> None:
        self._factory.rollback_attempts += 1


class _FakeHealthSessionFactory:
    def __init__(self) -> None:
        self.session_count = 0
        self.flush_attempts = 0
        self.commit_attempts = 0
        self.rollback_attempts = 0

    def __call__(self) -> _FakeHealthSession:
        self.session_count += 1
        return _FakeHealthSession(self)


class _FakeExecuteResult:
    def scalars(self) -> _FakeExecuteResult:
        return self

    def all(self) -> list[object]:
        return []


class _PendingRollbackHealthSession(_FakeHealthSession):
    async def execute(self, _stmt):  # type: ignore[no-untyped-def]
        factory = self._factory
        assert isinstance(factory, _PendingRollbackHealthSessionFactory)
        factory.execute_attempts += 1
        if factory.execute_attempts == 1:
            raise PendingRollbackError("connection invalidated during the health check")
        return _FakeExecuteResult()

    async def flush(self) -> None:
        self._factory.flush_attempts += 1


class _PendingRollbackHealthSessionFactory(_FakeHealthSessionFactory):
    def __init__(self) -> None:
        super().__init__()
        self.execute_attempts = 0

    def __call__(self) -> _PendingRollbackHealthSession:
        self.session_count += 1
        return _PendingRollbackHealthSession(self)


@pytest.mark.asyncio
async def test_health_persist_outcomes_retries_with_fresh_sessions(monkeypatch) -> None:
    """Locked writes should reopen a fresh session before retrying."""
    factory = _FakeHealthSessionFactory()
    monkeypatch.setattr(health_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(health_service, "sqlite_lock_retry_delay", lambda _attempt: 0.0)

    outcomes = [
        CheckOutcome(
            component="database",
            check_name="connectivity",
            status=HealthStatus.HEALTHY,
            message="Connected",
        )
    ]

    initial_session = factory()

    await HealthService._persist_outcomes(initial_session, outcomes)

    assert factory.session_count == 2
    assert factory.flush_attempts == 2
    assert factory.rollback_attempts == 1
    assert factory.commit_attempts == 2


@pytest.mark.asyncio
async def test_health_persist_outcomes_recovers_from_pending_rollback(monkeypatch) -> None:
    factory = _PendingRollbackHealthSessionFactory()
    monkeypatch.setattr(health_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(health_service, "sqlite_lock_retry_delay", lambda _attempt: 0.0)
    outcomes = [
        CheckOutcome(
            component="database",
            check_name="connectivity",
            status=HealthStatus.UNHEALTHY,
            message="Check timed out",
        )
    ]
    initial_session = factory()

    await HealthService._persist_outcomes(initial_session, outcomes)

    assert factory.session_count == 2
    assert factory.rollback_attempts == 1
    assert factory.flush_attempts == 1
    assert factory.commit_attempts == 2

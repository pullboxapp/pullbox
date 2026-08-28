"""Unit tests for the health check engine.

Each check is tested in isolation with mocked dependencies.  Tests cover
healthy, degraded, and unhealthy states, timeout behaviour, exception
isolation, and actionable guidance presence.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.direct_acquisition import (
    DirectArtifactHostKind,
    DirectHostConfig,
    DirectHostReachabilityState,
    DirectProviderConfig,
    DirectProviderState,
)
from pullbox.models.health import HealthStatus
from pullbox.models.library import LibraryRoot
from pullbox.providers.airdcpp.supervisor import (
    AirDcppSupervisorHealth,
    AirDcppSupervisorState,
)
from pullbox.providers.base import ProviderHealthResult, ProviderRegistry
from pullbox.services.health_service import CheckOutcome, HealthService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def async_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
def settings(tmp_path) -> MagicMock:
    s = MagicMock()
    s.comicvine_api_key = "test-key"
    s.data_dir = tmp_path / "data"
    s.logs_dir = tmp_path / "logs"
    s.backup_dir = tmp_path / "backups"
    s.data_dir.mkdir()
    s.logs_dir.mkdir()
    s.backup_dir.mkdir()
    return s


@pytest.fixture
def mock_scheduler() -> MagicMock:
    sched = MagicMock()
    sched.running = True
    sched.get_jobs.return_value = [{"id": "run_health_checks"}]
    sched.get_scheduled_tasks.return_value = []
    return sched


@pytest.fixture
def registry() -> ProviderRegistry:
    return ProviderRegistry()


def _make_service(
    settings: Any = None,
    registry: ProviderRegistry | None = None,
    scheduler: Any = None,
    bootstrap_errors: dict[str, list[dict[str, str]]] | None = None,
) -> HealthService:
    return HealthService(
        settings=settings,
        registry=registry,
        scheduler=scheduler,
        bootstrap_errors=bootstrap_errors,
    )


class _EmptyExecuteResult:
    def scalars(self) -> _EmptyExecuteResult:
        return self

    def all(self) -> list[object]:
        return []


# ---------------------------------------------------------------------------
# Database check
# ---------------------------------------------------------------------------


class TestDatabaseCheck:
    @pytest.mark.asyncio
    async def test_healthy(
        self, db_session: AsyncSession, settings: MagicMock, mock_scheduler: MagicMock
    ) -> None:
        service = _make_service(settings, scheduler=mock_scheduler)
        outcomes = await service.run_check(db_session, "database")
        assert len(outcomes) >= 1
        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].component == "database"
        assert outcomes[0].message == "Connected"
        assert outcomes[0].response_time_ms > 0
        # Verify sub-check details
        assert "checks" in outcomes[0].details
        assert len(outcomes[0].details["checks"]) == 2
        assert {check["name"] for check in outcomes[0].details["checks"]} == {
            "Connection round trip",
            "Query latency",
        }
        assert all(check["status"] == "healthy" for check in outcomes[0].details["checks"])

    @pytest.mark.asyncio
    async def test_degraded_high_latency(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        service = _make_service(settings)
        # Patch time.perf_counter to simulate >500ms latency
        call_count = 0
        original_perf_counter = __import__("time").perf_counter

        def slow_perf_counter() -> float:
            nonlocal call_count
            call_count += 1
            base = original_perf_counter()
            # Second call returns 0.6s later than first
            if call_count % 2 == 0:
                return base + 0.6
            return base

        with patch(
            "pullbox.services.health_service.time.perf_counter", side_effect=slow_perf_counter
        ):
            # Access private method directly for precision
            outcome = await service._check_database(db_session)

        assert outcome.status == HealthStatus.DEGRADED
        assert outcome.message == "Database performance degraded"
        assert all(check["status"] == "degraded" for check in outcome.details["checks"])

    @pytest.mark.asyncio
    async def test_round_trip_message_switches_to_seconds_above_one_second(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        service = _make_service(settings)
        call_count = 0
        original_perf_counter = __import__("time").perf_counter

        def slow_perf_counter() -> float:
            nonlocal call_count
            call_count += 1
            base = original_perf_counter()
            if call_count % 2 == 0:
                return base + 1.2
            return base

        with patch(
            "pullbox.services.health_service.time.perf_counter", side_effect=slow_perf_counter
        ):
            result = await service._check_database_round_trip(db_session)

        assert result.status == HealthStatus.UNHEALTHY
        assert result.message == "SELECT 1 took 1.2s (threshold: 1.0s)"

    @pytest.mark.asyncio
    async def test_unhealthy_connection_error(self, settings: MagicMock) -> None:
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                Exception("connection refused"),
                _EmptyExecuteResult(),
                _EmptyExecuteResult(),
            ]
        )
        session.add = MagicMock()
        session.flush = AsyncMock()

        service = _make_service(settings)
        outcomes = await service.run_check(session, "database")
        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert "connection refused" in outcomes[0].message.lower()


# ---------------------------------------------------------------------------
# Database size sub-check
# ---------------------------------------------------------------------------


class TestDatabaseSizeCheck:
    """Tests for SQLite database file size sub-check."""

    @staticmethod
    def _make_session_with_url(url: str) -> MagicMock:
        """Create a mock session whose get_bind().url returns the given URL."""
        session = MagicMock(spec=AsyncSession)
        mock_bind = MagicMock()
        mock_bind.url = url
        session.get_bind.return_value = mock_bind
        return session

    @pytest.mark.asyncio
    async def test_healthy_small_db(self, settings: MagicMock) -> None:
        """Database size below 500 MB -> healthy sub-check."""
        session = self._make_session_with_url("sqlite+aiosqlite:////data/pullbox.db")
        service = _make_service(settings)

        mock_stat = MagicMock()
        mock_stat.st_size = 100 * 1024 * 1024  # 100 MB

        with patch("pathlib.Path.stat", return_value=mock_stat):
            result = await service._check_db_size(session)

        assert result is not None
        assert result.status == HealthStatus.HEALTHY
        assert result.name == "Database size"
        assert "100.0 MB" in result.message

    @pytest.mark.asyncio
    async def test_degraded_large_db(self, settings: MagicMock) -> None:
        """Database size 500-1000 MB -> degraded sub-check."""
        session = self._make_session_with_url("sqlite+aiosqlite:////data/pullbox.db")
        service = _make_service(settings)

        mock_stat = MagicMock()
        mock_stat.st_size = 600 * 1024 * 1024  # 600 MB

        with patch("pathlib.Path.stat", return_value=mock_stat):
            result = await service._check_db_size(session)

        assert result is not None
        assert result.status == HealthStatus.DEGRADED
        assert "600 MB" in result.message
        assert "threshold: 500 MB" in result.message

    @pytest.mark.asyncio
    async def test_unhealthy_very_large_db(self, settings: MagicMock) -> None:
        """Database size above 1000 MB -> unhealthy sub-check."""
        session = self._make_session_with_url("sqlite+aiosqlite:////data/pullbox.db")
        service = _make_service(settings)

        mock_stat = MagicMock()
        mock_stat.st_size = 1200 * 1024 * 1024  # 1200 MB

        with patch("pathlib.Path.stat", return_value=mock_stat):
            result = await service._check_db_size(session)

        assert result is not None
        assert result.status == HealthStatus.UNHEALTHY
        assert "1200 MB" in result.message
        assert "threshold: 1000 MB" in result.message

    @pytest.mark.asyncio
    async def test_skipped_for_non_sqlite(self, settings: MagicMock) -> None:
        """PostgreSQL databases skip the size check entirely."""
        session = self._make_session_with_url("postgresql+asyncpg://localhost/pullbox")
        service = _make_service(settings)
        result = await service._check_db_size(session)
        assert result is None

    @pytest.mark.asyncio
    async def test_skipped_for_memory_db(self, settings: MagicMock) -> None:
        """In-memory SQLite databases skip the size check."""
        session = self._make_session_with_url("sqlite+aiosqlite:///:memory:")
        service = _make_service(settings)
        result = await service._check_db_size(session)
        assert result is None

    @pytest.mark.asyncio
    async def test_skipped_on_file_error(self, settings: MagicMock) -> None:
        """File access error -> None (gracefully skipped)."""
        session = self._make_session_with_url("sqlite+aiosqlite:////data/pullbox.db")
        service = _make_service(settings)

        with patch("pathlib.Path.stat", side_effect=OSError("No such file")):
            result = await service._check_db_size(session)

        assert result is None

    @pytest.mark.asyncio
    async def test_database_check_integrates_size(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        """_check_database includes db size sub-check when available."""
        service = _make_service(settings)
        size_result = {
            "name": "Database size",
            "status": "degraded",
            "message": "600 MB (threshold: 500 MB)",
        }
        with patch.object(
            service, "_check_db_size", new_callable=AsyncMock, return_value=size_result
        ):
            outcome = await service._check_database(db_session)

        assert outcome.status == HealthStatus.DEGRADED
        assert outcome.message == "Database storage issue"
        assert len(outcome.details["checks"]) == 3
        assert outcome.details["checks"][-1]["name"] == "Database size"
        assert outcome.details["checks"][-1]["status"] == "degraded"
        assert "cleanup_history" in outcome.actionable_guidance

    @pytest.mark.asyncio
    async def test_database_check_unhealthy_size(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        """_check_database shows unhealthy when db size is critical."""
        service = _make_service(settings)
        size_result = {
            "name": "Database size",
            "status": "unhealthy",
            "message": "1200 MB (threshold: 1000 MB)",
        }
        with patch.object(
            service, "_check_db_size", new_callable=AsyncMock, return_value=size_result
        ):
            outcome = await service._check_database(db_session)

        assert outcome.status == HealthStatus.UNHEALTHY
        assert outcome.message == "Database storage issue"
        assert "vacuum" in outcome.actionable_guidance.lower()


class TestDatabaseExtendedChecks:
    """Tests for the fuller database health suite."""

    @staticmethod
    def _scalar_result(value: object) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none.return_value = value
        return result

    @pytest.mark.asyncio
    async def test_integrity_check_flags_unhealthy_result(self, settings: MagicMock) -> None:
        session = TestDatabaseSizeCheck._make_session_with_url(
            "sqlite+aiosqlite:////data/pullbox.db"
        )
        session.execute = AsyncMock(
            return_value=self._scalar_result("row 17 missing from index ix_series_title_year")
        )
        service = _make_service(settings)

        result = await service._check_db_integrity(session)

        assert result is not None
        assert result.status == HealthStatus.UNHEALTHY
        assert result.name == "Integrity check"
        assert "quick_check reported" in result.message

    @pytest.mark.asyncio
    async def test_bloat_check_uses_ratio_and_reclaimable_size(self, settings: MagicMock) -> None:
        session = TestDatabaseSizeCheck._make_session_with_url(
            "sqlite+aiosqlite:////data/pullbox.db"
        )
        session.execute = AsyncMock(
            side_effect=[
                self._scalar_result(10_000),  # page_count
                self._scalar_result(2_000),  # freelist_count (20%)
                self._scalar_result(32_768),  # page_size -> ~62.5 MB reclaimable
            ]
        )
        service = _make_service(settings)

        result = await service._check_db_bloat(session)

        assert result is not None
        assert result.status == HealthStatus.DEGRADED
        assert result.name == "Database bloat"
        assert "20% reclaimable" in result.message


# ---------------------------------------------------------------------------
# Filesystem check
# ---------------------------------------------------------------------------


class TestFilesystemCheck:
    @pytest.mark.asyncio
    async def test_all_healthy(
        self, db_session: AsyncSession, settings: MagicMock, tmp_path
    ) -> None:
        root_path = tmp_path / "comics"
        root_path.mkdir()
        root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
        db_session.add(root)
        await db_session.flush()

        service = _make_service(settings)
        outcomes = await service.run_check(db_session, "filesystem")

        assert len(outcomes) == 1
        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].message == "All paths accessible and writable"
        # Verify sub-check details
        assert "checks" in outcomes[0].details
        assert all(c["status"] == "healthy" for c in outcomes[0].details["checks"])
        assert {c["name"] for c in outcomes[0].details["checks"]} == {
            "Library Root: Comics",
            "Data Directory",
            "Logs Directory",
            "Backup Directory",
        }
        assert len(outcomes[0].sub_checks) == 4

    @pytest.mark.asyncio
    async def test_low_disk_space_does_not_degrade_filesystem_accessibility(
        self, db_session: AsyncSession, settings: MagicMock, tmp_path
    ) -> None:
        root_path = tmp_path / "comics"
        root_path.mkdir()
        root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
        db_session.add(root)
        await db_session.flush()

        mock_usage = MagicMock()
        mock_usage.free = 3 * (1024**3)  # 3 GB — below 5 GB threshold
        mock_usage.total = 100 * (1024**3)
        mock_usage.used = 97 * (1024**3)

        with patch(
            "pullbox.services.health_service.shutil.disk_usage", return_value=mock_usage
        ) as disk_usage:
            service = _make_service(settings)
            outcomes = await service.run_check(db_session, "filesystem")

        disk_usage.assert_not_called()
        assert len(outcomes) == 1
        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].message == "All paths accessible and writable"
        assert all(c["status"] == "healthy" for c in outcomes[0].details["checks"])
        assert all(c["details"]["issue"] == "ok" for c in outcomes[0].details["checks"])

    @pytest.mark.asyncio
    async def test_unhealthy_inaccessible(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        root = LibraryRoot(name="Comics", path="/nonexistent/path", enabled=True)
        db_session.add(root)
        await db_session.flush()

        service = _make_service(settings)
        outcomes = await service.run_check(db_session, "filesystem")

        assert len(outcomes) == 1
        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert "inaccessible" in outcomes[0].message.lower()
        assert any(c["status"] == "unhealthy" for c in outcomes[0].details["checks"])

    @pytest.mark.asyncio
    async def test_unhealthy_when_path_not_writable(
        self, db_session: AsyncSession, settings: MagicMock, tmp_path
    ) -> None:
        root_path = tmp_path / "comics"
        root_path.mkdir()
        root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
        db_session.add(root)
        await db_session.flush()

        with patch(
            "pullbox.services.health_service.tempfile.mkstemp",
            side_effect=PermissionError("read-only"),
        ):
            service = _make_service(settings)
            outcomes = await service.run_check(db_session, "filesystem")

        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert "not writable" in outcomes[0].message.lower()
        assert any("not writable" in c["message"].lower() for c in outcomes[0].details["checks"])


# ---------------------------------------------------------------------------
# ComicVine check
# ---------------------------------------------------------------------------


class TestComicVineCheck:
    @pytest.mark.asyncio
    async def test_healthy(self, db_session: AsyncSession, settings: MagicMock) -> None:
        reg = ProviderRegistry()
        mock_provider = MagicMock()
        mock_provider.test_connection = AsyncMock(
            return_value=ProviderHealthResult(healthy=True, message="OK", response_time_ms=50.0)
        )
        reg.register_metadata_provider("comicvine", mock_provider)

        service = _make_service(settings, registry=reg)
        outcomes = await service.run_check(db_session, "comicvine")
        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].message == "API connected"
        assert "checks" in outcomes[0].details
        assert {check["name"] for check in outcomes[0].details["checks"]} == {
            "API key",
            "API connectivity",
            "API latency",
            "Rate limit",
        }
        assert all(check["status"] == "healthy" for check in outcomes[0].details["checks"])
        assert len(outcomes[0].sub_checks) == 4

    @pytest.mark.asyncio
    async def test_unknown_no_key(self, db_session: AsyncSession) -> None:
        s = MagicMock()
        s.comicvine_api_key = ""  # No key configured
        service = _make_service(s)
        outcomes = await service.run_check(db_session, "comicvine")
        assert outcomes[0].status == HealthStatus.UNKNOWN
        assert outcomes[0].message == "Not configured"
        assert outcomes[0].sub_checks == ()

    @pytest.mark.asyncio
    async def test_unhealthy_connection_fail(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        reg = ProviderRegistry()
        mock_provider = MagicMock()
        mock_provider.test_connection = AsyncMock(
            return_value=ProviderHealthResult(
                healthy=False, message="Connection refused", response_time_ms=0.0
            )
        )
        reg.register_metadata_provider("comicvine", mock_provider)

        service = _make_service(settings, registry=reg)
        outcomes = await service.run_check(db_session, "comicvine")
        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert outcomes[0].message == "API unreachable"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["API key"]["status"] == "healthy"
        assert checks["API connectivity"]["status"] == "unhealthy"
        assert checks["API latency"]["status"] == "unknown"
        assert checks["Rate limit"]["status"] == "unknown"

    @pytest.mark.asyncio
    async def test_unhealthy_invalid_key(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        reg = ProviderRegistry()
        mock_provider = MagicMock()
        mock_provider.test_connection = AsyncMock(
            return_value=ProviderHealthResult(
                healthy=False,
                message="ComicVine API error: invalid key",
                response_time_ms=120.0,
                details={"status_code": "100"},
            )
        )
        reg.register_metadata_provider("comicvine", mock_provider)

        service = _make_service(settings, registry=reg)
        outcomes = await service.run_check(db_session, "comicvine")

        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert outcomes[0].message == "API key invalid"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["API key"]["status"] == "unhealthy"
        assert checks["API connectivity"]["status"] == "healthy"
        assert checks["Rate limit"]["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_degraded_rate_limited(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        reg = ProviderRegistry()
        mock_provider = MagicMock()
        mock_provider.test_connection = AsyncMock(
            return_value=ProviderHealthResult(
                healthy=False,
                message="ComicVine API error: rate limited",
                response_time_ms=220.0,
                details={"status_code": "107"},
            )
        )
        reg.register_metadata_provider("comicvine", mock_provider)

        service = _make_service(settings, registry=reg)
        outcomes = await service.run_check(db_session, "comicvine")

        assert outcomes[0].status == HealthStatus.DEGRADED
        assert outcomes[0].message == "Rate limit reached"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["API key"]["status"] == "healthy"
        assert checks["API connectivity"]["status"] == "healthy"
        assert checks["Rate limit"]["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_degraded_high_latency(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        reg = ProviderRegistry()
        mock_provider = MagicMock()
        mock_provider.test_connection = AsyncMock(
            return_value=ProviderHealthResult(
                healthy=True,
                message="ComicVine API key is valid",
                response_time_ms=2200.0,
            )
        )
        reg.register_metadata_provider("comicvine", mock_provider)

        service = _make_service(settings, registry=reg)
        outcomes = await service.run_check(db_session, "comicvine")

        assert outcomes[0].status == HealthStatus.DEGRADED
        assert outcomes[0].message == "API latency elevated"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["API latency"]["status"] == "degraded"
        assert checks["API latency"]["message"] == "2.2s"


class TestComicVineCheckThrottling:
    """ComicVine health check runs at most every 30 minutes in run_all_checks."""

    @pytest.mark.asyncio
    async def test_skipped_when_recent_result_exists(
        self, db_session: AsyncSession, settings: MagicMock, mock_scheduler: MagicMock
    ) -> None:
        """run_all_checks skips ComicVine if last check is within 30 minutes."""
        from pullbox.models.health import HealthCurrentStatus as HealthCurrentStatusModel

        # Seed a recent ComicVine result (5 minutes ago)
        db_session.add(
            HealthCurrentStatusModel(
                component="comicvine",
                current_key="__summary__",
                check_name="api_connectivity",
                subject_key=None,
                subject_key_norm="",
                status=HealthStatus.HEALTHY,
                message="API connected",
                checked_at=datetime.now(UTC) - timedelta(minutes=5),
                is_summary=True,
            )
        )
        await db_session.flush()

        reg = ProviderRegistry()
        mock_provider = MagicMock()
        mock_provider.test_connection = AsyncMock(
            return_value=ProviderHealthResult(healthy=True, message="OK", response_time_ms=50.0)
        )
        reg.register_metadata_provider("comicvine", mock_provider)

        service = _make_service(settings, registry=reg, scheduler=mock_scheduler)
        outcomes = await service.run_all_checks(db_session)

        # ComicVine provider should NOT have been called
        mock_provider.test_connection.assert_not_called()
        # No new comicvine outcome in this run
        cv_outcomes = [o for o in outcomes if o.component == "comicvine"]
        assert len(cv_outcomes) == 0

    @pytest.mark.asyncio
    async def test_runs_when_no_recent_result(
        self, db_session: AsyncSession, settings: MagicMock, mock_scheduler: MagicMock
    ) -> None:
        """run_all_checks runs ComicVine if last check is older than 30 minutes."""
        from pullbox.models.health import HealthCurrentStatus as HealthCurrentStatusModel

        # Seed an old ComicVine result (45 minutes ago)
        db_session.add(
            HealthCurrentStatusModel(
                component="comicvine",
                current_key="__summary__",
                check_name="api_connectivity",
                subject_key=None,
                subject_key_norm="",
                status=HealthStatus.HEALTHY,
                message="API connected",
                checked_at=datetime.now(UTC) - timedelta(minutes=45),
                is_summary=True,
            )
        )
        await db_session.flush()

        reg = ProviderRegistry()
        mock_provider = MagicMock()
        mock_provider.test_connection = AsyncMock(
            return_value=ProviderHealthResult(healthy=True, message="OK", response_time_ms=50.0)
        )
        reg.register_metadata_provider("comicvine", mock_provider)

        service = _make_service(settings, registry=reg, scheduler=mock_scheduler)
        outcomes = await service.run_all_checks(db_session)

        mock_provider.test_connection.assert_called_once()
        cv_outcomes = [o for o in outcomes if o.component == "comicvine"]
        assert len(cv_outcomes) == 1
        assert cv_outcomes[0].status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_runs_when_never_checked(
        self, db_session: AsyncSession, settings: MagicMock, mock_scheduler: MagicMock
    ) -> None:
        """run_all_checks runs ComicVine if no prior results exist."""
        reg = ProviderRegistry()
        mock_provider = MagicMock()
        mock_provider.test_connection = AsyncMock(
            return_value=ProviderHealthResult(healthy=True, message="OK", response_time_ms=50.0)
        )
        reg.register_metadata_provider("comicvine", mock_provider)

        service = _make_service(settings, registry=reg, scheduler=mock_scheduler)
        outcomes = await service.run_all_checks(db_session)

        mock_provider.test_connection.assert_called_once()
        cv_outcomes = [o for o in outcomes if o.component == "comicvine"]
        assert len(cv_outcomes) == 1

    @pytest.mark.asyncio
    async def test_on_demand_check_always_runs(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        """run_check('comicvine') always runs, regardless of throttle."""
        from pullbox.models.health import HealthCheckResult as HealthCheckResultModel

        # Seed a very recent result
        db_session.add(
            HealthCheckResultModel(
                component="comicvine",
                check_name="api_connectivity",
                status=HealthStatus.HEALTHY,
                message="API connected",
                checked_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        await db_session.flush()

        reg = ProviderRegistry()
        mock_provider = MagicMock()
        mock_provider.test_connection = AsyncMock(
            return_value=ProviderHealthResult(healthy=True, message="OK", response_time_ms=50.0)
        )
        reg.register_metadata_provider("comicvine", mock_provider)

        service = _make_service(settings, registry=reg)
        outcomes = await service.run_check(db_session, "comicvine")

        # On-demand check should still run
        mock_provider.test_connection.assert_called_once()
        assert outcomes[0].status == HealthStatus.HEALTHY


# ---------------------------------------------------------------------------
# Download clients check
# ---------------------------------------------------------------------------


class TestDownloadClientsCheck:
    @staticmethod
    async def _seed_client_config(
        session: AsyncSession,
        *,
        config_id: int,
        name: str,
        client_type: str,
        url: str,
    ) -> None:
        from pullbox.models.client import DownloadClientConfig
        from pullbox.models.download import DownloadClientType

        session.add(
            DownloadClientConfig(
                id=config_id,
                name=name,
                client_type=DownloadClientType(client_type),
                url=url,
                enabled=True,
            )
        )
        await session.flush()

    @pytest.mark.asyncio
    async def test_all_healthy(self, db_session: AsyncSession, settings: MagicMock) -> None:
        reg = ProviderRegistry()
        clients = [
            (1, "SAB Main", "sabnzbd", "http://sab:8080"),
            (2, "qBit Main", "qbittorrent", "http://qbit:8081"),
        ]
        for config_id, name, client_type, url in clients:
            await self._seed_client_config(
                db_session,
                config_id=config_id,
                name=name,
                client_type=client_type,
                url=url,
            )
            client = MagicMock()
            client.name = name
            client.client_type = client_type
            client.test_connection = AsyncMock(
                return_value=ProviderHealthResult(
                    healthy=True,
                    message="Connected",
                    response_time_ms=30.0,
                    details={"version": "4.0.0"},
                )
            )
            client.get_queue = AsyncMock(return_value=[])
            reg.register_download_client(config_id, client)

        service = _make_service(settings, registry=reg)
        outcomes = await service.run_check(db_session, "download_clients")
        assert len(outcomes) == 3
        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].message == "All clients reachable"
        assert len(outcomes[0].details["checks"]) == 2
        assert all(c["status"] == "healthy" for c in outcomes[0].details["checks"])
        assert all(outcome.subject_key is not None for outcome in outcomes[1:])
        assert all(len(outcome.sub_checks) == 4 for outcome in outcomes[1:])

    @pytest.mark.asyncio
    async def test_ready_airdcpp_supervisor_projects_safe_native_health(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        await self._seed_client_config(
            db_session,
            config_id=12,
            name="Dedicated Air",
            client_type="airdcpp",
            url="http://airdcpp-vpn:5600",
        )
        supervisor = MagicMock()
        supervisor.state = AirDcppSupervisorState.READY
        supervisor.health = AirDcppSupervisorHealth(
            state=AirDcppSupervisorState.READY,
            compatible=True,
            api_version=1,
            api_feature_level=10,
            last_ready_at=datetime.now(UTC),
            remote_min_search_interval_seconds=45,
        )
        air_registry = MagicMock()
        air_registry.get.return_value = supervisor

        with patch(
            "pullbox.composition.airdcpp.get_airdcpp_supervisor_registry",
            return_value=air_registry,
        ):
            outcomes = await _make_service(
                settings,
                registry=ProviderRegistry(),
            ).run_check(db_session, "download_clients")

        assert outcomes[0].status is HealthStatus.HEALTHY
        assert outcomes[1].status is HealthStatus.HEALTHY
        assert outcomes[1].message == "AirDC++ search and queue services are ready"
        assert outcomes[1].details["minimum_search_interval_seconds"] == 45
        assert "password" not in str(outcomes[1].details).lower()
        assert "token" not in str(outcomes[1].details).lower()

    @pytest.mark.asyncio
    async def test_all_unhealthy(self, db_session: AsyncSession, settings: MagicMock) -> None:
        reg = ProviderRegistry()
        await self._seed_client_config(
            db_session,
            config_id=1,
            name="SAB Main",
            client_type="sabnzbd",
            url="http://sab:8080",
        )
        client = MagicMock()
        client.name = "SAB Main"
        client.client_type = "sabnzbd"
        client.test_connection = AsyncMock(
            return_value=ProviderHealthResult(
                healthy=False, message="Timeout", response_time_ms=10000.0
            )
        )
        reg.register_download_client(1, client)

        service = _make_service(settings, registry=reg)
        outcomes = await service.run_check(db_session, "download_clients")
        assert len(outcomes) == 2
        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert outcomes[0].message == "All clients unreachable or misconfigured"
        assert outcomes[0].details["checks"][0]["name"] == "SAB Main"
        assert outcomes[0].details["checks"][0]["status"] == "unhealthy"
        assert outcomes[1].message == "Client unreachable"

    @pytest.mark.asyncio
    async def test_degraded_mixed(self, db_session: AsyncSession, settings: MagicMock) -> None:
        reg = ProviderRegistry()
        await self._seed_client_config(
            db_session,
            config_id=1,
            name="SAB Main",
            client_type="sabnzbd",
            url="http://sab:8080",
        )
        await self._seed_client_config(
            db_session,
            config_id=2,
            name="qBit Main",
            client_type="qbittorrent",
            url="http://qbit:8081",
        )
        good = MagicMock()
        good.name = "SAB Main"
        good.client_type = "sabnzbd"
        good.test_connection = AsyncMock(
            return_value=ProviderHealthResult(
                healthy=True,
                message="Connected",
                response_time_ms=30.0,
                details={"version": "4.0.0"},
            )
        )
        good.get_queue = AsyncMock(return_value=[])
        bad = MagicMock()
        bad.name = "qBit Main"
        bad.client_type = "qbittorrent"
        bad.test_connection = AsyncMock(
            return_value=ProviderHealthResult(
                healthy=False, message="Refused", response_time_ms=5.0
            )
        )
        reg.register_download_client(1, good)
        reg.register_download_client(2, bad)

        service = _make_service(settings, registry=reg)
        outcomes = await service.run_check(db_session, "download_clients")
        assert len(outcomes) == 3
        assert outcomes[0].status == HealthStatus.DEGRADED
        assert "1 of 2" in outcomes[0].message
        checks = outcomes[0].details["checks"]
        assert len(checks) == 2
        statuses = {c["name"]: c["status"] for c in checks}
        assert statuses["SAB Main"] == "healthy"
        assert statuses["qBit Main"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_none_configured(self, db_session: AsyncSession, settings: MagicMock) -> None:
        reg = ProviderRegistry()
        service = _make_service(settings, registry=reg)
        outcomes = await service.run_check(db_session, "download_clients")
        assert len(outcomes) == 1
        assert outcomes[0].status == HealthStatus.UNKNOWN
        assert outcomes[0].message == "Not configured"

    @pytest.mark.asyncio
    async def test_direct_provider_and_reachable_artifact_host_are_healthy(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        db_session.add_all(
            [
                DirectProviderConfig(
                    provider_id="pullbox.getcomics",
                    display_name="GetComics",
                    endpoint="http://getcomics:8780",
                    enabled=True,
                    state=DirectProviderState.HEALTHY,
                ),
                DirectHostConfig(
                    host_kind=DirectArtifactHostKind.PIXELDRAIN,
                    enabled=True,
                    reachability_state=DirectHostReachabilityState.REACHABLE,
                ),
            ]
        )
        await db_session.flush()

        outcomes = await _make_service(settings, registry=ProviderRegistry()).run_check(
            db_session,
            "download_clients",
        )

        assert outcomes[0].status is HealthStatus.HEALTHY
        assert outcomes[0].message == "All acquisition routes available"
        assert {outcome.subject_key for outcome in outcomes[1:]} == {
            "direct-provider:1",
            "artifact-host:pixeldrain",
        }
        assert {outcome.subject_label for outcome in outcomes[1:]} == {
            "GetComics",
            "Pixeldrain",
        }

    @pytest.mark.asyncio
    async def test_generic_https_transport_does_not_degrade_direct_acquisition_health(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        generic_host = DirectHostConfig(
            host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
            enabled=True,
            reachability_state=DirectHostReachabilityState.UNAVAILABLE,
        )
        db_session.add_all(
            [
                DirectProviderConfig(
                    provider_id="pullbox.getcomics",
                    display_name="GetComics",
                    endpoint="http://getcomics:8780",
                    enabled=True,
                    state=DirectProviderState.HEALTHY,
                ),
                generic_host,
            ]
        )
        await db_session.flush()

        outcomes = await _make_service(settings, registry=ProviderRegistry()).run_check(
            db_session,
            "download_clients",
        )

        assert outcomes[0].status is HealthStatus.HEALTHY
        assert outcomes[0].message == "All acquisition routes available"
        assert {outcome.subject_key for outcome in outcomes[1:]} == {"direct-provider:1"}
        assert generic_host.reachability_state is DirectHostReachabilityState.NOT_CHECKED

    @pytest.mark.asyncio
    async def test_unreachable_artifact_host_degrades_direct_acquisition_health(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        db_session.add_all(
            [
                DirectProviderConfig(
                    provider_id="pullbox.getcomics",
                    display_name="GetComics",
                    endpoint="http://getcomics:8780",
                    enabled=True,
                    state=DirectProviderState.HEALTHY,
                ),
                DirectHostConfig(
                    host_kind=DirectArtifactHostKind.MEDIAFIRE,
                    enabled=True,
                    reachability_state=DirectHostReachabilityState.NOT_REACHABLE,
                ),
            ]
        )
        await db_session.flush()

        outcomes = await _make_service(settings, registry=ProviderRegistry()).run_check(
            db_session,
            "download_clients",
        )

        assert outcomes[0].status is HealthStatus.DEGRADED
        assert outcomes[0].message == "1 of 2 acquisition route(s) need attention"
        host_outcome = next(
            outcome for outcome in outcomes if outcome.subject_key == "artifact-host:mediafire"
        )
        assert host_outcome.status is HealthStatus.UNHEALTHY
        assert host_outcome.details["host_kind"] == "mediafire"

    @pytest.mark.asyncio
    async def test_untested_artifact_host_degrades_direct_acquisition_health(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        """An enabled but untested route cannot support an all-routes-available claim."""
        db_session.add_all(
            [
                DirectProviderConfig(
                    provider_id="pullbox.getcomics",
                    display_name="GetComics",
                    endpoint="http://getcomics:8780",
                    enabled=True,
                    state=DirectProviderState.HEALTHY,
                ),
                DirectHostConfig(
                    host_kind=DirectArtifactHostKind.MEDIAFIRE,
                    enabled=True,
                    reachability_state=DirectHostReachabilityState.NOT_CHECKED,
                ),
            ]
        )
        await db_session.flush()

        outcomes = await _make_service(settings, registry=ProviderRegistry()).run_check(
            db_session,
            "download_clients",
        )

        assert outcomes[0].status is HealthStatus.DEGRADED
        assert outcomes[0].message == "1 of 2 acquisition route(s) need attention"
        assert outcomes[0].actionable_guidance == (
            "Review Mediafire in Settings > Direct Downloads."
        )

    @pytest.mark.asyncio
    async def test_bootstrap_errors_report_unhealthy(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        reg = ProviderRegistry()
        await self._seed_client_config(
            db_session,
            config_id=7,
            name="Broken SAB",
            client_type="sabnzbd",
            url="http://broken:8080",
        )
        service = _make_service(
            settings,
            registry=reg,
            bootstrap_errors={
                "download_clients": [
                    {
                        "config_id": "7",
                        "name": "Broken SAB",
                        "status": "unhealthy",
                        "message": "Configuration error: saved credentials could not be loaded.",
                    }
                ]
            },
        )
        outcomes = await service.run_check(db_session, "download_clients")
        assert len(outcomes) == 2
        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert outcomes[0].message == "All clients unreachable or misconfigured"
        assert outcomes[0].details["checks"][0]["name"] == "Broken SAB"
        assert outcomes[1].message == "Configuration error"


# ---------------------------------------------------------------------------
# Indexers check
# ---------------------------------------------------------------------------


class TestIndexersCheck:
    """Grouped indexer health checks cover both Prowlarr and saved indexers."""

    async def _seed_indexer_config(
        self,
        db_session: AsyncSession,
        *,
        name: str,
        indexer_type: str,
        url: str,
        enabled: bool = True,
        source: str | None = None,
        prowlarr_indexer_id: int | None = None,
        manager_available: bool = True,
        resolver_enabled: bool = False,
    ) -> None:
        from pullbox.models.indexer import IndexerConfig

        db_session.add(
            IndexerConfig(
                name=name,
                indexer_type=indexer_type,
                url=url,
                api_key="key",
                enabled=enabled,
                source=source,
                prowlarr_indexer_id=prowlarr_indexer_id,
                manager_available=manager_available,
                resolver_enabled=resolver_enabled,
            )
        )
        await db_session.flush()

    async def _seed_prowlarr_config(self, db_session: AsyncSession) -> None:
        from pullbox.models.config import SystemConfig

        db_session.add(SystemConfig(key="prowlarr_url", value="http://prowlarr:9696"))
        db_session.add(SystemConfig(key="prowlarr_api_key", value="api-key"))
        await db_session.flush()

    async def _seed_jackett_config(self, db_session: AsyncSession) -> None:
        from pullbox.models.config import SystemConfig

        db_session.add(SystemConfig(key="jackett_url", value="http://jackett:9117"))
        db_session.add(SystemConfig(key="jackett_api_key", value="api-key"))
        await db_session.flush()

    @pytest.mark.asyncio
    async def test_all_healthy(self, db_session: AsyncSession, settings: MagicMock) -> None:
        from pullbox.models.indexer import IndexerType

        for name, itype in [("NZBGeek", IndexerType.NEWZNAB), ("Torznab1", IndexerType.TORZNAB)]:
            await self._seed_indexer_config(
                db_session,
                name=name,
                indexer_type=itype,
                url="http://fake",
            )

        healthy_result = ProviderHealthResult(
            healthy=True,
            message="OK",
            response_time_ms=80.0,
            details={"categories": "12"},
        )
        service = _make_service(settings)
        with (
            patch(
                "pullbox.providers.indexer.newznab.NewznabIndexer",
                autospec=True,
            ) as mock_newznab,
            patch(
                "pullbox.providers.indexer.torznab.TorznabIndexer",
            ) as mock_torznab,
        ):
            mock_newznab.return_value.test_connection = AsyncMock(return_value=healthy_result)
            mock_newznab.return_value.close = AsyncMock()
            mock_torznab.return_value.test_connection = AsyncMock(return_value=healthy_result)
            mock_torznab.return_value.close = AsyncMock()
            outcomes = await service.run_check(db_session, "indexers")

        assert len(outcomes) == 3
        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].message == "All indexers reachable"
        checks = outcomes[0].details["checks"]
        assert len(checks) == 2
        assert all(c["status"] == "healthy" for c in checks)
        subject_summaries = {outcome.subject_label: outcome for outcome in outcomes[1:]}
        assert set(subject_summaries) == {"NZBGeek", "Torznab1"}
        assert all(len(outcome.sub_checks) == 4 for outcome in subject_summaries.values())

    @pytest.mark.asyncio
    async def test_all_unhealthy(self, db_session: AsyncSession, settings: MagicMock) -> None:
        from pullbox.models.indexer import IndexerType

        await self._seed_indexer_config(
            db_session,
            name="NZBGeek",
            indexer_type=IndexerType.NEWZNAB,
            url="http://fake",
        )

        unhealthy = ProviderHealthResult(
            healthy=False,
            message="401 Unauthorized",
            response_time_ms=100.0,
        )
        service = _make_service(settings)
        with patch(
            "pullbox.providers.indexer.newznab.NewznabIndexer",
            autospec=True,
        ) as mock_newznab:
            mock_newznab.return_value.test_connection = AsyncMock(return_value=unhealthy)
            mock_newznab.return_value.close = AsyncMock()
            outcomes = await service.run_check(db_session, "indexers")

        assert len(outcomes) == 2
        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert outcomes[0].message == "All indexer services unreachable"
        assert outcomes[0].details["checks"][0]["name"] == "NZBGeek"
        assert outcomes[1].message == "Authentication failed"

    @pytest.mark.asyncio
    async def test_manual_torznab_health_check_uses_ranked_resolver_chain(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        from pullbox.models.indexer import IndexerType

        await self._seed_indexer_config(
            db_session,
            name="Challenged Torznab",
            indexer_type=IndexerType.TORZNAB,
            url="https://torznab.example",
            source="manual",
            resolver_enabled=True,
        )
        options = (MagicMock(name="resolver-option"),)
        healthy = ProviderHealthResult(
            healthy=True,
            message="OK",
            response_time_ms=50.0,
            details={"categories": "8"},
        )

        with (
            patch(
                "pullbox.services.direct_resolver_service.build_manual_torznab_resolver_options",
                AsyncMock(return_value=options),
            ) as build_options,
            patch("pullbox.providers.indexer.torznab.TorznabIndexer") as mock_torznab,
        ):
            mock_torznab.return_value.test_connection = AsyncMock(return_value=healthy)
            mock_torznab.return_value.close = AsyncMock()
            outcomes = await _make_service(settings).run_check(db_session, "indexers")

        assert outcomes[0].status == HealthStatus.HEALTHY
        build_options.assert_awaited_once()
        mock_torznab.assert_called_once_with(
            name="Challenged Torznab",
            url="https://torznab.example",
            api_key="key",
            resolver_enabled=True,
            resolver_options=options,
        )

    @pytest.mark.asyncio
    async def test_degraded_mixed(self, db_session: AsyncSession, settings: MagicMock) -> None:
        from pullbox.models.indexer import IndexerType

        await self._seed_indexer_config(
            db_session,
            name="NZBGeek",
            indexer_type=IndexerType.NEWZNAB,
            url="http://fake",
        )
        await self._seed_indexer_config(
            db_session,
            name="BadIndexer",
            indexer_type=IndexerType.TORZNAB,
            url="http://fake2",
        )

        healthy = ProviderHealthResult(
            healthy=True,
            message="OK",
            response_time_ms=80.0,
            details={"categories": "10"},
        )
        unhealthy = ProviderHealthResult(healthy=False, message="Timeout", response_time_ms=10000.0)

        service = _make_service(settings)
        with (
            patch(
                "pullbox.providers.indexer.newznab.NewznabIndexer",
                autospec=True,
            ) as mock_newznab,
            patch(
                "pullbox.providers.indexer.torznab.TorznabIndexer",
            ) as mock_torznab,
        ):
            mock_newznab.return_value.test_connection = AsyncMock(return_value=healthy)
            mock_newznab.return_value.close = AsyncMock()
            mock_torznab.return_value.test_connection = AsyncMock(return_value=unhealthy)
            mock_torznab.return_value.close = AsyncMock()
            outcomes = await service.run_check(db_session, "indexers")

        assert len(outcomes) == 3
        assert outcomes[0].status == HealthStatus.DEGRADED
        assert "1 of 2 service(s)" in outcomes[0].message
        statuses = {c["name"]: c["status"] for c in outcomes[0].details["checks"]}
        assert statuses["NZBGeek"] == "healthy"
        assert statuses["BadIndexer"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_none_configured(self, db_session: AsyncSession, settings: MagicMock) -> None:
        service = _make_service(settings)
        outcomes = await service.run_check(db_session, "indexers")
        assert len(outcomes) == 1
        assert outcomes[0].status == HealthStatus.UNKNOWN
        assert outcomes[0].message == "Not configured"

    @pytest.mark.asyncio
    async def test_disabled_indexers_skipped(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        """Disabled indexers should not be health-checked."""
        from pullbox.models.indexer import IndexerType

        await self._seed_indexer_config(
            db_session,
            name="Disabled",
            indexer_type=IndexerType.NEWZNAB,
            url="http://fake",
            enabled=False,
        )

        service = _make_service(settings)
        outcomes = await service.run_check(db_session, "indexers")
        assert len(outcomes) == 1
        assert outcomes[0].status == HealthStatus.UNKNOWN
        assert outcomes[0].message == "Not configured"

    @pytest.mark.asyncio
    async def test_prowlarr_synced_torznab_checked_individually(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        """Prowlarr-synced Torznab indexers should be tested individually."""
        from pullbox.models.indexer import IndexerType

        await self._seed_indexer_config(
            db_session,
            name="ProwlarrTorznab",
            indexer_type=IndexerType.TORZNAB,
            url="http://prowlarr-proxy/1",
            source="prowlarr",
            prowlarr_indexer_id=1,
        )

        healthy = ProviderHealthResult(
            healthy=True,
            message="OK",
            response_time_ms=50.0,
            details={"categories": "8"},
        )
        service = _make_service(settings)
        with patch(
            "pullbox.providers.indexer.torznab.TorznabIndexer",
        ) as mock_torznab:
            mock_torznab.return_value.test_connection = AsyncMock(return_value=healthy)
            mock_torznab.return_value.close = AsyncMock()
            outcomes = await service.run_check(db_session, "indexers")

        assert len(outcomes) == 2
        assert outcomes[0].status == HealthStatus.HEALTHY
        checks = outcomes[0].details["checks"]
        assert len(checks) == 1
        assert checks[0]["name"] == "ProwlarrTorznab"

    @pytest.mark.asyncio
    async def test_prowlarr_configured_adds_proxy_subject(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        await self._seed_prowlarr_config(db_session)

        healthy = ProviderHealthResult(
            healthy=True,
            message="Prowlarr: 4 indexer(s) configured",
            response_time_ms=240.0,
            details={"indexer_count": "4"},
        )
        service = _make_service(settings)
        with patch(
            "pullbox.providers.indexer.prowlarr.ProwlarrIndexer",
            autospec=True,
        ) as mock_prowlarr:
            mock_prowlarr.return_value.test_connection = AsyncMock(return_value=healthy)
            mock_prowlarr.return_value.close = AsyncMock()
            outcomes = await service.run_check(db_session, "indexers")

        assert len(outcomes) == 2
        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].message == "Prowlarr reachable"
        assert outcomes[1].subject_key == "prowlarr"
        assert outcomes[1].subject_label == "Prowlarr"
        assert len(outcomes[1].sub_checks) == 4

    @pytest.mark.asyncio
    async def test_prowlarr_unavailable_skips_individual_indexer_checks(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        from pullbox.models.indexer import IndexerType

        await self._seed_prowlarr_config(db_session)
        await self._seed_indexer_config(
            db_session,
            name="NZBGeek",
            indexer_type=IndexerType.NEWZNAB,
            url="http://nzbgeek",
            source="prowlarr",
            prowlarr_indexer_id=7,
        )

        unavailable = ProviderHealthResult(
            healthy=False,
            message="Connection refused",
            response_time_ms=0.0,
        )
        service = _make_service(settings)
        with (
            patch(
                "pullbox.providers.indexer.prowlarr.ProwlarrIndexer",
                autospec=True,
            ) as mock_prowlarr,
            patch(
                "pullbox.providers.indexer.newznab.NewznabIndexer",
                autospec=True,
            ) as mock_newznab,
        ):
            mock_prowlarr.return_value.test_connection = AsyncMock(return_value=unavailable)
            mock_prowlarr.return_value.close = AsyncMock()
            outcomes = await service.run_check(db_session, "indexers")

        mock_newznab.return_value.test_connection.assert_not_called()
        assert len(outcomes) == 3
        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert "Prowlarr" in outcomes[0].message
        subject_summaries = {outcome.subject_label: outcome for outcome in outcomes[1:]}
        assert subject_summaries["Prowlarr"].status == HealthStatus.UNHEALTHY
        assert subject_summaries["NZBGeek"].status == HealthStatus.UNKNOWN
        assert subject_summaries["NZBGeek"].message == ("Skipped because Prowlarr is unavailable")

    @pytest.mark.asyncio
    async def test_prowlarr_unavailable_still_checks_independent_jackett_tracker(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        from pullbox.models.indexer import IndexerType

        await self._seed_prowlarr_config(db_session)
        await self._seed_indexer_config(
            db_session,
            name="1337x (Jackett)",
            indexer_type=IndexerType.TORZNAB,
            url="http://jackett:9117/api/v2.0/indexers/1337x/results/torznab",
            source="jackett",
        )

        unavailable = ProviderHealthResult(
            healthy=False,
            message="Connection refused",
            response_time_ms=0.0,
        )
        healthy = ProviderHealthResult(
            healthy=True,
            message="1337x: 8 categories available",
            response_time_ms=80.0,
            details={"categories": "8"},
        )
        service = _make_service(settings)
        with (
            patch(
                "pullbox.providers.indexer.prowlarr.ProwlarrIndexer",
                autospec=True,
            ) as mock_prowlarr,
            patch("pullbox.providers.indexer.torznab.TorznabIndexer") as mock_torznab,
        ):
            mock_prowlarr.return_value.test_connection = AsyncMock(return_value=unavailable)
            mock_prowlarr.return_value.close = AsyncMock()
            mock_torznab.return_value.test_connection = AsyncMock(return_value=healthy)
            mock_torznab.return_value.close = AsyncMock()
            outcomes = await service.run_check(db_session, "indexers")

        assert mock_torznab.return_value.test_connection.await_count == 1
        assert outcomes[0].status == HealthStatus.DEGRADED
        subject_summaries = {outcome.subject_label: outcome for outcome in outcomes[1:]}
        assert subject_summaries["Prowlarr"].status == HealthStatus.UNHEALTHY
        assert subject_summaries["1337x (Jackett)"].status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_jackett_unavailable_skips_only_jackett_managed_indexers(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        from pullbox.models.indexer import IndexerType

        await self._seed_jackett_config(db_session)
        await self._seed_indexer_config(
            db_session,
            name="1337x (Jackett)",
            indexer_type=IndexerType.TORZNAB,
            url="http://jackett:9117/api/v2.0/indexers/1337x/results/torznab",
            source="jackett",
        )
        await self._seed_indexer_config(
            db_session,
            name="Independent Torznab",
            indexer_type=IndexerType.TORZNAB,
            url="http://independent.example/api",
        )

        unavailable = ProviderHealthResult(
            healthy=False,
            message="Connection refused",
            response_time_ms=0.0,
        )
        healthy = ProviderHealthResult(
            healthy=True,
            message="Independent Torznab: 8 categories available",
            response_time_ms=80.0,
            details={"categories": "8"},
        )
        service = _make_service(settings)
        with (
            patch("pullbox.providers.indexer.jackett.JackettClient") as mock_jackett,
            patch("pullbox.providers.indexer.torznab.TorznabIndexer") as mock_torznab,
        ):
            mock_jackett.return_value.test_connection = AsyncMock(return_value=unavailable)
            mock_jackett.return_value.close = AsyncMock()
            mock_torznab.return_value.test_connection = AsyncMock(return_value=healthy)
            mock_torznab.return_value.close = AsyncMock()
            outcomes = await service.run_check(db_session, "indexers")

        mock_torznab.return_value.test_connection.assert_awaited_once()
        assert outcomes[0].status == HealthStatus.DEGRADED
        assert outcomes[0].message == "1 of 3 service(s) need attention"
        subject_summaries = {outcome.subject_label: outcome for outcome in outcomes[1:]}
        assert subject_summaries["Jackett"].status == HealthStatus.UNHEALTHY
        assert subject_summaries["1337x (Jackett)"].status == HealthStatus.UNKNOWN
        assert subject_summaries["1337x (Jackett)"].message == (
            "Skipped because Jackett is unavailable"
        )
        assert subject_summaries["Independent Torznab"].status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_jackett_healthy_records_proxy_and_managed_indexer_health(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        from pullbox.models.indexer import IndexerType

        await self._seed_jackett_config(db_session)
        await self._seed_indexer_config(
            db_session,
            name="1337x (Jackett)",
            indexer_type=IndexerType.TORZNAB,
            url="http://jackett:9117/api/v2.0/indexers/1337x/results/torznab",
            source="jackett",
        )
        jackett_healthy = ProviderHealthResult(
            healthy=True,
            message="Jackett: 1 configured tracker(s)",
            response_time_ms=70.0,
            details={"indexer_count": "1"},
        )
        tracker_healthy = ProviderHealthResult(
            healthy=True,
            message="1337x: 8 categories available",
            response_time_ms=80.0,
            details={"categories": "8"},
        )
        service = _make_service(settings)
        with (
            patch("pullbox.providers.indexer.jackett.JackettClient") as mock_jackett,
            patch("pullbox.providers.indexer.torznab.TorznabIndexer") as mock_torznab,
        ):
            mock_jackett.return_value.test_connection = AsyncMock(return_value=jackett_healthy)
            mock_jackett.return_value.close = AsyncMock()
            mock_torznab.return_value.test_connection = AsyncMock(return_value=tracker_healthy)
            mock_torznab.return_value.close = AsyncMock()
            outcomes = await service.run_check(db_session, "indexers")

        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].message == "Jackett and all indexers reachable"
        subject_summaries = {outcome.subject_label: outcome for outcome in outcomes[1:]}
        assert subject_summaries["Jackett"].details["indexer_count"] == 1
        assert subject_summaries["1337x (Jackett)"].status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_jackett_outage_is_degraded_when_prowlarr_remains_healthy(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        from pullbox.models.indexer import IndexerType

        await self._seed_prowlarr_config(db_session)
        await self._seed_jackett_config(db_session)
        await self._seed_indexer_config(
            db_session,
            name="NZBGeek (Prowlarr)",
            indexer_type=IndexerType.NEWZNAB,
            url="http://prowlarr:9696/api/v1/indexer/1",
            source="prowlarr",
        )
        await self._seed_indexer_config(
            db_session,
            name="1337x (Jackett)",
            indexer_type=IndexerType.TORZNAB,
            url="http://jackett:9117/api/v2.0/indexers/1337x/results/torznab",
            source="jackett",
        )
        prowlarr_healthy = ProviderHealthResult(
            healthy=True,
            message="Prowlarr: 1 indexer(s) configured",
            response_time_ms=70.0,
            details={"indexer_count": "1"},
        )
        jackett_unavailable = ProviderHealthResult(
            healthy=False,
            message="Connection refused",
            response_time_ms=0.0,
        )
        indexer_healthy = ProviderHealthResult(
            healthy=True,
            message="NZBGeek: 8 categories available",
            response_time_ms=80.0,
            details={"categories": "8"},
        )
        service = _make_service(settings)
        with (
            patch(
                "pullbox.providers.indexer.prowlarr.ProwlarrIndexer",
                autospec=True,
            ) as mock_prowlarr,
            patch("pullbox.providers.indexer.jackett.JackettClient") as mock_jackett,
            patch("pullbox.providers.indexer.newznab.NewznabIndexer") as mock_newznab,
        ):
            mock_prowlarr.return_value.test_connection = AsyncMock(return_value=prowlarr_healthy)
            mock_prowlarr.return_value.close = AsyncMock()
            mock_jackett.return_value.test_connection = AsyncMock(return_value=jackett_unavailable)
            mock_jackett.return_value.close = AsyncMock()
            mock_newznab.return_value.test_connection = AsyncMock(return_value=indexer_healthy)
            mock_newznab.return_value.close = AsyncMock()
            outcomes = await service.run_check(db_session, "indexers")

        assert outcomes[0].status == HealthStatus.DEGRADED
        assert outcomes[0].message == "1 of 4 service(s) need attention"

    @pytest.mark.asyncio
    async def test_retired_manager_tracker_is_not_health_checked(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        from pullbox.models.indexer import IndexerType

        await self._seed_indexer_config(
            db_session,
            name="Retired (Jackett)",
            indexer_type=IndexerType.TORZNAB,
            url="http://jackett:9117/api/v2.0/indexers/retired/results/torznab",
            source="jackett",
            manager_available=False,
        )

        outcomes = await _make_service(settings).run_check(db_session, "indexers")

        assert len(outcomes) == 1
        assert outcomes[0].status == HealthStatus.UNKNOWN
        assert outcomes[0].message == "Not configured"

    @pytest.mark.asyncio
    async def test_prowlarr_latency_degraded_still_runs_individual_indexer_checks(
        self,
        db_session: AsyncSession,
        settings: MagicMock,
    ) -> None:
        from pullbox.models.indexer import IndexerType

        await self._seed_prowlarr_config(db_session)
        await self._seed_indexer_config(
            db_session,
            name="NZBGeek",
            indexer_type=IndexerType.NEWZNAB,
            url="http://nzbgeek",
        )

        prowlarr_slow = ProviderHealthResult(
            healthy=True,
            message="Prowlarr: 4 indexer(s) configured",
            response_time_ms=2000.0,
            details={"indexer_count": "4"},
        )
        healthy = ProviderHealthResult(
            healthy=True,
            message="NZBGeek: 12 categories available",
            response_time_ms=80.0,
            details={"categories": "12"},
        )
        service = _make_service(settings)
        with (
            patch(
                "pullbox.providers.indexer.prowlarr.ProwlarrIndexer",
                autospec=True,
            ) as mock_prowlarr,
            patch(
                "pullbox.providers.indexer.newznab.NewznabIndexer",
                autospec=True,
            ) as mock_newznab,
        ):
            mock_prowlarr.return_value.test_connection = AsyncMock(return_value=prowlarr_slow)
            mock_prowlarr.return_value.close = AsyncMock()
            mock_newznab.return_value.test_connection = AsyncMock(return_value=healthy)
            mock_newznab.return_value.close = AsyncMock()
            outcomes = await service.run_check(db_session, "indexers")

        mock_newznab.return_value.test_connection.assert_awaited_once()
        assert len(outcomes) == 3
        assert outcomes[0].status == HealthStatus.DEGRADED
        subject_summaries = {outcome.subject_label: outcome for outcome in outcomes[1:]}
        assert subject_summaries["Prowlarr"].status == HealthStatus.DEGRADED
        assert subject_summaries["NZBGeek"].status == HealthStatus.HEALTHY


# ---------------------------------------------------------------------------
# Scheduler check
# ---------------------------------------------------------------------------


class TestSchedulerCheck:
    @pytest.mark.asyncio
    async def test_healthy(
        self, db_session: AsyncSession, settings: MagicMock, mock_scheduler: MagicMock
    ) -> None:
        service = _make_service(settings, scheduler=mock_scheduler)
        outcomes = await service.run_check(db_session, "scheduler")
        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].message == "Running normally"
        assert "checks" in outcomes[0].details
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["Scheduler runtime"]["status"] == "healthy"
        assert checks["Failed tasks"]["status"] == "healthy"
        assert checks["Missed executions"]["status"] == "healthy"
        assert checks["Overlap skips"]["status"] == "healthy"
        assert checks["Stuck tasks"]["status"] == "healthy"
        assert checks["Overdue tasks"]["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_unhealthy_not_running(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        sched = MagicMock()
        sched.running = False
        service = _make_service(settings, scheduler=sched)
        outcomes = await service.run_check(db_session, "scheduler")
        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert outcomes[0].message == "Not running"

    @pytest.mark.asyncio
    async def test_degraded_failed_task(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        sched = MagicMock()
        sched.running = True
        sched.get_jobs.return_value = [{"id": "refresh_metadata"}]
        sched.get_scheduled_tasks.return_value = [
            {
                "task_id": "refresh_metadata",
                "name": "Refresh Metadata",
                "last_status": "failed",
                "last_execution": datetime.now(UTC).isoformat(),
                "next_run_time": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "is_running": False,
            }
        ]

        service = _make_service(settings, scheduler=sched)
        outcomes = await service.run_check(db_session, "scheduler")

        assert outcomes[0].status == HealthStatus.DEGRADED
        assert outcomes[0].message == "Failed tasks detected"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["Failed tasks"]["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_running_task_does_not_report_previous_failure(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        sched = MagicMock()
        sched.running = True
        sched.get_jobs.return_value = [{"id": "run_health_checks"}]
        sched.get_scheduled_tasks.return_value = [
            {
                "task_id": "run_health_checks",
                "name": "Check Health",
                "last_status": "failed",
                "last_execution": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                "next_run_time": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "is_running": True,
                "running_since": datetime.now(UTC).isoformat(),
            }
        ]

        service = _make_service(settings, scheduler=sched)
        outcomes = await service.run_check(db_session, "scheduler")

        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].message == "Running normally"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["Failed tasks"]["status"] == "healthy"
        assert checks["Failed tasks"]["message"] == "No failed tasks"

    @pytest.mark.asyncio
    async def test_degraded_overdue_task(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        sched = MagicMock()
        sched.running = True
        sched.get_jobs.return_value = [{"id": "test_task"}]

        # Simulate a task that ran 2 hours ago but should run every 30 min
        now = datetime.now(UTC)
        last = (now - timedelta(hours=2)).isoformat()
        next_run = (now - timedelta(hours=2) + timedelta(minutes=30)).isoformat()

        sched.get_scheduled_tasks.return_value = [
            {
                "task_id": "overdue_task",
                "name": "Overdue Task",
                "last_execution": last,
                "next_run_time": next_run,
            }
        ]

        service = _make_service(settings, scheduler=sched)
        outcomes = await service.run_check(db_session, "scheduler")
        assert outcomes[0].status == HealthStatus.DEGRADED
        assert outcomes[0].message == "Overdue tasks detected"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["Overdue tasks"]["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_degraded_recent_missed_execution(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        sched = MagicMock()
        sched.running = True
        sched.get_jobs.return_value = [{"id": "search_wanted"}]
        sched.get_scheduled_tasks.return_value = [
            {
                "task_id": "search_wanted",
                "name": "Search Wanted",
                "last_missed_at": datetime.now(UTC).isoformat(),
                "missed_count": 2,
                "is_running": False,
            }
        ]

        service = _make_service(settings, scheduler=sched)
        outcomes = await service.run_check(db_session, "scheduler")

        assert outcomes[0].status == HealthStatus.DEGRADED
        assert outcomes[0].message == "Missed executions detected"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["Missed executions"]["status"] == "degraded"
        assert checks["Missed executions"]["message"] == "Search Wanted"
        assert "2 miss" not in checks["Missed executions"]["message"]

    @pytest.mark.asyncio
    async def test_recent_missed_execution_clears_after_later_success(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        sched = MagicMock()
        sched.running = True
        sched.get_jobs.return_value = [{"id": "refresh_dashboard_intelligence"}]
        now = datetime.now(UTC)
        sched.get_scheduled_tasks.return_value = [
            {
                "task_id": "refresh_dashboard_intelligence",
                "name": "Refresh Dashboard Intelligence",
                "last_missed_at": (now - timedelta(hours=1)).isoformat(),
                "missed_count": 10,
                "last_execution": now.isoformat(),
                "next_run_time": (now + timedelta(hours=1)).isoformat(),
                "is_running": False,
            }
        ]

        service = _make_service(settings, scheduler=sched)
        outcomes = await service.run_check(db_session, "scheduler")

        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].message == "Running normally"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["Missed executions"]["status"] == "healthy"
        assert "cleared by later successful runs" in checks["Missed executions"]["message"].lower()
        assert "Refresh Dashboard Intelligence" in checks["Missed executions"]["message"]
        assert "10 miss" not in checks["Missed executions"]["message"]

    @pytest.mark.asyncio
    async def test_degraded_recent_overlap_skip(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        sched = MagicMock()
        sched.running = True
        sched.get_jobs.return_value = [{"id": "monitor_downloads"}]
        sched.get_scheduled_tasks.return_value = [
            {
                "task_id": "monitor_downloads",
                "name": "Monitor Downloads",
                "last_overlap_at": datetime.now(UTC).isoformat(),
                "overlap_count": 3,
                "is_running": False,
            }
        ]

        service = _make_service(settings, scheduler=sched)
        outcomes = await service.run_check(db_session, "scheduler")

        assert outcomes[0].status == HealthStatus.DEGRADED
        assert outcomes[0].message == "Overlap skips detected"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["Overlap skips"]["status"] == "degraded"
        assert checks["Overlap skips"]["message"] == "Monitor Downloads"
        assert "3 skip" not in checks["Overlap skips"]["message"]

    @pytest.mark.asyncio
    async def test_recent_overlap_skip_clears_after_later_success(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        sched = MagicMock()
        sched.running = True
        sched.get_jobs.return_value = [{"id": "monitor_downloads"}]
        now = datetime.now(UTC)
        sched.get_scheduled_tasks.return_value = [
            {
                "task_id": "monitor_downloads",
                "name": "Monitor Downloads",
                "last_overlap_at": (now - timedelta(minutes=30)).isoformat(),
                "overlap_count": 9,
                "last_execution": now.isoformat(),
                "next_run_time": (now + timedelta(minutes=1)).isoformat(),
                "is_running": False,
            }
        ]

        service = _make_service(settings, scheduler=sched)
        outcomes = await service.run_check(db_session, "scheduler")

        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].message == "Running normally"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["Overlap skips"]["status"] == "healthy"
        assert "cleared by later successful runs" in checks["Overlap skips"]["message"].lower()
        assert "Monitor Downloads" in checks["Overlap skips"]["message"]
        assert "9 skip" not in checks["Overlap skips"]["message"]

    @pytest.mark.asyncio
    async def test_degraded_recent_exclusive_task_block(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        sched = MagicMock()
        sched.running = True
        sched.get_jobs.return_value = [{"id": "refresh_dashboard_intelligence"}]
        sched.get_scheduled_tasks.return_value = [
            {
                "task_id": "refresh_dashboard_intelligence",
                "name": "Refresh Dashboard Intelligence",
                "last_exclusive_block_at": datetime.now(UTC).isoformat(),
                "exclusive_block_count": 1,
                "is_running": False,
            }
        ]

        service = _make_service(settings, scheduler=sched)
        outcomes = await service.run_check(db_session, "scheduler")

        assert outcomes[0].status == HealthStatus.DEGRADED
        assert outcomes[0].message == "Exclusive task blocks detected"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["Exclusive task blocks"]["status"] == "degraded"
        assert checks["Exclusive task blocks"]["message"] == "Refresh Dashboard Intelligence"
        assert "1 block" not in checks["Exclusive task blocks"]["message"]

    @pytest.mark.asyncio
    async def test_recent_exclusive_task_block_clears_after_later_success(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        sched = MagicMock()
        sched.running = True
        sched.get_jobs.return_value = [{"id": "refresh_dashboard_intelligence"}]
        now = datetime.now(UTC)
        sched.get_scheduled_tasks.return_value = [
            {
                "task_id": "refresh_dashboard_intelligence",
                "name": "Refresh Dashboard Intelligence",
                "last_exclusive_block_at": (now - timedelta(minutes=30)).isoformat(),
                "exclusive_block_count": 4,
                "last_execution": now.isoformat(),
                "next_run_time": (now + timedelta(hours=1)).isoformat(),
                "is_running": False,
            }
        ]

        service = _make_service(settings, scheduler=sched)
        outcomes = await service.run_check(db_session, "scheduler")

        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].message == "Running normally"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["Exclusive task blocks"]["status"] == "healthy"
        assert (
            "cleared by later successful runs" in checks["Exclusive task blocks"]["message"].lower()
        )
        assert "Refresh Dashboard Intelligence" in checks["Exclusive task blocks"]["message"]
        assert "4 block" not in checks["Exclusive task blocks"]["message"]

    @pytest.mark.asyncio
    async def test_unhealthy_stuck_task(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        sched = MagicMock()
        sched.running = True
        sched.get_jobs.return_value = [{"id": "sync_new_issues"}]
        sched.get_scheduled_tasks.return_value = [
            {
                "task_id": "sync_new_issues",
                "name": "Sync New Issues",
                "is_running": True,
                "running_since": (datetime.now(UTC) - timedelta(minutes=30)).isoformat(),
                "last_duration_seconds": 60.0,
            }
        ]

        service = _make_service(settings, scheduler=sched)
        outcomes = await service.run_check(db_session, "scheduler")

        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert outcomes[0].message == "Stuck tasks detected"
        checks = {check["name"]: check for check in outcomes[0].details["checks"]}
        assert checks["Stuck tasks"]["status"] == "unhealthy"


# ---------------------------------------------------------------------------
# System resources check
# ---------------------------------------------------------------------------


class TestSystemResourcesCheck:
    @pytest.mark.asyncio
    async def test_disk_pressure_uses_library_root(
        self, db_session: AsyncSession, settings: MagicMock, tmp_path
    ) -> None:
        library_root = tmp_path / "comics"
        library_root.mkdir()
        settings.library_root = library_root
        settings.data_dir = tmp_path / "data"

        mock_usage = MagicMock()
        mock_usage.free = 50 * (1024**3)
        mock_usage.total = 100 * (1024**3)
        mock_usage.used = 50 * (1024**3)

        mock_mem = MagicMock()
        mock_mem.percent = 45.0
        mock_mem.available = 8 * (1024**3)
        mock_mem.total = 16 * (1024**3)

        mock_swap = MagicMock()
        mock_swap.percent = 0.0
        mock_swap.used = 0.0
        mock_swap.total = 0.0

        with (
            patch(
                "pullbox.services.health_service.shutil.disk_usage",
                return_value=mock_usage,
            ) as disk_usage,
            patch("pullbox.services.health_service.psutil.virtual_memory", return_value=mock_mem),
            patch("pullbox.services.health_service.psutil.swap_memory", return_value=mock_swap),
            patch(
                "pullbox.services.health_service.psutil.getloadavg",
                return_value=(2.0, 2.0, 2.0),
            ),
            patch("pullbox.services.health_service.psutil.cpu_count", return_value=8),
        ):
            service = _make_service(settings)
            outcomes = await service.run_check(db_session, "system")

        disk_usage.assert_called_once_with(library_root)
        disk_check = {check["name"]: check for check in outcomes[0].details["checks"]}[
            "Disk pressure"
        ]
        assert disk_check["details"]["path"] == str(library_root)
        assert disk_check["details"]["path_source"] == "library_root"

    @pytest.mark.asyncio
    async def test_healthy(self, db_session: AsyncSession, settings: MagicMock) -> None:
        mock_usage = MagicMock()
        mock_usage.free = 50 * (1024**3)
        mock_usage.total = 100 * (1024**3)
        mock_usage.used = 50 * (1024**3)

        mock_mem = MagicMock()
        mock_mem.percent = 45.0
        mock_mem.available = 8 * (1024**3)
        mock_mem.total = 16 * (1024**3)

        mock_swap = MagicMock()
        mock_swap.percent = 0.0
        mock_swap.used = 0.0
        mock_swap.total = 0.0

        with (
            patch("pullbox.services.health_service.shutil.disk_usage", return_value=mock_usage),
            patch("pullbox.services.health_service.psutil.virtual_memory", return_value=mock_mem),
            patch("pullbox.services.health_service.psutil.swap_memory", return_value=mock_swap),
            patch(
                "pullbox.services.health_service.psutil.getloadavg",
                return_value=(2.0, 2.0, 2.0),
            ),
            patch("pullbox.services.health_service.psutil.cpu_count", return_value=8),
        ):
            service = _make_service(settings)
            outcomes = await service.run_check(db_session, "system")

        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].message == "Resources normal"
        checks = outcomes[0].details["checks"]
        assert len(checks) == 4
        names = {c["name"] for c in checks}
        assert "CPU load" in names
        assert "Memory pressure" in names
        assert "Swap pressure" in names
        assert "Disk pressure" in names
        assert all(c["status"] == "healthy" for c in checks)

    @pytest.mark.asyncio
    async def test_degraded_disk(self, db_session: AsyncSession, settings: MagicMock) -> None:
        mock_usage = MagicMock()
        mock_usage.free = 40 * (1024**3)
        mock_usage.total = 100 * (1024**3)
        mock_usage.used = 60 * (1024**3)

        mock_mem = MagicMock()
        mock_mem.percent = 40.0
        mock_mem.available = 8 * (1024**3)
        mock_mem.total = 16 * (1024**3)

        mock_swap = MagicMock()
        mock_swap.percent = 0.0
        mock_swap.used = 0.0
        mock_swap.total = 0.0

        with (
            patch("pullbox.services.health_service.shutil.disk_usage", return_value=mock_usage),
            patch("pullbox.services.health_service.psutil.virtual_memory", return_value=mock_mem),
            patch("pullbox.services.health_service.psutil.swap_memory", return_value=mock_swap),
            patch(
                "pullbox.services.health_service.psutil.getloadavg",
                return_value=(2.0, 2.0, 2.0),
            ),
            patch("pullbox.services.health_service.psutil.cpu_count", return_value=8),
        ):
            service = _make_service(settings)
            outcomes = await service.run_check(db_session, "system")

        assert outcomes[0].status == HealthStatus.DEGRADED
        assert outcomes[0].message == "Resources running hot"
        checks = {c["name"]: c["status"] for c in outcomes[0].details["checks"]}
        assert checks["Disk pressure"] == "degraded"
        assert checks["Memory pressure"] == "healthy"

    @pytest.mark.asyncio
    async def test_high_used_percent_with_adequate_free_space_is_healthy(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        mock_usage = MagicMock()
        mock_usage.free = 65 * (1024**3)
        mock_usage.total = 460 * (1024**3)
        mock_usage.used = 395 * (1024**3)

        mock_mem = MagicMock()
        mock_mem.percent = 40.0
        mock_mem.available = 8 * (1024**3)
        mock_mem.total = 16 * (1024**3)

        mock_swap = MagicMock()
        mock_swap.percent = 0.0
        mock_swap.used = 0.0
        mock_swap.total = 0.0

        with (
            patch("pullbox.services.health_service.shutil.disk_usage", return_value=mock_usage),
            patch("pullbox.services.health_service.psutil.virtual_memory", return_value=mock_mem),
            patch("pullbox.services.health_service.psutil.swap_memory", return_value=mock_swap),
            patch(
                "pullbox.services.health_service.psutil.getloadavg",
                return_value=(2.0, 2.0, 2.0),
            ),
            patch("pullbox.services.health_service.psutil.cpu_count", return_value=8),
        ):
            service = _make_service(settings)
            outcomes = await service.run_check(db_session, "system")

        assert outcomes[0].status == HealthStatus.HEALTHY
        checks = {c["name"]: c for c in outcomes[0].details["checks"]}
        assert checks["Disk pressure"]["status"] == "healthy"
        assert checks["Disk pressure"]["details"]["used_pct"] == 85.9
        assert checks["Disk pressure"]["details"]["free_gb"] == 65.0

    @pytest.mark.asyncio
    async def test_unhealthy_disk(self, db_session: AsyncSession, settings: MagicMock) -> None:
        mock_usage = MagicMock()
        mock_usage.free = 9 * (1024**3)
        mock_usage.total = 460 * (1024**3)
        mock_usage.used = 451 * (1024**3)

        mock_mem = MagicMock()
        mock_mem.percent = 40.0
        mock_mem.available = 8 * (1024**3)
        mock_mem.total = 16 * (1024**3)

        mock_swap = MagicMock()
        mock_swap.percent = 0.0
        mock_swap.used = 0.0
        mock_swap.total = 0.0

        with (
            patch("pullbox.services.health_service.shutil.disk_usage", return_value=mock_usage),
            patch("pullbox.services.health_service.psutil.virtual_memory", return_value=mock_mem),
            patch("pullbox.services.health_service.psutil.swap_memory", return_value=mock_swap),
            patch(
                "pullbox.services.health_service.psutil.getloadavg",
                return_value=(2.0, 2.0, 2.0),
            ),
            patch("pullbox.services.health_service.psutil.cpu_count", return_value=8),
        ):
            service = _make_service(settings)
            outcomes = await service.run_check(db_session, "system")

        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert outcomes[0].message == "Disk pressure critical"
        checks = {c["name"]: c["status"] for c in outcomes[0].details["checks"]}
        assert checks["Disk pressure"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_unhealthy_memory(self, db_session: AsyncSession, settings: MagicMock) -> None:
        mock_usage = MagicMock()
        mock_usage.free = 50 * (1024**3)
        mock_usage.total = 100 * (1024**3)
        mock_usage.used = 50 * (1024**3)

        mock_mem = MagicMock()
        mock_mem.percent = 97.0
        mock_mem.available = 0.5 * (1024**3)
        mock_mem.total = 16 * (1024**3)

        mock_swap = MagicMock()
        mock_swap.percent = 0.0
        mock_swap.used = 0.0
        mock_swap.total = 0.0

        with (
            patch("pullbox.services.health_service.shutil.disk_usage", return_value=mock_usage),
            patch("pullbox.services.health_service.psutil.virtual_memory", return_value=mock_mem),
            patch("pullbox.services.health_service.psutil.swap_memory", return_value=mock_swap),
            patch(
                "pullbox.services.health_service.psutil.getloadavg",
                return_value=(2.0, 2.0, 2.0),
            ),
            patch("pullbox.services.health_service.psutil.cpu_count", return_value=8),
        ):
            service = _make_service(settings)
            outcomes = await service.run_check(db_session, "system")

        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert outcomes[0].message == "Memory pressure critical"
        checks = {c["name"]: c["status"] for c in outcomes[0].details["checks"]}
        assert checks["Disk pressure"] == "healthy"
        assert checks["Memory pressure"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_degraded_cpu_load(self, db_session: AsyncSession, settings: MagicMock) -> None:
        mock_usage = MagicMock()
        mock_usage.free = 50 * (1024**3)
        mock_usage.total = 100 * (1024**3)
        mock_usage.used = 50 * (1024**3)

        mock_mem = MagicMock()
        mock_mem.percent = 45.0
        mock_mem.available = 8 * (1024**3)
        mock_mem.total = 16 * (1024**3)

        mock_swap = MagicMock()
        mock_swap.percent = 0.0
        mock_swap.used = 0.0
        mock_swap.total = 0.0

        with (
            patch("pullbox.services.health_service.shutil.disk_usage", return_value=mock_usage),
            patch("pullbox.services.health_service.psutil.virtual_memory", return_value=mock_mem),
            patch("pullbox.services.health_service.psutil.swap_memory", return_value=mock_swap),
            patch(
                "pullbox.services.health_service.psutil.getloadavg",
                return_value=(8.0, 8.0, 8.0),
            ),
            patch("pullbox.services.health_service.psutil.cpu_count", return_value=8),
        ):
            service = _make_service(settings)
            outcomes = await service.run_check(db_session, "system")

        assert outcomes[0].status == HealthStatus.DEGRADED
        assert outcomes[0].message == "Resources running hot"
        checks = {c["name"]: c["status"] for c in outcomes[0].details["checks"]}
        assert checks["CPU load"] == "degraded"

    @pytest.mark.asyncio
    async def test_degraded_swap_pressure(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        mock_usage = MagicMock()
        mock_usage.free = 50 * (1024**3)
        mock_usage.total = 100 * (1024**3)
        mock_usage.used = 50 * (1024**3)

        mock_mem = MagicMock()
        mock_mem.percent = 45.0
        mock_mem.available = 8 * (1024**3)
        mock_mem.total = 16 * (1024**3)

        mock_swap = MagicMock()
        mock_swap.percent = 30.0
        mock_swap.used = 2 * (1024**3)
        mock_swap.total = 8 * (1024**3)

        with (
            patch("pullbox.services.health_service.shutil.disk_usage", return_value=mock_usage),
            patch("pullbox.services.health_service.psutil.virtual_memory", return_value=mock_mem),
            patch("pullbox.services.health_service.psutil.swap_memory", return_value=mock_swap),
            patch(
                "pullbox.services.health_service.psutil.getloadavg",
                return_value=(2.0, 2.0, 2.0),
            ),
            patch("pullbox.services.health_service.psutil.cpu_count", return_value=8),
        ):
            service = _make_service(settings)
            outcomes = await service.run_check(db_session, "system")

        assert outcomes[0].status == HealthStatus.DEGRADED
        checks = {c["name"]: c["status"] for c in outcomes[0].details["checks"]}
        assert checks["Swap pressure"] == "degraded"


# ---------------------------------------------------------------------------
# Cross-cutting tests
# ---------------------------------------------------------------------------


class TestCrossCutting:
    @pytest.mark.asyncio
    async def test_timeout_returns_unhealthy(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        """A check that exceeds 10s timeout should return UNHEALTHY."""
        service = _make_service(settings)

        async def slow_check() -> CheckOutcome:
            await asyncio.sleep(20)
            return CheckOutcome(
                component="test", check_name="slow", status=HealthStatus.HEALTHY, message="ok"
            )

        outcomes = await service._safe_run(slow_check(), "test", "slow")
        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert "timed out" in outcomes[0].message.lower()

    @pytest.mark.asyncio
    async def test_exception_isolation(self, db_session: AsyncSession, settings: MagicMock) -> None:
        """An exception in one check should not prevent others from running."""
        service = _make_service(settings)

        async def exploding_check() -> CheckOutcome:
            raise RuntimeError("boom")

        outcomes = await service._safe_run(exploding_check(), "test", "explode")
        assert outcomes[0].status == HealthStatus.UNHEALTHY
        assert "boom" in outcomes[0].message.lower()

    @pytest.mark.asyncio
    async def test_actionable_guidance_present(self, db_session: AsyncSession) -> None:
        """Non-healthy outcomes should have non-empty guidance."""
        s = MagicMock()
        s.comicvine_api_key = ""
        service = _make_service(s)
        outcomes = await service.run_check(db_session, "comicvine")
        assert outcomes[0].status != HealthStatus.HEALTHY
        assert outcomes[0].actionable_guidance
        assert len(outcomes[0].actionable_guidance) > 10

    @pytest.mark.asyncio
    async def test_run_all_returns_all_components(
        self, db_session: AsyncSession, settings: MagicMock, mock_scheduler: MagicMock
    ) -> None:
        """run_all_checks should return results for all 7 component types."""
        mock_usage = MagicMock()
        mock_usage.free = 50 * (1024**3)
        mock_usage.total = 100 * (1024**3)
        mock_usage.used = 50 * (1024**3)

        mock_mem = MagicMock()
        mock_mem.percent = 40.0
        mock_mem.available = 8 * (1024**3)
        mock_mem.total = 16 * (1024**3)

        mock_swap = MagicMock()
        mock_swap.percent = 0.0
        mock_swap.used = 0.0
        mock_swap.total = 0.0

        with (
            patch("pullbox.services.health_service.shutil.disk_usage", return_value=mock_usage),
            patch("pullbox.services.health_service.psutil.virtual_memory", return_value=mock_mem),
            patch("pullbox.services.health_service.psutil.swap_memory", return_value=mock_swap),
            patch(
                "pullbox.services.health_service.psutil.getloadavg",
                return_value=(2.0, 2.0, 2.0),
            ),
            patch("pullbox.services.health_service.psutil.cpu_count", return_value=8),
            patch("pathlib.Path.is_dir", return_value=True),
        ):
            service = _make_service(settings, scheduler=mock_scheduler)
            outcomes = await service.run_all_checks(db_session)

        components = {o.component for o in outcomes}
        expected = {
            "database",
            "filesystem",
            "comicvine",
            "download_clients",
            "indexers",
            "scheduler",
            "system",
        }
        assert expected.issubset(components), f"Missing: {expected - components}"

    @pytest.mark.asyncio
    async def test_run_check_dispatches_correctly(
        self, db_session: AsyncSession, settings: MagicMock, mock_scheduler: MagicMock
    ) -> None:
        """run_check should only run the requested component."""
        service = _make_service(settings, scheduler=mock_scheduler)
        outcomes = await service.run_check(db_session, "database")
        assert all(o.component == "database" for o in outcomes)

    @pytest.mark.asyncio
    async def test_run_check_unknown_component(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        """Unknown component name should return UNKNOWN status."""
        service = _make_service(settings)
        outcomes = await service.run_check(db_session, "nonexistent")
        assert outcomes[0].status == HealthStatus.UNKNOWN
        assert "unknown component" in outcomes[0].message.lower()

    @pytest.mark.asyncio
    async def test_response_time_measured(
        self, db_session: AsyncSession, settings: MagicMock
    ) -> None:
        """Every outcome should have a positive response_time_ms."""
        service = _make_service(settings)
        outcomes = await service.run_check(db_session, "database")
        for outcome in outcomes:
            assert outcome.response_time_ms > 0

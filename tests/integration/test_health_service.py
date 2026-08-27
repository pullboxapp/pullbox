"""Integration tests for the health check engine.

Tests run against a real in-memory SQLite database to verify persistence,
history retrieval, cleanup, and overall status aggregation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.health import HealthCheckResult as HealthCheckResultModel
from pullbox.models.health import HealthCurrentStatus as HealthCurrentStatusModel
from pullbox.models.health import HealthIncident as HealthIncidentModel
from pullbox.models.health import HealthStatus
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


def _settings() -> MagicMock:
    s = MagicMock()
    s.comicvine_api_key = ""
    s.data_dir = "/tmp"
    s.logs_dir = None
    s.backup_dir = None
    return s


def _scheduler() -> MagicMock:
    sched = MagicMock()
    sched.running = True
    sched.get_jobs.return_value = [{"id": "run_health_checks"}]
    sched.get_scheduled_tasks.return_value = []
    return sched


# ---------------------------------------------------------------------------
# Persistence tests
# ---------------------------------------------------------------------------


class TestRunAllChecksPersistence:
    @pytest.mark.asyncio
    async def test_run_all_persists_results(self, db_session: AsyncSession) -> None:
        """run_all_checks should write results to the health_check_results table."""
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
            service = HealthService(settings=_settings(), registry=None, scheduler=_scheduler())
            outcomes = await service.run_all_checks(db_session)

        assert len(outcomes) > 0

        # Verify rows in DB
        from sqlalchemy import func, select

        count_stmt = select(func.count(HealthCheckResultModel.id))
        count = (await db_session.execute(count_stmt)).scalar_one()
        summary_count = int(
            (
                await db_session.execute(
                    select(func.count(HealthCheckResultModel.id)).where(
                        HealthCheckResultModel.is_summary.is_(True)
                    )
                )
            ).scalar_one()
            or 0
        )
        subcheck_count = int(
            (
                await db_session.execute(
                    select(func.count(HealthCheckResultModel.id)).where(
                        HealthCheckResultModel.is_summary.is_(False)
                    )
                )
            ).scalar_one()
            or 0
        )
        by_component = {
            component: count
            for component, count in (
                await db_session.execute(
                    select(
                        HealthCheckResultModel.component,
                        func.count(HealthCheckResultModel.id),
                    )
                    .where(HealthCheckResultModel.is_summary.is_(False))
                    .group_by(HealthCheckResultModel.component)
                )
            ).all()
        }
        assert summary_count == len(outcomes)
        assert by_component == {
            "database": 2,
            "filesystem": 1,
            "scheduler": 7,
            "system": 4,
        }
        assert subcheck_count == sum(by_component.values())
        assert count == summary_count + subcheck_count

    @pytest.mark.asyncio
    async def test_run_all_retries_when_persist_hits_sqlite_lock(
        self, db_session: AsyncSession
    ) -> None:
        """run_all_checks should retry persistence after a transient SQLite lock."""
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

        real_flush = AsyncSession.flush
        flush_calls = 0

        async def flaky_flush(session: AsyncSession, *args: object, **kwargs: object) -> None:
            nonlocal flush_calls
            flush_calls += 1
            if session is db_session and flush_calls == 1:
                raise OperationalError(
                    "INSERT INTO health_check_results VALUES (...)",
                    {},
                    Exception("database is locked"),
                )
            await real_flush(session, *args, **kwargs)

        service = HealthService(settings=_settings(), registry=None, scheduler=_scheduler())
        rollback_spy = AsyncMock(wraps=db_session.rollback)
        sleep_spy = AsyncMock()

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
            patch.object(AsyncSession, "flush", autospec=True, side_effect=flaky_flush),
            patch.object(db_session, "rollback", rollback_spy),
            patch("pullbox.services.health_service.asyncio.sleep", sleep_spy),
        ):
            outcomes = await service.run_all_checks(db_session)

        assert len(outcomes) > 0
        assert flush_calls >= 2
        rollback_spy.assert_awaited_once_with()
        sleep_spy.assert_awaited_once_with(0.25)

        rows = (await db_session.execute(select(HealthCheckResultModel))).scalars().all()
        assert rows

    @pytest.mark.asyncio
    async def test_database_check_runs_against_real_sqlite(self, db_session: AsyncSession) -> None:
        """The database check should succeed against the real in-memory SQLite."""
        service = HealthService(settings=_settings())
        outcomes = await service.run_check(db_session, "database")
        assert outcomes[0].status == HealthStatus.HEALTHY
        assert outcomes[0].response_time_ms > 0

    @pytest.mark.asyncio
    async def test_database_run_persists_grouped_summary_and_subchecks(
        self, db_session: AsyncSession
    ) -> None:
        """Database runs should persist one summary row plus grouped sub-check rows."""
        service = HealthService(settings=_settings())

        await service.run_check(db_session, "database")

        rows = (
            (
                await db_session.execute(
                    select(HealthCheckResultModel)
                    .where(HealthCheckResultModel.component == "database")
                    .order_by(
                        HealthCheckResultModel.is_summary.desc(),
                        HealthCheckResultModel.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )

        assert len(rows) == 3
        assert rows[0].is_summary is True
        assert rows[0].check_name == "connectivity"
        assert {row.check_name for row in rows[1:]} == {"connection_round_trip", "query_latency"}
        assert all(row.run_id == rows[0].run_id for row in rows)
        assert rows[0].run_id is not None

    @pytest.mark.asyncio
    async def test_database_run_upserts_current_summary_and_subchecks(
        self, db_session: AsyncSession
    ) -> None:
        """Current health rows should update in place while history keeps samples."""
        service = HealthService(settings=_settings())

        await service.run_check(db_session, "database")
        current_rows = (
            (
                await db_session.execute(
                    select(HealthCurrentStatusModel)
                    .where(HealthCurrentStatusModel.component == "database")
                    .order_by(
                        HealthCurrentStatusModel.is_summary.desc(),
                        HealthCurrentStatusModel.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        first_ids = {row.current_key: row.id for row in current_rows}
        first_run_id = current_rows[0].run_id

        await service.run_check(db_session, "database")
        current_rows = (
            (
                await db_session.execute(
                    select(HealthCurrentStatusModel)
                    .where(HealthCurrentStatusModel.component == "database")
                    .order_by(
                        HealthCurrentStatusModel.is_summary.desc(),
                        HealthCurrentStatusModel.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        history_count = len(
            (
                await db_session.execute(
                    select(HealthCheckResultModel).where(
                        HealthCheckResultModel.component == "database"
                    )
                )
            )
            .scalars()
            .all()
        )

        assert len(current_rows) == 3
        assert {row.current_key: row.id for row in current_rows} == first_ids
        assert current_rows[0].current_key == "__summary__"
        assert {row.check_name for row in current_rows[1:]} == {
            "connection_round_trip",
            "query_latency",
        }
        assert current_rows[0].run_id is not None
        assert current_rows[0].run_id != first_run_id
        assert history_count == 6

    @pytest.mark.asyncio
    async def test_current_health_prunes_retired_subject_and_resolves_incident(
        self,
        db_session: AsyncSession,
    ) -> None:
        await HealthService._persist_outcomes(
            db_session,
            [
                CheckOutcome(
                    component="download_clients",
                    check_name="connectivity",
                    status=HealthStatus.DEGRADED,
                    message="One route needs attention",
                ),
                CheckOutcome(
                    component="download_clients",
                    check_name="artifact_host_summary",
                    status=HealthStatus.UNHEALTHY,
                    message="Artifact host unavailable",
                    subject_key="artifact-host:generic_https",
                    subject_label="Generic HTTPS",
                ),
            ],
        )

        await HealthService._persist_outcomes(
            db_session,
            [
                CheckOutcome(
                    component="download_clients",
                    check_name="connectivity",
                    status=HealthStatus.HEALTHY,
                    message="All acquisition routes available",
                )
            ],
        )

        current_rows = list(
            (
                await db_session.execute(
                    select(HealthCurrentStatusModel).where(
                        HealthCurrentStatusModel.component == "download_clients"
                    )
                )
            )
            .scalars()
            .all()
        )
        incident = (
            await db_session.execute(
                select(HealthIncidentModel).where(
                    HealthIncidentModel.component == "download_clients",
                    HealthIncidentModel.subject_key == "artifact-host:generic_https",
                )
            )
        ).scalar_one()

        assert [(row.subject_key, row.current_key) for row in current_rows] == [
            (None, "__summary__")
        ]
        assert incident.resolved_at is not None

    @pytest.mark.asyncio
    async def test_comicvine_run_persists_grouped_summary_and_subchecks(
        self, db_session: AsyncSession
    ) -> None:
        """ComicVine runs should persist one summary row plus grouped sub-check rows."""
        registry = ProviderRegistry()
        provider = MagicMock()
        provider.test_connection = AsyncMock(
            return_value=ProviderHealthResult(
                healthy=True,
                message="ComicVine API key is valid",
                response_time_ms=140.0,
            )
        )
        registry.register_metadata_provider("comicvine", provider)

        service = HealthService(settings=_settings(), registry=registry)
        await service.run_check(db_session, "comicvine")

        rows = (
            (
                await db_session.execute(
                    select(HealthCheckResultModel)
                    .where(HealthCheckResultModel.component == "comicvine")
                    .order_by(
                        HealthCheckResultModel.is_summary.desc(),
                        HealthCheckResultModel.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )

        assert len(rows) == 5
        assert rows[0].is_summary is True
        assert rows[0].check_name == "api_connectivity"
        assert {row.check_name for row in rows[1:]} == {
            "api_key",
            "api_connectivity",
            "api_latency",
            "rate_limit",
        }
        assert all(row.run_id == rows[0].run_id for row in rows)
        assert rows[0].run_id is not None

    @pytest.mark.asyncio
    async def test_filesystem_run_persists_grouped_summary_and_subchecks(
        self, db_session: AsyncSession, tmp_path
    ) -> None:
        """Filesystem runs should persist one summary row plus grouped path rows."""
        library_root = tmp_path / "library"
        library_root.mkdir()

        from pullbox.models.library import LibraryRoot

        db_session.add(LibraryRoot(name="Comics", path=str(library_root), enabled=True))
        await db_session.flush()

        mock_usage = MagicMock()
        mock_usage.free = 50 * (1024**3)
        mock_usage.total = 100 * (1024**3)
        mock_usage.used = 50 * (1024**3)

        service = HealthService(settings=_settings())
        with patch("pullbox.services.health_service.shutil.disk_usage", return_value=mock_usage):
            await service.run_check(db_session, "filesystem")

        rows = (
            (
                await db_session.execute(
                    select(HealthCheckResultModel)
                    .where(HealthCheckResultModel.component == "filesystem")
                    .order_by(
                        HealthCheckResultModel.is_summary.desc(),
                        HealthCheckResultModel.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )

        assert len(rows) == 3
        assert rows[0].is_summary is True
        assert rows[0].check_name == "accessibility"
        assert {row.check_name for row in rows[1:]} == {"Library Root: Comics", "Data Directory"}
        assert all(row.run_id == rows[0].run_id for row in rows)
        assert rows[0].run_id is not None

    @pytest.mark.asyncio
    async def test_system_run_persists_grouped_summary_and_subchecks(
        self, db_session: AsyncSession
    ) -> None:
        """System resource runs should persist one summary row plus grouped host checks."""
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

        service = HealthService(settings=_settings())
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
            await service.run_check(db_session, "system")

        rows = (
            (
                await db_session.execute(
                    select(HealthCheckResultModel)
                    .where(HealthCheckResultModel.component == "system")
                    .order_by(
                        HealthCheckResultModel.is_summary.desc(),
                        HealthCheckResultModel.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )

        assert len(rows) == 5
        assert rows[0].is_summary is True
        assert rows[0].check_name == "resources"
        assert {row.check_name for row in rows[1:]} == {
            "cpu_load",
            "memory_pressure",
            "swap_pressure",
            "disk_pressure",
        }
        assert all(row.run_id == rows[0].run_id for row in rows)

    @pytest.mark.asyncio
    async def test_download_clients_run_persists_component_and_subject_rows(
        self, db_session: AsyncSession
    ) -> None:
        """Download-client runs should persist component, subject, and sub-check rows."""
        from pullbox.models.client import DownloadClientConfig
        from pullbox.models.download import DownloadClientType

        db_session.add(
            DownloadClientConfig(
                id=5,
                name="SAB Main",
                client_type=DownloadClientType.SABNZBD,
                url="http://sab:8080",
                enabled=True,
            )
        )
        await db_session.flush()

        registry = ProviderRegistry()
        client = MagicMock()
        client.name = "SAB Main"
        client.client_type = "sabnzbd"
        client.test_connection = AsyncMock(
            return_value=ProviderHealthResult(
                healthy=True,
                message="SABnzbd v4.0.0",
                response_time_ms=120.0,
                details={"version": "4.0.0"},
            )
        )
        client.get_queue = AsyncMock(return_value=[])
        registry.register_download_client(5, client)

        service = HealthService(settings=_settings(), registry=registry)
        await service.run_check(db_session, "download_clients")

        rows = (
            (
                await db_session.execute(
                    select(HealthCheckResultModel)
                    .where(HealthCheckResultModel.component == "download_clients")
                    .order_by(
                        HealthCheckResultModel.is_summary.desc(),
                        HealthCheckResultModel.subject_key.asc(),
                        HealthCheckResultModel.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )

        assert len(rows) == 6
        assert rows[0].is_summary is True
        assert rows[0].subject_key is None
        assert rows[0].check_name == "connectivity"
        assert rows[1].is_summary is True
        assert rows[1].subject_key == "5"
        assert rows[1].subject_label == "SAB Main"
        assert rows[1].check_name == "client_summary"
        assert {row.check_name for row in rows[2:]} == {
            "endpoint_reachability",
            "authentication",
            "client_identity",
            "queue_access",
        }
        assert all(row.run_id == rows[0].run_id for row in rows)
        assert rows[0].run_id is not None

    @pytest.mark.asyncio
    async def test_indexers_run_persists_proxy_and_indexer_subject_rows(
        self, db_session: AsyncSession
    ) -> None:
        """Indexer runs should persist component, proxy, and indexer subject rows."""
        from pullbox.models.config import SystemConfig
        from pullbox.models.indexer import IndexerConfig, IndexerType

        db_session.add(SystemConfig(key="prowlarr_url", value="http://prowlarr:9696"))
        db_session.add(SystemConfig(key="prowlarr_api_key", value="api-key"))
        db_session.add(
            IndexerConfig(
                id=7,
                name="NZBGeek",
                indexer_type=IndexerType.NEWZNAB,
                url="http://nzbgeek",
                api_key="key",
                enabled=True,
            )
        )
        await db_session.flush()

        service = HealthService(settings=_settings())
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
            mock_prowlarr.return_value.test_connection = AsyncMock(
                return_value=ProviderHealthResult(
                    healthy=True,
                    message="Prowlarr: 4 indexer(s) configured",
                    response_time_ms=140.0,
                    details={"indexer_count": "4"},
                )
            )
            mock_prowlarr.return_value.close = AsyncMock()
            mock_newznab.return_value.test_connection = AsyncMock(
                return_value=ProviderHealthResult(
                    healthy=True,
                    message="NZBGeek: 12 categories available",
                    response_time_ms=80.0,
                    details={"categories": "12"},
                )
            )
            mock_newznab.return_value.close = AsyncMock()
            await service.run_check(db_session, "indexers")

        rows = (
            (
                await db_session.execute(
                    select(HealthCheckResultModel)
                    .where(HealthCheckResultModel.component == "indexers")
                    .order_by(
                        HealthCheckResultModel.is_summary.desc(),
                        HealthCheckResultModel.subject_key.asc(),
                        HealthCheckResultModel.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )

        assert len(rows) == 11
        assert rows[0].is_summary is True
        assert rows[0].subject_key is None
        assert rows[0].check_name == "connectivity"
        summary_rows = [row for row in rows[1:3] if row.is_summary]
        assert {row.subject_key for row in summary_rows} == {"7", "prowlarr"}
        assert {row.subject_label for row in summary_rows} == {"NZBGeek", "Prowlarr"}
        assert {row.check_name for row in rows[3:]} == {
            "api_connectivity",
            "authentication",
            "capabilities",
            "indexer_registry",
            "endpoint_reachability",
            "latency",
        }
        assert all(row.run_id == rows[0].run_id for row in rows)
        assert rows[0].run_id is not None


# ---------------------------------------------------------------------------
# History tests
# ---------------------------------------------------------------------------


class TestHistory:
    async def _seed_history(
        self, session: AsyncSession, count: int, component: str = "database"
    ) -> None:
        """Insert *count* health check rows for *component*."""
        now = datetime.now(UTC)
        for i in range(count):
            row = HealthCheckResultModel(
                component=component,
                check_name="connectivity",
                status=HealthStatus.HEALTHY,
                message=f"check {i}",
                details_json=json.dumps({"index": str(i)}),
                response_time_ms=float(i),
                checked_at=now - timedelta(minutes=count - i),
            )
            session.add(row)
        await session.flush()

    @pytest.mark.asyncio
    async def test_history_ordered_by_time(self, db_session: AsyncSession) -> None:
        await self._seed_history(db_session, 5)
        history = await HealthService.get_history(db_session)
        assert len(history) == 5
        # Most recent first
        for i in range(len(history) - 1):
            assert history[i].checked_at >= history[i + 1].checked_at

    @pytest.mark.asyncio
    async def test_history_filtered_by_component(self, db_session: AsyncSession) -> None:
        await self._seed_history(db_session, 3, component="database")
        await self._seed_history(db_session, 2, component="comicvine")

        db_history = await HealthService.get_history(db_session, component="database")
        assert len(db_history) == 3
        assert all(h.component == "database" for h in db_history)

    @pytest.mark.asyncio
    async def test_history_respects_limit(self, db_session: AsyncSession) -> None:
        await self._seed_history(db_session, 10)
        history = await HealthService.get_history(db_session, limit=3)
        assert len(history) == 3


# ---------------------------------------------------------------------------
# Incident tests
# ---------------------------------------------------------------------------


class TestHealthIncidents:
    @pytest.mark.asyncio
    async def test_non_healthy_summary_opens_updates_and_resolves_incident(
        self, db_session: AsyncSession
    ) -> None:
        await HealthService._persist_outcomes(
            db_session,
            [
                CheckOutcome(
                    component="database",
                    check_name="connectivity",
                    status=HealthStatus.UNHEALTHY,
                    message="Database unavailable",
                    response_time_ms=0.0,
                )
            ],
        )

        incidents = (
            (
                await db_session.execute(
                    select(HealthIncidentModel).where(HealthIncidentModel.component == "database")
                )
            )
            .scalars()
            .all()
        )
        assert len(incidents) == 1
        assert incidents[0].status == HealthStatus.UNHEALTHY
        assert incidents[0].occurrence_count == 1
        assert incidents[0].resolved_at is None
        first_run_id = incidents[0].last_run_id

        await HealthService._persist_outcomes(
            db_session,
            [
                CheckOutcome(
                    component="database",
                    check_name="connectivity",
                    status=HealthStatus.UNHEALTHY,
                    message="Still unavailable",
                    response_time_ms=0.0,
                )
            ],
        )

        incidents = (
            (
                await db_session.execute(
                    select(HealthIncidentModel).where(HealthIncidentModel.component == "database")
                )
            )
            .scalars()
            .all()
        )
        assert len(incidents) == 1
        assert incidents[0].occurrence_count == 2
        assert incidents[0].last_message == "Still unavailable"
        assert incidents[0].last_run_id != first_run_id

        await HealthService._persist_outcomes(
            db_session,
            [
                CheckOutcome(
                    component="database",
                    check_name="connectivity",
                    status=HealthStatus.HEALTHY,
                    message="Connected",
                    response_time_ms=5.0,
                )
            ],
        )

        incidents = (
            (
                await db_session.execute(
                    select(HealthIncidentModel).where(HealthIncidentModel.component == "database")
                )
            )
            .scalars()
            .all()
        )
        assert len(incidents) == 1
        assert incidents[0].occurrence_count == 2
        assert incidents[0].last_message == "Still unavailable"
        assert incidents[0].resolved_at is not None

    @pytest.mark.asyncio
    async def test_unknown_summary_does_not_open_incident(self, db_session: AsyncSession) -> None:
        await HealthService._persist_outcomes(
            db_session,
            [
                CheckOutcome(
                    component="indexers",
                    check_name="indexer_summary",
                    subject_key="7",
                    subject_label="NZBGeek",
                    status=HealthStatus.UNKNOWN,
                    message="Skipped because Prowlarr is unavailable",
                )
            ],
        )

        incidents = (await db_session.execute(select(HealthIncidentModel))).scalars().all()
        assert incidents == []


# ---------------------------------------------------------------------------
# Cleanup tests
# ---------------------------------------------------------------------------


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_removes_old_records(self, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        # Old record (8 days ago)
        old = HealthCheckResultModel(
            component="database",
            check_name="connectivity",
            status=HealthStatus.HEALTHY,
            message="old",
            checked_at=now - timedelta(days=8),
        )
        # Recent record (1 day ago)
        recent = HealthCheckResultModel(
            component="database",
            check_name="connectivity",
            status=HealthStatus.HEALTHY,
            message="recent",
            checked_at=now - timedelta(days=1),
        )
        db_session.add_all([old, recent])
        await db_session.flush()

        deleted = await HealthService.cleanup_history(db_session, retention_days=7)
        assert deleted == 1

        history = await HealthService.get_history(db_session)
        assert len(history) == 1
        assert history[0].message == "recent"

    @pytest.mark.asyncio
    async def test_cleanup_preserves_recent(self, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        for i in range(5):
            row = HealthCheckResultModel(
                component="database",
                check_name="connectivity",
                status=HealthStatus.HEALTHY,
                message=f"recent-{i}",
                checked_at=now - timedelta(hours=i),
            )
            db_session.add(row)
        await db_session.flush()

        deleted = await HealthService.cleanup_history(db_session, retention_days=7)
        assert deleted == 0

        history = await HealthService.get_history(db_session)
        assert len(history) == 5

    @pytest.mark.asyncio
    async def test_cleanup_returns_count(self, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        for i in range(3):
            row = HealthCheckResultModel(
                component="system",
                check_name="resources",
                status=HealthStatus.HEALTHY,
                message=f"old-{i}",
                checked_at=now - timedelta(days=30 + i),
            )
            db_session.add(row)
        await db_session.flush()

        deleted = await HealthService.cleanup_history(db_session, retention_days=7)
        assert deleted == 3


# ---------------------------------------------------------------------------
# Overall status aggregation
# ---------------------------------------------------------------------------


class TestOverallStatus:
    @staticmethod
    def _current_summary(
        component: str,
        status: HealthStatus,
        *,
        checked_at: datetime,
        message: str = "ok",
    ) -> HealthCurrentStatusModel:
        return HealthCurrentStatusModel(
            component=component,
            current_key="__summary__",
            check_name="check",
            subject_key=None,
            subject_key_norm="",
            status=status,
            message=message,
            checked_at=checked_at,
            is_summary=True,
        )

    @pytest.mark.asyncio
    async def test_overall_status_uses_current_status_table(self, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        db_session.add(
            HealthCheckResultModel(
                component="database",
                check_name="check",
                status=HealthStatus.UNHEALTHY,
                message="stale history",
                checked_at=now,
                is_summary=True,
            )
        )
        db_session.add(
            HealthCurrentStatusModel(
                component="database",
                current_key="__summary__",
                check_name="check",
                subject_key=None,
                subject_key_norm="",
                status=HealthStatus.HEALTHY,
                message="current state",
                checked_at=now,
                is_summary=True,
            )
        )
        await db_session.flush()

        status = await HealthService.get_overall_status(db_session)
        assert status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_all_healthy(self, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        for comp in ["database", "comicvine", "scheduler"]:
            db_session.add(self._current_summary(comp, HealthStatus.HEALTHY, checked_at=now))
        await db_session.flush()

        status = await HealthService.get_overall_status(db_session)
        assert status == HealthStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_one_degraded(self, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        db_session.add(self._current_summary("database", HealthStatus.HEALTHY, checked_at=now))
        db_session.add(
            self._current_summary(
                "filesystem",
                HealthStatus.DEGRADED,
                checked_at=now,
                message="low disk",
            )
        )
        await db_session.flush()

        status = await HealthService.get_overall_status(db_session)
        assert status == HealthStatus.DEGRADED

    @pytest.mark.asyncio
    async def test_one_unhealthy(self, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        db_session.add(self._current_summary("database", HealthStatus.HEALTHY, checked_at=now))
        db_session.add(
            self._current_summary(
                "comicvine",
                HealthStatus.UNHEALTHY,
                checked_at=now,
                message="down",
            )
        )
        await db_session.flush()

        status = await HealthService.get_overall_status(db_session)
        assert status == HealthStatus.UNHEALTHY

    @pytest.mark.asyncio
    async def test_unhealthy_takes_precedence(self, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        db_session.add(
            self._current_summary(
                "database",
                HealthStatus.DEGRADED,
                checked_at=now,
                message="slow",
            )
        )
        db_session.add(
            self._current_summary(
                "comicvine",
                HealthStatus.UNHEALTHY,
                checked_at=now,
                message="down",
            )
        )
        db_session.add(self._current_summary("scheduler", HealthStatus.HEALTHY, checked_at=now))
        await db_session.flush()

        status = await HealthService.get_overall_status(db_session)
        assert status == HealthStatus.UNHEALTHY

    @pytest.mark.asyncio
    async def test_empty_returns_unknown(self, db_session: AsyncSession) -> None:
        status = await HealthService.get_overall_status(db_session)
        assert status == HealthStatus.UNKNOWN

    @pytest.mark.asyncio
    async def test_overall_status_ignores_non_summary_rows(self, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        db_session.add_all(
            [
                HealthCurrentStatusModel(
                    component="database",
                    current_key="__summary__",
                    check_name="connectivity",
                    subject_key=None,
                    subject_key_norm="",
                    status=HealthStatus.HEALTHY,
                    message="ok",
                    checked_at=now,
                    is_summary=True,
                    run_id="run-1",
                ),
                HealthCurrentStatusModel(
                    component="database",
                    current_key="query_latency",
                    check_name="query_latency",
                    subject_key=None,
                    subject_key_norm="",
                    status=HealthStatus.UNHEALTHY,
                    message="slow",
                    checked_at=now,
                    is_summary=False,
                    run_id="run-1",
                ),
            ]
        )
        await db_session.flush()

        status = await HealthService.get_overall_status(db_session)
        assert status == HealthStatus.HEALTHY

"""Application lifespan startup/shutdown contract tests."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from fastapi import FastAPI

if TYPE_CHECKING:
    from pathlib import Path


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeScheduler:
    def __init__(self) -> None:
        self.running = False
        self.overrides: dict[str, Any] | None = None
        self.loaded = False
        self.shutdown_called = False

    def register_tasks(self, *, overrides: dict[str, Any]) -> None:
        self.overrides = overrides

    async def load_persisted_stats(self) -> None:
        self.loaded = True

    def start(self) -> None:
        self.running = True

    def shutdown(self) -> None:
        self.shutdown_called = True
        self.running = False


class _FakeEventBus:
    def __init__(self) -> None:
        self.subscriptions: list[tuple[type[object], object]] = []

    def subscribe(self, event_type: type[object], callback: object) -> None:
        self.subscriptions.append((event_type, callback))


class _FakeImportRunner:
    instances: ClassVar[list[_FakeImportRunner]] = []

    def __init__(self, factory: object) -> None:
        self.factory = factory
        self.recovered = 2
        self.__class__.instances.append(self)

    async def recover_and_dispatch(self) -> int:
        return self.recovered


class _FakeDirectRuntime:
    instances: ClassVar[list[_FakeDirectRuntime]] = []

    def __init__(self) -> None:
        self.closed = False
        self.__class__.instances.append(self)

    async def aclose(self) -> None:
        self.closed = True


class _FakeDirectRunner:
    instances: ClassVar[list[_FakeDirectRunner]] = []
    registered: ClassVar[list[_FakeDirectRunner | None]] = []

    def __init__(self, factory: object, *, executor: _FakeDirectRuntime) -> None:
        self.factory = factory
        self.executor = executor
        self.recovered = 6
        self.closed = False
        self.__class__.instances.append(self)

    async def recover_and_dispatch(self) -> int:
        return self.recovered

    async def aclose(self) -> None:
        self.closed = True
        await self.executor.aclose()


class _FakeAirDcppRegistry:
    instances: ClassVar[list[_FakeAirDcppRegistry]] = []

    def __init__(self) -> None:
        self.stopped = False
        self.__class__.instances.append(self)

    async def stop(self) -> None:
        self.stopped = True


class _FakeQueueManager:
    instances: ClassVar[list[_FakeQueueManager]] = []

    def __init__(self, *, session_factory: object) -> None:
        self.session_factory = session_factory
        self.executors: dict[str, object] = {}
        self.__class__.instances.append(self)

    def register_executor(self, name: str, executor: object) -> None:
        self.executors[name] = executor

    async def recover_and_dispatch(self) -> int:
        return 3


@dataclass(slots=True)
class _UpdateResult:
    update_available: bool
    current_version: str = "0.9.11-dev"
    latest_version: str = "0.9.11"
    release_url: str = "https://example.test/releases/v0.9.11"


class _FakeUpdateCheckService:
    instances: ClassVar[list[_FakeUpdateCheckService]] = []

    def __init__(self, *, cache_ttl_hours: int) -> None:
        self.cache_ttl_hours = cache_ttl_hours
        self.__class__.instances.append(self)

    async def check_for_update(self) -> _UpdateResult:
        return _UpdateResult(update_available=True)


def _settings(tmp_path, *, startup_update_check_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        airdcpp_enabled=True,
        base_url="http://pullbox.test",
        bind_address="0.0.0.0",
        data_dir=tmp_path / "data",
        db_url="sqlite+aiosqlite:///test.db",
        debug=False,
        download_poll_seconds=30,
        health_comicvine_interval_hours=24,
        health_database_interval_minutes=15,
        health_download_clients_interval_hours=24,
        health_filesystem_interval_minutes=15,
        health_indexers_interval_hours=24,
        health_scheduler_interval_minutes=15,
        health_system_interval_minutes=15,
        library_root=tmp_path / "comics",
        logs_dir=tmp_path / "logs",
        port=8585,
        process_completed_interval_seconds=60,
        search_interval_hours=24,
        startup_update_check_enabled=startup_update_check_enabled,
    )


@pytest.fixture
def patched_lifespan(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Patch expensive startup dependencies while preserving lifespan control flow."""
    import pullbox.app as app

    _FakeImportRunner.instances.clear()
    _FakeDirectRuntime.instances.clear()
    _FakeDirectRunner.instances.clear()
    _FakeDirectRunner.registered.clear()
    _FakeAirDcppRegistry.instances.clear()
    _FakeQueueManager.instances.clear()
    _FakeUpdateCheckService.instances.clear()
    app._startup_background_tasks.clear()
    app._update_check_service_ref.clear()

    scheduler = _FakeScheduler()
    event_bus = _FakeEventBus()
    settings = _settings(tmp_path, startup_update_check_enabled=True)

    async def neverending_debug_enforcer() -> None:
        await asyncio.Event().wait()

    async def no_op_async(*_args: object, **_kwargs: object) -> None:
        return None

    async def no_op_count(*_args: object, **_kwargs: object) -> int:
        return 0

    def session_factory() -> _FakeSession:
        return _FakeSession()

    async def load_config_values(_session: object, keys: tuple[str, ...]) -> dict[str, str]:
        defaults = {
            "base_url": "http://configured.test",
            "download_poll_interval_seconds": "45",
            "health_comicvine_interval_hours": "36",
            "health_database_interval_minutes": "10",
            "health_download_clients_interval_hours": "48",
            "health_filesystem_interval_minutes": "20",
            "health_indexers_interval_hours": "12",
            "health_scheduler_interval_minutes": "5",
            "health_system_interval_minutes": "30",
            "instance_name": "Lifecycle Test",
            "process_completed_interval_seconds": "120",
            "search_interval_hours": "18",
        }
        return {key: defaults.get(key, "") for key in keys}

    async def build_import_service(_session: object) -> object:
        return SimpleNamespace(
            recover_pending_comicinfo_enrichment=lambda _factory: asyncio.sleep(0, result=4),
            recover_pending_catalog_hydration=lambda _factory: asyncio.sleep(0, result=5),
        )

    async def start_airdcpp_registry(_factory: object, *, enabled: bool) -> object | None:
        assert enabled is True
        return _FakeAirDcppRegistry()

    monkeypatch.setattr(app, "get_runtime_settings", lambda: settings)
    monkeypatch.setattr(app, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "_run_dev_auto_migrations_if_enabled", no_op_async)
    monkeypatch.setattr(app, "_apply_db_logging_overrides", no_op_async)
    monkeypatch.setattr(app, "_apply_db_utility_logging_overrides", no_op_async)
    monkeypatch.setattr(app, "restore_debug_logging_override_on_startup", no_op_async)
    monkeypatch.setattr(app, "_run_debug_logging_expiry_enforcer", neverending_debug_enforcer)
    monkeypatch.setattr(app, "get_event_bus", lambda: event_bus)
    monkeypatch.setattr(app, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(app, "load_system_config_values", load_config_values)
    monkeypatch.setattr(app, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr(app, "dispose_engine", no_op_async)
    monkeypatch.setattr(
        "pullbox.services.library_service.reconcile_runtime_library_paths",
        lambda _session, _root: asyncio.sleep(0, result={"series": 1}),
    )
    monkeypatch.setattr(
        "pullbox.core.cover_migration.migrate_covers_to_dotcovers",
        lambda _factory: asyncio.sleep(0, result=2),
    )
    monkeypatch.setattr(
        "pullbox.utilities.settings.ensure_utility_directories",
        lambda *_args: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        "pullbox.utilities.settings.cleanup_utility_trash_retention",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "pullbox.utilities.settings.resolve_utility_directory",
        lambda *, default_parent, default_subdir, **_kwargs: default_parent / default_subdir,
    )
    monkeypatch.setattr(
        "pullbox.core.display_time.load_display_settings",
        lambda _session: asyncio.sleep(0, result={"timezone": "UTC"}),
    )
    monkeypatch.setattr("pullbox.tasks.import_task.ImportRunner", _FakeImportRunner)
    monkeypatch.setattr("pullbox.tasks.import_task.set_import_runner", lambda _runner: None)
    monkeypatch.setattr(
        "pullbox.composition.services.build_direct_acquisition_runtime",
        _FakeDirectRuntime,
    )
    monkeypatch.setattr(
        "pullbox.tasks.direct_acquisition_task.DirectAcquisitionRunner",
        _FakeDirectRunner,
    )
    monkeypatch.setattr(
        "pullbox.tasks.direct_acquisition_task.set_direct_acquisition_runner",
        _FakeDirectRunner.registered.append,
    )
    monkeypatch.setattr("pullbox.composition.services.build_import_service", build_import_service)
    monkeypatch.setattr(
        "pullbox.composition.airdcpp.start_airdcpp_supervisor_registry",
        start_airdcpp_registry,
    )
    monkeypatch.setattr(
        "pullbox.services.restore_recovery_service.has_pending_restore_recovery",
        lambda: False,
    )
    monkeypatch.setattr(
        "pullbox.services.restore_recovery_service.run_restore_recovery_if_pending",
        lambda: asyncio.sleep(0, result={"status": "completed"}),
    )
    monkeypatch.setattr(
        "pullbox.tasks.cover_backfill_task.backfill_series_covers",
        lambda *, limit: asyncio.sleep(0, result=limit),
    )
    monkeypatch.setattr(
        "pullbox.services.whats_new_refresh_queue.refresh_whats_new_cache_if_needed",
        no_op_async,
    )
    monkeypatch.setattr("pullbox.utilities.job_queue.JobQueueManager", _FakeQueueManager)
    monkeypatch.setattr("pullbox.utilities.router.set_queue_manager", lambda _queue: None)
    monkeypatch.setattr("pullbox.services.update_check.UpdateCheckService", _FakeUpdateCheckService)

    yield SimpleNamespace(app=app, event_bus=event_bus, scheduler=scheduler, settings=settings)

    app._startup_background_tasks.clear()
    app._update_check_service_ref.clear()


@pytest.mark.asyncio
async def test_lifespan_starts_background_services_and_shuts_down_cleanly(
    patched_lifespan,
) -> None:
    app_module = patched_lifespan.app

    async with app_module.lifespan(FastAPI()):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(patched_lifespan.event_bus.subscriptions) == 4
        assert patched_lifespan.scheduler.loaded is True
        assert patched_lifespan.scheduler.running is True
        assert patched_lifespan.scheduler.overrides is not None
        assert patched_lifespan.scheduler.overrides["monitor_downloads"] == {"seconds": 3}
        assert patched_lifespan.scheduler.overrides["process_completed"] == {"seconds": 300}
        assert _FakeImportRunner.instances
        assert _FakeDirectRunner.instances
        assert _FakeDirectRunner.registered[-1] is _FakeDirectRunner.instances[-1]
        assert _FakeAirDcppRegistry.instances
        assert _FakeQueueManager.instances
        assert set(_FakeQueueManager.instances[-1].executors) == {
            "db_check_cleanup",
            "export_library",
            "file_convert",
            "integrity_check",
            "library_permissions",
            "mass_convert_pipeline",
            "mass_rename",
            "rollback",
        }
        assert app_module.get_update_check_service() is _FakeUpdateCheckService.instances[-1]

    assert patched_lifespan.scheduler.shutdown_called is True
    assert _FakeDirectRunner.instances[-1].closed is True
    assert _FakeDirectRuntime.instances[-1].closed is True
    assert _FakeDirectRunner.registered[-1] is None
    assert _FakeAirDcppRegistry.instances[-1].stopped is True


@pytest.mark.asyncio
async def test_lifespan_runs_restore_recovery_instead_of_cover_backfill(
    patched_lifespan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module = patched_lifespan.app
    calls: list[str] = []
    patched_lifespan.settings.startup_update_check_enabled = False

    monkeypatch.setattr(
        "pullbox.services.restore_recovery_service.has_pending_restore_recovery",
        lambda: True,
    )

    async def recover_restore() -> dict[str, str]:
        calls.append("restore")
        return {"status": "completed"}

    async def backfill(*, limit: int) -> int:
        calls.append(f"backfill:{limit}")
        return limit

    monkeypatch.setattr(
        "pullbox.services.restore_recovery_service.run_restore_recovery_if_pending",
        recover_restore,
    )
    monkeypatch.setattr("pullbox.tasks.cover_backfill_task.backfill_series_covers", backfill)

    async with app_module.lifespan(FastAPI()):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert "restore" in calls
    assert not any(call.startswith("backfill:") for call in calls)
    assert app_module.get_update_check_service() is _FakeUpdateCheckService.instances[-1]


def test_resolve_alembic_ini_prefers_current_working_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import pullbox.app as app

    alembic_dir = tmp_path / "alembic"
    alembic_dir.mkdir()
    alembic_ini = alembic_dir / "alembic.ini"
    alembic_ini.write_text("[alembic]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert app._resolve_alembic_ini() == alembic_ini


def test_resolve_alembic_ini_falls_back_to_package_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import pullbox.app as app

    monkeypatch.chdir(tmp_path)

    resolved = app._resolve_alembic_ini()

    assert resolved.name == "alembic.ini"
    assert resolved.parent.name == "alembic"


def test_run_dev_auto_migrations_sync_reports_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import pullbox.app as app

    alembic_ini = tmp_path / "alembic" / "alembic.ini"
    alembic_ini.parent.mkdir()
    alembic_ini.write_text("[alembic]\n", encoding="utf-8")
    monkeypatch.setattr(app, "_resolve_alembic_ini", lambda: alembic_ini)
    monkeypatch.setattr(
        app.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=2,
            stdout="migration stdout",
            stderr="migration stderr",
        ),
    )

    with pytest.raises(RuntimeError) as exc_info:
        app._run_dev_auto_migrations_sync()

    message = str(exc_info.value)
    assert "Dev auto-migration failed before app startup" in message
    assert "migration stdout" in message
    assert "migration stderr" in message


def test_run_dev_auto_migrations_sync_logs_success_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import pullbox.app as app

    logged: list[dict[str, str]] = []
    alembic_ini = tmp_path / "alembic" / "alembic.ini"
    alembic_ini.parent.mkdir()
    alembic_ini.write_text("[alembic]\n", encoding="utf-8")
    monkeypatch.setattr(app, "_resolve_alembic_ini", lambda: alembic_ini)
    monkeypatch.setattr(
        app.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="upgraded head\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(app.logger, "info", lambda event, **details: logged.append(details))

    app._run_dev_auto_migrations_sync()

    assert logged == [{"output": "upgraded head"}]


@pytest.mark.asyncio
async def test_debug_logging_expiry_enforcer_logs_poll_failures_then_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pullbox.app as app

    warnings: list[str] = []

    async def fail_poll() -> None:
        raise RuntimeError("database unavailable")

    async def cancel_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(app, "_poll_debug_logging_expiry_once", fail_poll)
    monkeypatch.setattr(app.asyncio, "sleep", cancel_sleep)
    monkeypatch.setattr(app.logger, "warning", lambda event, **_details: warnings.append(event))

    with pytest.raises(asyncio.CancelledError):
        await app._run_debug_logging_expiry_enforcer()

    assert warnings == ["debug_logging_expiry_enforcer_failed"]

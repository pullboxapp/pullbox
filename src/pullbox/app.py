"""
Pullbox application factory — creates and configures the FastAPI instance.

The create_app() function is the single entry point used by uvicorn:
    uvicorn pullbox.app:create_app --factory
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urljoin

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

import pullbox
from pullbox.api.middleware import (
    CsrfMiddleware,
    RateLimitMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
    SetupDetectionMiddleware,
)
from pullbox.config import PullboxSettings
from pullbox.core.config_resolver import (
    get_int_setting,
    get_runtime_settings,
    is_container_runtime,
    load_system_config_values,
)
from pullbox.core.events import (
    DownloadCompleted,
    DownloadFailed,
    FileMatched,
    SeriesAdded,
    get_event_bus,
)
from pullbox.core.exceptions import AuthenticationError, PullboxError
from pullbox.core.scheduler import get_scheduler
from pullbox.core.subscribers import (
    on_download_completed,
    on_download_failed,
    on_file_matched,
    on_series_added,
    recover_recent_search_on_add_misses,
)
from pullbox.database import dispose_engine, get_session_factory
from pullbox.logging import configure_logging
from pullbox.services.debug_logging_service import (
    expire_debug_logging_override_if_needed,
    restore_debug_logging_override_on_startup,
)
from pullbox.startup_messages import render_ready_summary

logger = structlog.get_logger(__name__)

AUTH_REDIRECT_HEADER = "X-Pullbox-Auth-Redirect"
AUTH_REDIRECT_PATH = "/login"

STATIC_DIR = Path(__file__).parent / "ui" / "static"
STATIC_ASSET_CACHE_CONTROL = "public, max-age=86400"
_startup_background_tasks: set[asyncio.Task[object]] = set()
_update_check_service_ref: dict[str, object] = {}
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


class PullboxStaticFiles(StaticFiles):
    """Serve packaged app assets with an explicit browser-cache contract."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers.setdefault("Cache-Control", STATIC_ASSET_CACHE_CONTROL)
        return response


def get_update_check_service() -> object | None:
    """Return the update check service instance, or None if not initialized."""
    return _update_check_service_ref.get("instance")


def _resolve_alembic_ini() -> Path:
    """Resolve the Alembic config for dev auto-migration."""
    package_root = Path(__file__).resolve().parents[2]
    candidates = (
        Path.cwd() / "alembic" / "alembic.ini",
        package_root / "alembic" / "alembic.ini",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    msg = "Alembic config not found for dev auto-migration."
    raise RuntimeError(msg)


def _dev_auto_migrate_enabled() -> bool:
    """Return whether app startup should self-apply migrations in dev mode."""
    raw_value = os.environ.get("PULLBOX_DEV_AUTO_MIGRATE", "")
    return raw_value.strip().lower() in _TRUTHY_ENV_VALUES


def _run_dev_auto_migrations_sync() -> None:
    """Run Alembic synchronously for the devserver hot-reload worker."""
    alembic_ini = _resolve_alembic_ini()
    command = [
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(alembic_ini),
        "upgrade",
        "head",
    ]
    result = subprocess.run(
        command,
        cwd=str(alembic_ini.parent.parent),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = (
            "Dev auto-migration failed before app startup.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        raise RuntimeError(msg)
    if result.stdout.strip():
        logger.info("dev_auto_migration_completed", output=result.stdout.strip())


async def _run_dev_auto_migrations_if_enabled() -> None:
    """Apply pending migrations before startup DB reads when devserver requests it."""
    if not _dev_auto_migrate_enabled():
        return
    await asyncio.to_thread(_run_dev_auto_migrations_sync)


async def _apply_db_logging_overrides(settings: PullboxSettings) -> None:
    """Read logging config from the database and reconfigure if different from env defaults.

    The Settings UI saves log_level, log_size_limit_mb, and log_backup_count
    to the system_config table. Runtime log directory comes from bootstrap
    settings and stays read-only in the app.
    """
    from sqlalchemy import select

    from pullbox.models.config import SystemConfig

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(SystemConfig).where(
                SystemConfig.key.in_(["log_level", "log_size_limit_mb", "log_backup_count"])
            )
        )
        db_configs = {row.key: row.value for row in result.scalars().all()}
    db_level = db_configs.get("log_level", "info")
    db_size_mb = int(db_configs.get("log_size_limit_mb", "1"))
    db_backup_count = int(db_configs.get("log_backup_count", "5"))

    configure_logging(
        db_level,
        debug=settings.debug,
        logs_dir=settings.logs_dir,
        log_size_limit_mb=db_size_mb,
        log_backup_count=db_backup_count,
    )
    logger.debug(
        "logging_reconfigured_from_db",
        log_level=db_level,
        logs_dir=str(settings.logs_dir),
        log_size_limit_mb=db_size_mb,
        log_backup_count=db_backup_count,
    )


async def _apply_db_utility_logging_overrides(settings: PullboxSettings) -> None:
    """Apply utility logging overrides from SystemConfig at startup."""
    from sqlalchemy import select

    from pullbox.models.config import DEFAULT_SYSTEM_CONFIG, SystemConfig
    from pullbox.utilities.logging_config import configure_utility_logging_runtime

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(SystemConfig).where(SystemConfig.key == "utility_log_level")
        )
        db_configs = {row.key: row.value for row in result.scalars().all()}

    utility_log_level = db_configs.get(
        "utility_log_level",
        str(DEFAULT_SYSTEM_CONFIG["utility_log_level"][0]),
    )
    configure_utility_logging_runtime(log_dir=settings.logs_dir, level=utility_log_level)
    logger.debug(
        "utility_logging_configured",
        logs_dir=str(settings.logs_dir),
        utility_log_level=utility_log_level.upper(),
    )


async def _poll_debug_logging_expiry_once() -> None:
    """Check once whether a temporary debug-logging override has expired."""
    factory = get_session_factory()
    async with factory() as session:
        await expire_debug_logging_override_if_needed(session, source="timer")


async def _run_debug_logging_expiry_enforcer() -> None:
    """Periodically enforce debug-logging expiry without relying on the UI."""
    while True:
        try:
            await _poll_debug_logging_expiry_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "debug_logging_expiry_enforcer_failed",
                subsystem="debug_logging",
                exc_info=True,
            )
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup and shutdown lifecycle."""
    settings = get_runtime_settings()
    configure_logging(
        "info",
        debug=settings.debug,
        logs_dir=settings.logs_dir,
        log_size_limit_mb=1,
        log_backup_count=5,
    )

    await _run_dev_auto_migrations_if_enabled()

    # Apply any logging overrides saved in the database
    try:
        await _apply_db_logging_overrides(settings)
    except Exception:
        logger.warning("db_logging_override_failed", subsystem="db_logging", exc_info=True)

    # Check for active debug logging override (clear if expired, apply if still valid)
    try:
        await restore_debug_logging_override_on_startup()
    except Exception:
        logger.warning(
            "debug_logging_override_check_failed",
            subsystem="debug_logging",
            exc_info=True,
        )

    debug_logging_task = asyncio.create_task(_run_debug_logging_expiry_enforcer())
    _startup_background_tasks.add(debug_logging_task)
    debug_logging_task.add_done_callback(_startup_background_tasks.discard)

    # Import task modules so @scheduled_task decorators populate the registry
    import pullbox.tasks

    # Wire event subscribers
    event_bus = get_event_bus()
    event_bus.subscribe(DownloadCompleted, on_download_completed)
    event_bus.subscribe(DownloadFailed, on_download_failed)
    event_bus.subscribe(FileMatched, on_file_matched)
    event_bus.subscribe(SeriesAdded, on_series_added)
    logger.debug("event_subscribers_registered", count=4)

    # Reconcile persisted library paths to the active runtime root before any
    # startup task touches tracked series or library files.
    try:
        from pullbox.services.library_service import reconcile_runtime_library_paths

        factory = get_session_factory()
        async with factory() as session:
            reconciliation = await reconcile_runtime_library_paths(session, settings.library_root)
            if reconciliation:
                await session.commit()
                logger.info("library_paths_reconciled_at_startup", **reconciliation)
    except Exception:
        logger.warning("library_path_reconciliation_failed", exc_info=True)

    # One-time migration: move covers from series folders to .covers/
    from pullbox.core.cover_migration import migrate_covers_to_dotcovers

    try:
        factory = get_session_factory()
        migrated = await migrate_covers_to_dotcovers(factory)
        if migrated:
            logger.info("covers_migrated_at_startup", files_moved=migrated)
    except Exception:
        logger.warning("cover_migration_failed", subsystem="cover_migration", exc_info=True)

    # Ensure utility directories (trash and export) exist
    from pullbox.utilities.settings import (
        cleanup_utility_trash_retention,
        ensure_utility_directories,
        resolve_utility_directory,
    )

    try:
        factory = get_session_factory()
        async with factory() as session:
            from sqlalchemy import select

            from pullbox.models.config import SystemConfig

            result = await session.execute(
                select(SystemConfig).where(
                    SystemConfig.key.in_(
                        [
                            "utility_trash_folder",
                            "utility_export_folder",
                            "utility_trash_retention_days",
                        ]
                    )
                )
            )
            util_cfg = {row.key: row.value for row in result.scalars().all()}

        trash_dir = resolve_utility_directory(
            db_value=util_cfg.get("utility_trash_folder", ""),
            default_parent=settings.library_root,
            default_subdir=".trash",
            library_root=settings.library_root,
            data_dir=settings.data_dir,
        )
        export_dir = resolve_utility_directory(
            db_value=util_cfg.get("utility_export_folder", ""),
            default_parent=settings.data_dir,
            default_subdir="exports",
            library_root=settings.library_root,
            data_dir=settings.data_dir,
        )
        await ensure_utility_directories(trash_dir, export_dir)
        retention_days = int(util_cfg.get("utility_trash_retention_days", "30") or "30")
        cleanup_utility_trash_retention(trash_dir, retention_days)
    except Exception:
        logger.warning("utility_directory_creation_failed", subsystem="utility_dirs", exc_info=True)

    try:
        await _apply_db_utility_logging_overrides(settings)
    except Exception:
        logger.warning(
            "utility_logging_configuration_failed",
            subsystem="utility_logging",
            exc_info=True,
        )

    # Warm the display settings cache so _ctx() has real values from first request
    try:
        from pullbox.core.display_time import load_display_settings

        _factory = get_session_factory()
        async with _factory() as ds_session:
            _ds = await load_display_settings(ds_session)
            logger.debug("display_settings_cached", settings=_ds)
    except Exception:
        logger.warning(
            "display_settings_cache_warmup_failed",
            subsystem="display_settings",
            exc_info=True,
        )

    # Warm the instance name and base URL caches
    instance_name_for_summary: str | None = None
    base_url_for_summary: str | None = None
    try:
        _factory = get_session_factory()
        async with _factory() as _cache_session:
            configs = await load_system_config_values(_cache_session, ("instance_name", "base_url"))
            import pullbox.ui.routes as _ui_routes

            _ui_routes._cached_instance_name = configs["instance_name"]
            _ui_routes._cached_base_url = configs["base_url"]
            instance_name_for_summary = configs["instance_name"]
            base_url_for_summary = configs["base_url"]
            logger.debug("instance_name_cached", name=configs["instance_name"])
            logger.debug("base_url_cached", url=configs["base_url"])
    except Exception:
        logger.warning("config_cache_warmup_failed", subsystem="config_cache", exc_info=True)

    logger.info(
        "runtime_fingerprint",
        version=pullbox.__version__,
        instance_name=instance_name_for_summary,
        base_url=base_url_for_summary,
        bind_address=settings.bind_address,
        port=settings.port,
        debug=settings.debug,
        db_url=settings.db_url,
        data_dir=str(settings.data_dir),
        library_root=str(settings.library_root),
        logs_dir=str(settings.logs_dir),
        container_runtime=is_container_runtime(),
    )

    # Recover and resume the dedicated import runner
    from pullbox.tasks.import_task import ImportRunner, set_import_runner

    try:
        factory = get_session_factory()
        import_runner = ImportRunner(factory)
        set_import_runner(import_runner)
        import_runner_task = asyncio.create_task(import_runner.recover_and_dispatch())
        _startup_background_tasks.add(import_runner_task)

        def _cleanup_import_runner_task(task: asyncio.Task[object]) -> None:
            _startup_background_tasks.discard(task)
            with suppress(asyncio.CancelledError):
                exc = task.exception()
                if exc is not None:
                    logger.warning("import_runner_startup_failed", exc_info=exc)
                    return

                recovered = task.result()
                if isinstance(recovered, int) and recovered:
                    logger.info("import_jobs_recovered_at_startup", count=recovered)

        import_runner_task.add_done_callback(_cleanup_import_runner_task)
    except Exception:
        logger.warning("import_recovery_failed", subsystem="import_recovery", exc_info=True)

    # Recover native direct-download attempts. Signed artifact URLs remain
    # ephemeral and are reconstructed by the runner only when work resumes.
    from pullbox.composition.services import build_direct_acquisition_runtime
    from pullbox.tasks.direct_acquisition_task import (
        DirectAcquisitionRunner,
        set_direct_acquisition_runner,
    )

    direct_runner: DirectAcquisitionRunner | None = None
    try:
        factory = get_session_factory()
        direct_runtime = build_direct_acquisition_runtime()
        direct_runner = DirectAcquisitionRunner(factory, executor=direct_runtime)
        set_direct_acquisition_runner(direct_runner)
        direct_recovery_task = asyncio.create_task(direct_runner.recover_and_dispatch())
        _startup_background_tasks.add(direct_recovery_task)

        def _cleanup_direct_recovery_task(task: asyncio.Task[object]) -> None:
            _startup_background_tasks.discard(task)
            with suppress(asyncio.CancelledError):
                exc = task.exception()
                if exc is not None:
                    logger.warning("direct_acquisition_recovery_failed", exc_info=exc)
                    return
                recovered = task.result()
                if isinstance(recovered, int) and recovered:
                    logger.info("direct_acquisitions_recovered_at_startup", count=recovered)

        direct_recovery_task.add_done_callback(_cleanup_direct_recovery_task)
    except Exception:
        set_direct_acquisition_runner(None)
        logger.warning(
            "direct_acquisition_startup_failed",
            subsystem="direct_acquisition",
            exc_info=True,
        )

    # Start exact-client AirDC++ supervisors without waiting for remote I/O.
    # The feature-off path creates no session, pool, socket, or background task.
    from pullbox.composition.airdcpp import start_airdcpp_supervisor_registry

    airdcpp_registry = None
    try:
        airdcpp_registry = await start_airdcpp_supervisor_registry(
            get_session_factory(),
            enabled=settings.airdcpp_enabled,
        )
    except Exception:
        logger.warning(
            "airdcpp_supervisor_startup_failed",
            subsystem="airdcpp",
            exc_info=True,
        )

    # Resume deferred import metadata work from imports that completed before
    # the app stopped. This is background-only so startup stays fast.
    try:
        from pullbox.composition.services import build_import_service

        factory = get_session_factory()

        async def _resume_deferred_import_metadata() -> tuple[int, int]:
            async with factory() as session:
                import_service = await build_import_service(session)
            comicinfo_jobs = await import_service.recover_pending_comicinfo_enrichment(factory)
            hydrated_series = await import_service.recover_pending_catalog_hydration(factory)
            return comicinfo_jobs, hydrated_series

        import_metadata_recovery_task = asyncio.create_task(_resume_deferred_import_metadata())
        _startup_background_tasks.add(import_metadata_recovery_task)

        def _cleanup_import_metadata_recovery_task(task: asyncio.Task[object]) -> None:
            _startup_background_tasks.discard(task)
            with suppress(asyncio.CancelledError):
                exc = task.exception()
                if exc is not None:
                    logger.warning("import_metadata_recovery_failed", exc_info=exc)
                    return

                recovered = task.result()
                if not isinstance(recovered, tuple) or len(recovered) != 2:
                    return
                comicinfo_jobs, hydrated_series = recovered
                if comicinfo_jobs:
                    logger.info("import_comicinfo_recovered_at_startup", jobs=comicinfo_jobs)
                if hydrated_series:
                    logger.info(
                        "import_catalog_hydration_recovered_at_startup",
                        series=hydrated_series,
                    )

        import_metadata_recovery_task.add_done_callback(_cleanup_import_metadata_recovery_task)
    except Exception:
        logger.warning(
            "import_metadata_recovery_startup_failed",
            subsystem="import_recovery",
            exc_info=True,
        )

    # Load scheduler interval overrides from SystemConfig (DB) first,
    # falling back to PullboxSettings (env vars / defaults)
    _interval_keys = [
        "search_interval_hours",
        "download_poll_interval_seconds",
        "process_completed_interval_seconds",
        "health_scheduler_interval_minutes",
        "health_database_interval_minutes",
        "health_filesystem_interval_minutes",
        "health_system_interval_minutes",
        "health_download_clients_interval_hours",
        "health_indexers_interval_hours",
        "health_comicvine_interval_hours",
    ]
    _db_intervals: dict[str, str] = {}
    try:
        _ifactory = get_session_factory()
        async with _ifactory() as _isession:
            _db_intervals = await load_system_config_values(_isession, tuple(_interval_keys))
    except Exception:
        pass  # Fall back to PullboxSettings defaults

    scheduler = get_scheduler()
    _search_hrs = get_int_setting(
        _db_intervals, "search_interval_hours", settings.search_interval_hours
    )
    _dl_poll = get_int_setting(
        _db_intervals, "download_poll_interval_seconds", settings.download_poll_seconds
    )
    _pc_secs = get_int_setting(
        _db_intervals,
        "process_completed_interval_seconds",
        settings.process_completed_interval_seconds,
    )
    _pc_secs = max(300, _pc_secs)
    overrides = {
        "search_wanted": {"hours": _search_hrs},
        "monitor_downloads": {"seconds": _dl_poll},
        "process_completed": {"seconds": _pc_secs},
        "run_scheduler_health_check": {
            "minutes": max(
                1,
                get_int_setting(
                    _db_intervals,
                    "health_scheduler_interval_minutes",
                    settings.health_scheduler_interval_minutes,
                ),
            )
        },
        "run_database_health_check": {
            "minutes": max(
                1,
                get_int_setting(
                    _db_intervals,
                    "health_database_interval_minutes",
                    settings.health_database_interval_minutes,
                ),
            )
        },
        "run_filesystem_health_check": {
            "minutes": max(
                1,
                get_int_setting(
                    _db_intervals,
                    "health_filesystem_interval_minutes",
                    settings.health_filesystem_interval_minutes,
                ),
            )
        },
        "run_system_health_check": {
            "minutes": max(
                1,
                get_int_setting(
                    _db_intervals,
                    "health_system_interval_minutes",
                    settings.health_system_interval_minutes,
                ),
            )
        },
        "run_download_client_health_checks": {
            "hours": max(
                1,
                get_int_setting(
                    _db_intervals,
                    "health_download_clients_interval_hours",
                    settings.health_download_clients_interval_hours,
                ),
            )
        },
        "run_indexer_health_checks": {
            "hours": max(
                1,
                get_int_setting(
                    _db_intervals,
                    "health_indexers_interval_hours",
                    settings.health_indexers_interval_hours,
                ),
            )
        },
        "run_comicvine_health_check": {
            "hours": max(
                1,
                get_int_setting(
                    _db_intervals,
                    "health_comicvine_interval_hours",
                    settings.health_comicvine_interval_hours,
                ),
            )
        },
    }
    scheduler.register_tasks(overrides=overrides)
    await scheduler.load_persisted_stats()
    scheduler.start()
    try:
        from pullbox.tasks.search_task import recover_wanted_search_sweep_schedule

        await recover_wanted_search_sweep_schedule()
    except Exception:
        logger.warning("search_wanted_sweep_recovery_failed", exc_info=True)

    search_on_add_recovery_task = asyncio.create_task(recover_recent_search_on_add_misses())
    _startup_background_tasks.add(search_on_add_recovery_task)

    def _cleanup_search_on_add_recovery_task(task: asyncio.Task[object]) -> None:
        _startup_background_tasks.discard(task)
        with suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                logger.warning("search_on_add_recovery_startup_failed", exc_info=exc)
                return
            recovered = task.result()
            if isinstance(recovered, int) and recovered:
                logger.info("search_on_add_recovered_at_startup", count=recovered)

    search_on_add_recovery_task.add_done_callback(_cleanup_search_on_add_recovery_task)

    from pullbox.services.restore_recovery_service import (
        has_pending_restore_recovery,
        run_restore_recovery_if_pending,
    )

    restore_recovery_pending = has_pending_restore_recovery()
    if restore_recovery_pending:
        restore_recovery_task = asyncio.create_task(run_restore_recovery_if_pending())
        _startup_background_tasks.add(restore_recovery_task)

        def _cleanup_restore_recovery_task(task: asyncio.Task[object]) -> None:
            _startup_background_tasks.discard(task)
            with suppress(asyncio.CancelledError):
                exc = task.exception()
                if exc is not None:
                    logger.warning("restore_recovery_startup_failed", exc_info=exc)
                    return
                result = task.result()
                if isinstance(result, dict):
                    logger.info(
                        "restore_recovery_startup_complete",
                        status=result.get("status"),
                    )

        restore_recovery_task.add_done_callback(_cleanup_restore_recovery_task)
    else:
        from pullbox.tasks.cover_backfill_task import backfill_series_covers

        startup_cover_task = asyncio.create_task(backfill_series_covers(limit=24))
        _startup_background_tasks.add(startup_cover_task)

        def _cleanup_startup_task(task: asyncio.Task[object]) -> None:
            _startup_background_tasks.discard(task)
            with suppress(asyncio.CancelledError):
                exc = task.exception()
                if exc is not None:
                    logger.warning("startup_cover_backfill_failed", exc_info=exc)

        startup_cover_task.add_done_callback(_cleanup_startup_task)

    from pullbox.services.whats_new_refresh_queue import refresh_whats_new_cache_if_needed

    startup_whats_new_task = asyncio.create_task(refresh_whats_new_cache_if_needed())
    _startup_background_tasks.add(startup_whats_new_task)

    def _cleanup_whats_new_startup_task(task: asyncio.Task[object]) -> None:
        _startup_background_tasks.discard(task)
        with suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                logger.warning("whats_new_startup_task_failed", exc_info=exc)

    startup_whats_new_task.add_done_callback(_cleanup_whats_new_startup_task)

    # Initialize the utility job queue manager
    from pullbox.utilities.executors.db_check_cleanup import DBCheckCleanupExecutor
    from pullbox.utilities.executors.export_library import ExportLibraryExecutor
    from pullbox.utilities.executors.file_converter import FileConverterExecutor
    from pullbox.utilities.executors.integrity_checker import IntegrityCheckerExecutor
    from pullbox.utilities.executors.library_permissions import LibraryPermissionsExecutor
    from pullbox.utilities.executors.mass_convert_pipeline import MassConvertPipelineExecutor
    from pullbox.utilities.executors.mass_rename import MassRenameExecutor
    from pullbox.utilities.executors.rollback_executor import RollbackExecutor
    from pullbox.utilities.job_queue import JobQueueManager
    from pullbox.utilities.router import set_queue_manager

    queue_mgr = JobQueueManager(session_factory=get_session_factory())
    queue_mgr.register_executor("file_convert", FileConverterExecutor)
    queue_mgr.register_executor("mass_convert_pipeline", MassConvertPipelineExecutor)
    queue_mgr.register_executor("mass_rename", MassRenameExecutor)
    queue_mgr.register_executor("integrity_check", IntegrityCheckerExecutor)
    queue_mgr.register_executor("library_permissions", LibraryPermissionsExecutor)
    queue_mgr.register_executor("db_check_cleanup", DBCheckCleanupExecutor)
    queue_mgr.register_executor("export_library", ExportLibraryExecutor)
    queue_mgr.register_executor("rollback", RollbackExecutor)
    set_queue_manager(queue_mgr)
    logger.debug("utility_queue_manager_initialized", executor_count=8)

    utility_queue_task = asyncio.create_task(queue_mgr.recover_and_dispatch())
    _startup_background_tasks.add(utility_queue_task)

    def _cleanup_utility_queue_task(task: asyncio.Task[object]) -> None:
        _startup_background_tasks.discard(task)
        with suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                logger.warning("utility_queue_startup_failed", exc_info=exc)
                return

            recovered = task.result()
            if isinstance(recovered, int) and recovered:
                logger.info("utility_jobs_recovered_at_startup", count=recovered)

    utility_queue_task.add_done_callback(_cleanup_utility_queue_task)

    # Initialize update check service — check on startup, then daily
    from pullbox.services.update_check import UpdateCheckService

    update_service = UpdateCheckService(cache_ttl_hours=24)
    _update_check_service_ref["instance"] = update_service

    async def _startup_update_check() -> None:
        try:
            result = await update_service.check_for_update()
            if result and result.update_available:
                logger.info(
                    "update_available",
                    current=result.current_version,
                    latest=result.latest_version,
                    url=result.release_url,
                )
            elif result:
                logger.debug(
                    "update_check_current",
                    version=result.current_version,
                )
        except Exception:
            logger.warning(
                "startup_update_check_failed",
                subsystem="update_check",
                exc_info=True,
            )

    if settings.startup_update_check_enabled:
        startup_update_task = asyncio.create_task(_startup_update_check())
        _startup_background_tasks.add(startup_update_task)
        startup_update_task.add_done_callback(lambda t: _startup_background_tasks.discard(t))
    else:
        logger.debug("startup_update_check_skipped", reason="disabled_by_runtime")

    ready_base_url = (base_url_for_summary or settings.base_url).strip() or settings.base_url
    ready_health_url = urljoin(ready_base_url.rstrip("/") + "/", "ping")
    print(
        render_ready_summary(
            base_url=ready_base_url,
            health_url=ready_health_url,
            scheduler_active=scheduler.running,
        ),
        flush=True,
    )
    logger.info(
        "application_ready",
        instance_name=instance_name_for_summary,
        base_url=ready_base_url,
        health_url=ready_health_url,
        scheduler_active=scheduler.running,
        startup_tasks_pending=len([task for task in _startup_background_tasks if not task.done()]),
    )

    yield

    remaining_startup_tasks = [task for task in _startup_background_tasks if not task.done()]
    for task in remaining_startup_tasks:
        task.cancel()
    if remaining_startup_tasks:
        await asyncio.gather(*remaining_startup_tasks, return_exceptions=True)

    scheduler.shutdown()
    if airdcpp_registry is not None:
        try:
            from pullbox.composition.airdcpp import stop_airdcpp_supervisor_registry

            await stop_airdcpp_supervisor_registry(airdcpp_registry)
        except Exception:
            logger.warning(
                "airdcpp_supervisor_shutdown_failed",
                subsystem="airdcpp",
                exc_info=True,
            )
    if direct_runner is not None:
        try:
            await direct_runner.aclose()
        except Exception:
            logger.warning(
                "direct_acquisition_shutdown_failed",
                subsystem="direct_acquisition",
                exc_info=True,
            )
        finally:
            set_direct_acquisition_runner(None)
    from pullbox.services.operation_progress_dispatch import drain_operation_progress_updates

    await drain_operation_progress_updates()
    await dispose_engine()
    logger.info("application_stopped")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_runtime_settings()

    # Initialize config.xml provider (host-level settings)
    from pullbox.core.config_file import init_config_provider

    init_config_provider(settings.data_dir)

    # Configure logging early so factory-time logs are captured
    configure_logging(
        "info",
        debug=settings.debug,
        logs_dir=settings.logs_dir,
        log_size_limit_mb=1,
        log_backup_count=5,
    )

    app = FastAPI(
        title="Pullbox",
        description="Comic book management and acquisition platform",
        version=pullbox.__version__,
        lifespan=lifespan,
    )
    app.state.db_session_factory = get_session_factory()

    # ── Middleware ──────────────────────────────────────────────────
    # Note: add_middleware wraps in reverse order — last added is outermost.
    # SetupDetectionMiddleware added first so it runs inside logging.
    app.add_middleware(SetupDetectionMiddleware)
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(RequestLoggingMiddleware)

    # Rate limiting — runs after logging so rate-limited requests are logged
    from pullbox.core.api_rate_limiter import APIRateLimiter

    limiter = APIRateLimiter(
        tier1=settings.rate_limit_tier1,
        tier2=settings.rate_limit_tier2,
        tier3=settings.rate_limit_tier3,
        enabled=settings.rate_limit_enabled,
    )
    app.add_middleware(RateLimitMiddleware, limiter=limiter)

    if settings.debug:
        allowed_origins = [
            f"http://localhost:{settings.port}",
            f"http://127.0.0.1:{settings.port}",
            "http://localhost:8585",
            "http://127.0.0.1:8585",
        ]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Security headers — outermost layer so headers are added to every response
    app.add_middleware(SecurityHeadersMiddleware, debug=settings.debug)

    # ── Exception handlers ─────────────────────────────────────────
    @app.exception_handler(PullboxError)
    async def pullbox_error_handler(request: Request, exc: PullboxError) -> JSONResponse:
        """Convert PullboxError subclasses to structured JSON responses."""
        logger.warning(
            "request_error",
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            path=request.url.path,
        )
        headers: dict[str, str] = {}
        retry_after = getattr(exc, "retry_after_seconds", None)
        if isinstance(retry_after, int) and retry_after > 0:
            headers["Retry-After"] = str(retry_after)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all for unhandled exceptions — return JSON instead of HTML.

        In production mode, returns a generic message to avoid leaking
        internal paths, SQL structure, or stack trace details.
        """
        logger.exception(
            "unhandled_error",
            path=request.url.path,
            method=request.method,
            error=str(exc),
        )
        message = str(exc) if settings.debug else "An unexpected error occurred."
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": message}},
        )

    @app.exception_handler(AuthenticationError)
    async def auth_error_handler(
        request: Request, exc: AuthenticationError
    ) -> JSONResponse | RedirectResponse:
        """Redirect page navigations to login; return JSON 401 for XHR/HTMX."""
        is_htmx_request = request.headers.get("HX-Request", "").lower() == "true"
        accepts_html = "text/html" in request.headers.get("accept", "").lower()
        is_api_request = request.url.path.startswith("/api/")

        if accepts_html and not is_htmx_request and not is_api_request:
            return RedirectResponse(url=AUTH_REDIRECT_PATH, status_code=302)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers={AUTH_REDIRECT_HEADER: AUTH_REDIRECT_PATH},
        )

    # ── System endpoints ─────────────────────────────────────────────
    @app.get("/ping", tags=["system"])
    async def ping() -> dict[str, str]:
        """Unauthenticated health check for Docker HEALTHCHECK and monitoring tools."""
        return {"status": "ok", "service": "pullbox"}

    # ── Static files ───────────────────────────────────────────────
    if STATIC_DIR.is_dir():
        app.mount("/static", PullboxStaticFiles(directory=STATIC_DIR), name="static")

    # Serve cover images from the data directory
    covers_dir = settings.covers_dir
    if covers_dir.is_dir():
        app.mount("/covers", StaticFiles(directory=covers_dir), name="covers")

    # ── Routes ─────────────────────────────────────────────────────
    from pullbox.api.v1.router import v1_router
    from pullbox.ui.routes import router as ui_router

    app.include_router(v1_router)
    app.include_router(ui_router)

    return app

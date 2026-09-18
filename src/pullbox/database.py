"""
Pullbox database — async SQLAlchemy engine and session factory.

Configures the async engine from application settings, applies SQLite
WAL mode pragmas for concurrent read performance, sets restrictive file
permissions on the database, and provides an async session generator
for FastAPI dependency injection.
"""

import asyncio
import shutil
import sqlite3
import stat
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from weakref import WeakSet

import structlog
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
    create_async_engine,
)

from pullbox.config import get_settings

logger = structlog.get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_maintenance_gate = asyncio.Event()
_maintenance_gate.set()
_maintenance_lock = asyncio.Lock()
_maintenance_reason: str | None = None
_MAINTENANCE_DRAIN_SECONDS = 5.0
_active_sessions: WeakSet["GateAwareAsyncSession"] = WeakSet()
_draining_transactions: dict["GateAwareAsyncSession", AsyncSessionTransaction] = {}
_SQLITE_BUSY_TIMEOUT_MS = 15000
_SQLITE_BUSY_TIMEOUT_PRAGMA = "PRAGMA busy_timeout=15000"
_SQLITE_ALLOWED_JOURNAL_MODES = frozenset({"WAL", "DELETE"})
_SQLITE_JOURNAL_MODE_PRAGMAS = {
    "DELETE": "PRAGMA journal_mode=DELETE",
    "WAL": "PRAGMA journal_mode=WAL",
}


class DatabaseMaintenanceBusyError(RuntimeError):
    """Existing transactions could not drain; maintenance may be retried later."""


class GateAwareAsyncSession(AsyncSession):
    """AsyncSession that pauses database I/O while maintenance is active."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _active_sessions.add(self)

    async def _wait_for_ready(self) -> None:
        transaction = _draining_transactions.get(self)
        if transaction is not None and self.get_transaction() is transaction:
            return
        await wait_for_database_ready()

    async def connection(self, *args: Any, **kwargs: Any) -> Any:
        await self._wait_for_ready()
        return await super().connection(*args, **kwargs)

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        await self._wait_for_ready()
        return await super().execute(*args, **kwargs)

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        await self._wait_for_ready()
        return await super().scalar(*args, **kwargs)

    async def scalars(self, *args: Any, **kwargs: Any) -> Any:
        await self._wait_for_ready()
        return await super().scalars(*args, **kwargs)

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        await self._wait_for_ready()
        return await super().stream(*args, **kwargs)

    async def stream_scalars(self, *args: Any, **kwargs: Any) -> Any:
        await self._wait_for_ready()
        return await super().stream_scalars(*args, **kwargs)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        await self._wait_for_ready()
        return await super().get(*args, **kwargs)

    async def get_one(self, *args: Any, **kwargs: Any) -> Any:
        await self._wait_for_ready()
        return await super().get_one(*args, **kwargs)

    async def flush(self, *args: Any, **kwargs: Any) -> None:
        await self._wait_for_ready()
        await super().flush(*args, **kwargs)

    async def delete(self, *args: Any, **kwargs: Any) -> None:
        await self._wait_for_ready()
        await super().delete(*args, **kwargs)

    async def merge(self, *args: Any, **kwargs: Any) -> Any:
        await self._wait_for_ready()
        return await super().merge(*args, **kwargs)

    async def refresh(self, *args: Any, **kwargs: Any) -> None:
        await self._wait_for_ready()
        await super().refresh(*args, **kwargs)

    async def commit(self) -> None:
        await self._wait_for_ready()
        await super().commit()


def _resolve_sqlite_journal_mode() -> str:
    """Return the preferred SQLite journal mode for new connections."""
    mode = getattr(get_settings(), "sqlite_journal_mode", "WAL")
    journal_mode = str(mode).strip().upper() or "WAL"
    if journal_mode not in _SQLITE_ALLOWED_JOURNAL_MODES:
        logger.warning(
            "database_sqlite_journal_mode_invalid",
            requested=journal_mode,
            applied="WAL",
        )
        return "WAL"
    return journal_mode


def _should_fallback_sqlite_journal_mode(exc: sqlite3.Error) -> bool:
    """Return True when WAL should fall back to a safer journal mode."""
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "disk i/o error",
            "locking protocol",
        )
    )


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection: object, _connection_record: object) -> None:
    """Enable WAL mode, foreign keys, busy timeout, and sync mode for SQLite.

    Only runs for SQLite connections — PostgreSQL and other backends skip this.
    """
    module_name = type(dbapi_connection).__module__
    if "sqlite" not in module_name and "aiosqlite" not in module_name:
        return
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    journal_mode = _resolve_sqlite_journal_mode()
    applied_journal_mode = journal_mode
    try:
        cursor.execute(_SQLITE_JOURNAL_MODE_PRAGMAS[journal_mode])
    except sqlite3.Error as exc:
        if journal_mode == "WAL" and _should_fallback_sqlite_journal_mode(exc):
            applied_journal_mode = "DELETE"
            logger.warning(
                "database_sqlite_journal_mode_fallback",
                requested=journal_mode,
                applied=applied_journal_mode,
                error=str(exc),
            )
            cursor.execute(_SQLITE_JOURNAL_MODE_PRAGMAS[applied_journal_mode])
        else:
            cursor.close()
            raise
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(_SQLITE_BUSY_TIMEOUT_PRAGMA)
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def _set_db_permissions(db_url: str) -> None:
    """Set restrictive permissions on the SQLite database file.

    Sets owner-only read/write (0o600) to prevent other users on the
    system from reading the database.  Silently skips non-SQLite URLs,
    nonexistent files, and permission errors (e.g. Windows, network mounts).
    """
    if "sqlite" not in db_url:
        return

    if ":///" in db_url:
        raw_path = db_url.split(":///", 1)[1]
    elif "://" in db_url:
        raw_path = db_url.split("://", 1)[1]
    else:
        return

    # Strip query params (e.g., ?check_same_thread=False)
    raw_path = raw_path.split("?", 1)[0]
    db_file = Path(raw_path)

    if not db_file.exists():
        return

    try:
        db_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
        logger.debug("database_permissions_set", path=db_file.name, mode="600")
    except OSError as exc:
        logger.warning("database_permissions_failed", error=str(exc))


def _sqlite_db_file_from_url(db_url: str) -> Path | None:
    """Return the SQLite database file path for a SQLAlchemy URL."""
    if "sqlite" not in db_url:
        return None

    if ":///" in db_url:
        raw_path = db_url.split(":///", 1)[1]
    elif "://" in db_url:
        raw_path = db_url.split("://", 1)[1]
    else:
        return None

    return Path(raw_path.split("?", 1)[0])


def _probe_sqlite_file(db_file: Path, *, immutable: bool = False) -> tuple[bool, str | None]:
    """Return whether a SQLite file can be opened and queried successfully."""
    conn: sqlite3.Connection | None = None
    try:
        if immutable:
            target = f"{db_file.resolve().as_uri()}?mode=ro&immutable=1"
            conn = sqlite3.connect(target, uri=True)
            probe_sql = "PRAGMA quick_check"
        else:
            conn = sqlite3.connect(str(db_file))
            probe_sql = "SELECT count(*) FROM sqlite_master"

        cursor = conn.cursor()
        cursor.execute(probe_sql).fetchone()
        return True, None
    except sqlite3.Error as exc:
        return False, str(exc)
    finally:
        if conn is not None:
            conn.close()


def _recover_sqlite_sidecars_if_needed(db_url: str) -> None:
    """Quarantine stale SQLite WAL/SHM sidecars when the main DB is still readable.

    SQLite can surface intermittent ``disk I/O error`` failures when a stale or
    corrupted ``-wal`` / ``-shm`` pair survives an unclean shutdown. If the main
    database file is still readable in immutable mode, move the sidecars aside so
    the next normal open can recreate them cleanly.
    """
    db_file = _sqlite_db_file_from_url(db_url)
    if db_file is None or not db_file.exists():
        return

    ok, error = _probe_sqlite_file(db_file)
    if ok or error is None or "disk i/o error" not in error.lower():
        return

    immutable_ok, immutable_error = _probe_sqlite_file(db_file, immutable=True)
    if not immutable_ok:
        logger.error(
            "database_sqlite_sidecar_recovery_unavailable",
            path=db_file.name,
            error=error,
            immutable_error=immutable_error,
        )
        return

    sidecars = [
        sidecar
        for sidecar in (
            db_file.with_name(f"{db_file.name}-wal"),
            db_file.with_name(f"{db_file.name}-shm"),
        )
        if sidecar.exists()
    ]
    if not sidecars:
        logger.warning(
            "database_sqlite_sidecar_recovery_no_sidecars",
            path=db_file.name,
            error=error,
        )
        return

    recovery_dir = db_file.parent / f"recovery_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    recovery_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for sidecar in sidecars:
        destination = recovery_dir / sidecar.name
        shutil.move(str(sidecar), destination)
        moved.append(sidecar.name)

    retry_ok, retry_error = _probe_sqlite_file(db_file)
    log_event = (
        "database_sqlite_sidecar_recovery_succeeded"
        if retry_ok
        else "database_sqlite_sidecar_recovery_failed"
    )
    log_fn = logger.warning if retry_ok else logger.error
    log_fn(
        log_event,
        path=db_file.name,
        moved=moved,
        recovery_dir=str(recovery_dir),
        original_error=error,
        retry_error=retry_error,
    )


def _build_engine_kwargs(db_url: str, *, echo: bool) -> dict[str, object]:
    """Build keyword arguments for create_async_engine based on backend.

    PostgreSQL gets full connection pool tuning; file-based SQLite gets
    pre-ping only (it uses a single-writer model); in-memory SQLite gets
    no pool settings (uses StaticPool internally).
    """
    kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": True}

    if "postgresql" in db_url or "asyncpg" in db_url:
        kwargs.update(
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=3600,
        )
    elif ":memory:" in db_url:
        # In-memory SQLite — no pool tuning
        del kwargs["pool_pre_ping"]

    return kwargs


def get_engine() -> AsyncEngine:
    """Return the async engine, creating it on first call."""
    global _engine
    if _engine is None:
        settings = get_settings()
        echo = getattr(settings, "sql_echo", False)
        _recover_sqlite_sidecars_if_needed(settings.db_url)
        kwargs = _build_engine_kwargs(settings.db_url, echo=echo)
        _engine = create_async_engine(settings.db_url, **kwargs)
        _set_db_permissions(settings.db_url)
        logger.debug("database_engine_created", db_url=settings.db_url)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the async session factory, creating it on first call."""
    global _session_factory
    if _session_factory is None:
        engine = get_engine()
        _session_factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=GateAwareAsyncSession,
        )
    return _session_factory


async def wait_for_database_ready() -> None:
    """Block until database maintenance work has finished."""
    await _maintenance_gate.wait()


def database_maintenance_reason() -> str | None:
    """Return the active exclusive-maintenance reason, if any."""
    return _maintenance_reason


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session for FastAPI dependency injection.

    Commits on success, rolls back on exception.
    """
    await wait_for_database_ready()
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Dispose the engine, closing all connections. Called on app shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.debug("database_engine_disposed")


async def await_maintenance_worker[T](operation: Awaitable[T]) -> T:
    """Keep the maintenance fence until a non-cancellable worker actually stops."""
    worker = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        with suppress(BaseException):
            worker.result()
        raise


@asynccontextmanager
async def database_maintenance_window(*, reason: str) -> AsyncGenerator[None, None]:
    """Temporarily pause new DB sessions for exclusive maintenance work."""
    global _maintenance_reason

    async with _maintenance_lock:
        _maintenance_reason = reason
        _maintenance_gate.clear()
        try:
            _draining_transactions.update(
                (session, transaction)
                for session in tuple(_active_sessions)
                if (transaction := session.get_transaction()) is not None
            )
            try:
                async with asyncio.timeout(_MAINTENANCE_DRAIN_SECONDS):
                    while any(
                        session.get_transaction() is transaction
                        for session, transaction in _draining_transactions.items()
                    ):
                        await asyncio.sleep(0.01)
            except TimeoutError as exc:
                raise DatabaseMaintenanceBusyError(
                    "Database is busy; retry maintenance after current work finishes."
                ) from exc
            _draining_transactions.clear()
            await dispose_engine()
            logger.info("database_maintenance_started", reason=reason)
            yield
        finally:
            _draining_transactions.clear()
            _maintenance_gate.set()
            _maintenance_reason = None
            logger.info("database_maintenance_finished", reason=reason)

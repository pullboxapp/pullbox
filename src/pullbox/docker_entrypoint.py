"""Container entrypoint for shell-less Docker Hardened Images."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, TextIO, cast

from pullbox.core.shutdown import RESTART_EXIT_CODE
from pullbox.startup_log_tee import StartupLogMirror
from pullbox.startup_messages import (
    build_startup_summary,
    render_bootstrap_summary,
    render_launching,
    render_migration_complete,
    render_migration_start,
    render_restart_requested,
)

DEFAULT_COMMAND = ("python", "-m", "pullbox")

# Uvicorn gets five seconds to drain active requests. Keep the entrypoint's
# supervisor deadline below Docker's default ten-second stop grace period while
# leaving enough time for Uvicorn to finish its lifespan shutdown.
CHILD_SHUTDOWN_TIMEOUT_SECONDS = 8.0


class _TeeStream:
    """Mirror writes to the original stream and the rotating startup log."""

    def __init__(self, original: TextIO, mirror: StartupLogMirror) -> None:
        self._original = original
        self._mirror = mirror

    @property
    def encoding(self) -> str:
        """Expose the wrapped stream encoding for logging libraries."""
        return self._original.encoding or "utf-8"

    def writable(self) -> bool:
        """Report that this stream accepts writes."""
        return True

    def write(self, text: str) -> int:
        """Write text to container output and startup.log."""
        self._original.write(text)
        self._original.flush()
        self._mirror.write(text)
        return len(text)

    def flush(self) -> None:
        """Flush container output."""
        self._original.flush()


def _env_int(name: str, default: int) -> int:
    """Read a positive integer environment setting."""
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _configure_runtime_environment() -> Path:
    """Set Docker defaults and create state directories without shell helpers."""
    os.environ.setdefault("PULLBOX_DB_URL", "sqlite+aiosqlite:////data/pullbox.db")

    logs_dir = Path(os.environ.get("PULLBOX_LOGS_DIR", "/data/logs"))
    data_dirs = [
        logs_dir,
        Path(os.environ.get("PULLBOX_TEMP_DIR", "/data/tmp")),
        Path(os.environ.get("PULLBOX_BACKUP_DIR", "/data/backups")),
    ]
    for path in data_dirs:
        path.mkdir(parents=True, exist_ok=True)

    return logs_dir / "startup.log"


def _install_startup_log_mirror(startup_log: Path) -> StartupLogMirror:
    """Mirror stdout and stderr to the rotating startup log."""
    mirror = StartupLogMirror(
        startup_log,
        max_bytes=_env_int("PULLBOX_LOG_SIZE_LIMIT_MB", 1) * 1024 * 1024,
        backup_count=_env_int("PULLBOX_LOG_BACKUP_COUNT", 5),
    )
    sys.stdout = cast("TextIO", _TeeStream(sys.stdout, mirror))
    sys.stderr = cast("TextIO", _TeeStream(sys.stderr, mirror))
    return mirror


def _run_migrations() -> None:
    """Run database migrations before application launch."""
    print(render_migration_start(), flush=True)
    exit_code = _run_process(
        [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", "upgrade", "head"]
    )
    if exit_code != 0:
        sys.exit(exit_code)
    print(render_migration_complete(), flush=True)


def _run_process(command: list[str]) -> int:
    """Run a child process, mirror its output, and forward termination signals."""
    try:
        process = subprocess.Popen(
            command,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        print(f"[bootstrap] Failed to launch {' '.join(command)}: {exc}", file=sys.stderr)
        return 127

    previous_handlers: dict[signal.Signals, Any] = {}
    forwarded_signal: signal.Signals | None = None
    shutdown_timer: threading.Timer | None = None

    def _force_kill_after_timeout() -> None:
        if process.poll() is None:
            process.kill()

    def _forward(signum: int, _frame: object | None) -> None:
        nonlocal forwarded_signal, shutdown_timer
        if process.poll() is None:
            forwarded = signal.Signals(signum)
            process.send_signal(forwarded)
            if forwarded_signal is None:
                forwarded_signal = forwarded
                shutdown_timer = threading.Timer(
                    CHILD_SHUTDOWN_TIMEOUT_SECONDS,
                    _force_kill_after_timeout,
                )
                shutdown_timer.daemon = True
                shutdown_timer.start()

    for signum in (signal.Signals.SIGINT, signal.Signals.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, cast("Any", _forward))

    try:
        if process.stdout is not None:
            for line in process.stdout:
                print(line, end="", flush=True)
        exit_code = process.wait()
    finally:
        if shutdown_timer is not None:
            shutdown_timer.cancel()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, cast("Any", handler))

    # Uvicorn intentionally re-raises the captured shutdown signal after its
    # graceful lifecycle completes. A SIGTERM that this entrypoint forwarded is
    # therefore an expected container stop, not an application failure. Raising
    # here also prevents a stop received during migrations from launching the
    # application after the migration child exits.
    if forwarded_signal is signal.Signals.SIGTERM and exit_code in {
        0,
        -int(signal.Signals.SIGTERM),
    }:
        raise SystemExit(0)

    return exit_code


def main(argv: list[str] | None = None) -> None:
    """Run migrations, launch Pullbox, and honor graceful restart requests."""
    command = list(argv if argv is not None else sys.argv[1:]) or list(DEFAULT_COMMAND)
    startup_log = _configure_runtime_environment()
    mirror = _install_startup_log_mirror(startup_log)
    try:
        print(render_bootstrap_summary(build_startup_summary(startup_log=str(startup_log))))
        _run_migrations()
        print(render_launching(), flush=True)

        while True:
            exit_code = _run_process(command)
            if exit_code != RESTART_EXIT_CODE:
                sys.exit(exit_code)

            print(render_restart_requested(RESTART_EXIT_CODE), flush=True)
            _run_migrations()
            print(render_launching(), flush=True)
    finally:
        mirror.close()


if __name__ == "__main__":
    main()

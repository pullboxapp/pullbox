"""Human-readable startup logging helpers for container/runtime boot."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from urllib.parse import urljoin

from pullbox.branding import display_version, startup_banner
from pullbox.config import get_settings
from pullbox.core.build_metadata import BuildMetadata, get_build_metadata
from pullbox.core.db_utils import database_backend_label


@dataclass(frozen=True, slots=True)
class StartupSummary:
    """Values shown in the startup bootstrap summary."""

    version: str
    release_date: str | None
    branch: str | None
    commit: str | None
    base_url: str
    bind_address: str
    port: int
    database: str
    library_root: str
    startup_log: str


def _status_line(prefix: str, label: str, value: str) -> str:
    """Format one startup status line."""
    return f"[{prefix}] {label}: {value}"


def _coalesce(value: str | None, fallback: str = "—") -> str:
    """Return a non-empty display value."""
    trimmed = (value or "").strip()
    return trimmed or fallback


def build_startup_summary(*, startup_log: str) -> StartupSummary:
    """Build the bootstrap summary from runtime settings and build metadata."""
    settings = get_settings()
    metadata = get_build_metadata()
    return StartupSummary(
        version=display_version(),
        release_date=metadata.release_date,
        branch=metadata.branch,
        commit=metadata.commit,
        base_url=settings.base_url,
        bind_address=settings.bind_address,
        port=settings.port,
        database=database_backend_label(settings.db_url),
        library_root=str(settings.library_root),
        startup_log=startup_log,
    )


def render_bootstrap_summary(summary: StartupSummary) -> str:
    """Render the startup banner plus bootstrap context."""
    lines = [
        startup_banner(version=summary.version),
        "",
        f"[bootstrap] Pullbox v{summary.version}",
    ]
    if summary.release_date:
        lines.append(_status_line("bootstrap", "Released", summary.release_date))
    lines.extend(
        [
            _status_line("bootstrap", "Branch", _coalesce(summary.branch)),
            _status_line("bootstrap", "Commit", _coalesce(summary.commit)),
            _status_line("bootstrap", "Base URL", summary.base_url),
            _status_line("bootstrap", "Bind", f"{summary.bind_address}:{summary.port}"),
            _status_line("bootstrap", "Database", summary.database),
            _status_line("bootstrap", "Library Root", summary.library_root),
            _status_line("bootstrap", "Logs", summary.startup_log),
        ]
    )
    return "\n".join(lines)


def render_migration_start() -> str:
    """Render the migration-start bootstrap line."""
    return "[bootstrap] Running database migrations..."


def render_migration_complete() -> str:
    """Render the migration-complete bootstrap line."""
    return "[bootstrap] Database migrations complete"


def render_launching() -> str:
    """Render the pre-launch bootstrap line."""
    return "[bootstrap] Launching Pullbox web app..."


def render_restart_requested(exit_code: int) -> str:
    """Render the restart-requested bootstrap line."""
    return f"[bootstrap] Restart requested (exit code {exit_code}), relaunching..."


def render_ready_summary(
    *,
    base_url: str,
    scheduler_active: bool,
    health_url: str | None = None,
) -> str:
    """Render the final app-ready status block."""
    resolved_health_url = health_url or urljoin(base_url.rstrip("/") + "/", "ping")
    scheduler_state = "active" if scheduler_active else "inactive"
    return "\n".join(
        [
            "[ready] Pullbox is ready",
            _status_line("ready", "Open", base_url),
            _status_line("ready", "Health", resolved_health_url),
            _status_line("ready", "Scheduler", scheduler_state),
        ]
    )


def _emit(message: str) -> None:
    """Print one startup message block."""
    print(message, flush=True)


def main() -> None:
    """Render one startup message block for the Docker entrypoint."""
    parser = argparse.ArgumentParser(description="Render Pullbox startup log messages.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser("bootstrap")
    bootstrap_parser.add_argument("--startup-log", required=True)

    subparsers.add_parser("migration-start")
    subparsers.add_parser("migration-complete")
    subparsers.add_parser("launching")

    restart_parser = subparsers.add_parser("restart-requested")
    restart_parser.add_argument("--exit-code", type=int, required=True)

    args = parser.parse_args()

    if args.command == "bootstrap":
        _emit(render_bootstrap_summary(build_startup_summary(startup_log=args.startup_log)))
        return
    if args.command == "migration-start":
        _emit(render_migration_start())
        return
    if args.command == "migration-complete":
        _emit(render_migration_complete())
        return
    if args.command == "launching":
        _emit(render_launching())
        return
    if args.command == "restart-requested":
        _emit(render_restart_requested(args.exit_code))
        return
    raise AssertionError(f"Unhandled startup command: {args.command}")


__all__ = [
    "BuildMetadata",
    "StartupSummary",
    "build_startup_summary",
    "render_bootstrap_summary",
    "render_launching",
    "render_migration_complete",
    "render_migration_start",
    "render_ready_summary",
    "render_restart_requested",
]


if __name__ == "__main__":
    main()

"""Pullbox entry point — resolve bootstrap runtime settings, then start uvicorn.

Usage:
    python -m pullbox

Bind address and port are runtime-managed bootstrap settings.
The persistent host secret is still generated into config.xml on first run.
"""

from __future__ import annotations

from pathlib import Path

from pullbox.core.https_runtime import (
    resolve_https_runtime_settings,
    uvicorn_ssl_kwargs,
    validate_https_runtime_settings,
)

# Leave time for in-flight requests to finish while ensuring long-lived SSE
# connections cannot consume Docker's default 10-second stop grace period.
GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 5


def _resolve_db_path(db_url: str) -> Path | None:
    """Extract the SQLite file path from a database URL.

    Returns None for non-SQLite URLs (e.g. PostgreSQL) or malformed strings.
    """
    if not db_url or "sqlite" not in db_url:
        return None
    if ":///" in db_url:
        raw = db_url.split(":///", 1)[1]
    elif "://" in db_url:
        raw = db_url.split("://", 1)[1]
    else:
        return None
    raw = raw.split("?", 1)[0]
    return Path(raw) if raw else None


def ensure_host_secret(data_dir: Path, db_path: Path | None = None) -> None:
    """Ensure the host secret file exists before the app starts."""
    from pullbox.core.config_file import ConfigFileProvider

    config_path = data_dir / "config.xml"
    provider = ConfigFileProvider(config_path)
    provider.ensure_config_exists(db_path=db_path)


def main() -> None:
    """Start uvicorn with runtime-managed host/port settings."""
    import uvicorn

    from pullbox.config import get_settings

    settings = get_settings()
    db_path = _resolve_db_path(settings.db_url)
    ensure_host_secret(settings.data_dir, db_path=db_path)
    https_settings = resolve_https_runtime_settings(settings=settings)
    validate_https_runtime_settings(https_settings)

    uvicorn.run(
        "pullbox.app:create_app",
        host=settings.bind_address,
        port=settings.port,
        factory=True,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
        **uvicorn_ssl_kwargs(https_settings),
    )


if __name__ == "__main__":
    main()

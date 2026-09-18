"""Post-restore aftercare for database restore-point recovery.

Database restore points intentionally contain only the SQLite database. This
service records a durable filesystem marker during restore so the next startup
can rebuild derived state, such as local cover files and stale ComicVine
metadata, once the restored database is active.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from pullbox.config import get_settings

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)

RESTORE_RECOVERY_MARKER_FILENAME = "restore_recovery_pending.json"
RESTORE_RECOVERY_STATUS_FILENAME = "restore_recovery_status.json"
_SWEEP_POLL_SECONDS = 5.0

_STEP_DEFINITIONS = (
    ("cover_backfill", "Backfill series cover cache"),
    ("issue_catalog_sync", "Sync ComicVine issue catalogs"),
    ("metadata_refresh", "Refresh stale series metadata"),
)

_DB_CHECK_RECOMMENDATION = {
    "key": "db_check",
    "label": "Run Utilities > Database Check",
    "message": (
        "Database restore points do not inspect your library files. Run Database Check "
        "when restoring onto a different filesystem or after moving library paths."
    ),
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _data_dir(data_dir: Path | None = None) -> Path:
    return data_dir or get_settings().data_dir


def _marker_path(data_dir: Path | None = None) -> Path:
    return _data_dir(data_dir) / RESTORE_RECOVERY_MARKER_FILENAME


def _status_path(data_dir: Path | None = None) -> Path:
    return _data_dir(data_dir) / RESTORE_RECOVERY_STATUS_FILENAME


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _empty_step_status(status: str = "pending") -> list[dict[str, str]]:
    return [
        {
            "key": key,
            "label": label,
            "status": status,
            "message": "",
        }
        for key, label in _STEP_DEFINITIONS
    ]


def _idle_status() -> dict[str, Any]:
    return {
        "status": "idle",
        "restore_filename": None,
        "requested_at": None,
        "started_at": None,
        "completed_at": None,
        "current_step": None,
        "message": "No restore recovery is pending.",
        "steps": [],
        "recommendations": [_DB_CHECK_RECOMMENDATION],
    }


def has_pending_restore_recovery(*, data_dir: Path | None = None) -> bool:
    """Return True when startup should run post-restore aftercare."""
    return _marker_path(data_dir).is_file()


def mark_restore_recovery_pending(
    restore_filename: str,
    *,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Record that post-restore recovery should run after the next restart."""
    requested_at = _utc_now()
    marker = {
        "restore_filename": restore_filename,
        "requested_at": requested_at,
    }
    status: dict[str, Any] = {
        "status": "pending",
        "restore_filename": restore_filename,
        "requested_at": requested_at,
        "started_at": None,
        "completed_at": None,
        "current_step": None,
        "message": "Post-restore recovery will run after Pullbox restarts.",
        "steps": _empty_step_status(),
        "recommendations": [_DB_CHECK_RECOMMENDATION],
    }
    _write_json(_marker_path(data_dir), marker)
    _write_json(_status_path(data_dir), status)
    logger.info("restore_recovery_marked_pending", restore_filename=restore_filename)
    return status


def get_restore_recovery_status(*, data_dir: Path | None = None) -> dict[str, Any]:
    """Return the latest post-restore recovery status."""
    status = _read_json(_status_path(data_dir))
    if status is None:
        return _idle_status()
    status.setdefault("recommendations", [_DB_CHECK_RECOMMENDATION])
    return status


async def _run_cover_backfill_step() -> str:
    from pullbox.tasks.cover_backfill_task import backfill_series_covers

    stats = await backfill_series_covers()
    return (
        f"Processed {stats.processed} series covers; downloaded {stats.downloaded}, "
        f"linked {stats.linked_existing}, failed {stats.failed}."
    )


async def _run_issue_sync_step() -> str:
    from pullbox.tasks.metadata_task import sync_new_issues

    result = await sync_new_issues()
    if result.status == "waiting":
        await _wait_for_metadata_sweep("sync_new_issues")
    return "ComicVine issue catalog sync completed."


async def _run_metadata_refresh_step() -> str:
    from pullbox.tasks.metadata_task import refresh_metadata

    result = await refresh_metadata()
    if result.status == "waiting":
        await _wait_for_metadata_sweep("refresh_metadata")
    return "Series metadata refresh completed."


async def _wait_for_metadata_sweep(task_id: str) -> None:
    """Observe scheduled continuation batches without holding a database transaction."""
    from pullbox.database import get_session_factory
    from pullbox.tasks.metadata_sweep_state import load_sweep

    factory = get_session_factory()
    while True:
        async with factory() as session:
            state = await load_sweep(session, task_id)
        if not state.active:
            return
        # The scheduler owns remaining work and provider cooldowns. Cancellation
        # leaves the restore marker and durable sweep checkpoint for startup.
        await asyncio.sleep(_SWEEP_POLL_SECONDS)


async def run_restore_recovery_if_pending(
    *,
    data_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Run post-restore aftercare once when a restore marker is present."""
    marker_path = _marker_path(data_dir)
    marker = _read_json(marker_path)
    if marker is None:
        return None

    restore_filename = str(marker.get("restore_filename") or "")
    requested_at = str(marker.get("requested_at") or _utc_now())
    status: dict[str, Any] = {
        "status": "running",
        "restore_filename": restore_filename,
        "requested_at": requested_at,
        "started_at": _utc_now(),
        "completed_at": None,
        "current_step": None,
        "message": "Post-restore recovery is rebuilding derived metadata.",
        "steps": _empty_step_status(),
        "recommendations": [_DB_CHECK_RECOMMENDATION],
    }
    _write_json(_status_path(data_dir), status)
    logger.info("restore_recovery_started", restore_filename=restore_filename)

    runners = (
        _run_cover_backfill_step,
        _run_issue_sync_step,
        _run_metadata_refresh_step,
    )
    failed = 0
    for index, runner in enumerate(runners):
        step = status["steps"][index]
        step["status"] = "running"
        status["current_step"] = step["key"]
        _write_json(_status_path(data_dir), status)

        try:
            step["message"] = await runner()
            step["status"] = "completed"
        except Exception as exc:
            failed += 1
            step["status"] = "failed"
            step["message"] = str(exc)
            logger.warning(
                "restore_recovery_step_failed",
                step=step["key"],
                restore_filename=restore_filename,
                exc_info=exc,
            )
        _write_json(_status_path(data_dir), status)

    status["completed_at"] = _utc_now()
    status["current_step"] = None
    if failed:
        status["status"] = "attention"
        status["message"] = "Post-restore recovery completed but needs attention."
    else:
        status["status"] = "completed"
        status["message"] = "Post-restore recovery completed."

    _write_json(_status_path(data_dir), status)
    marker_path.unlink(missing_ok=True)
    logger.info(
        "restore_recovery_completed",
        restore_filename=restore_filename,
        status=status["status"],
        failed_steps=failed,
    )
    return status

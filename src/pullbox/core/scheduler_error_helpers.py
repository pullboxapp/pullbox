"""Scheduler error-classification helpers."""

from __future__ import annotations


def is_locked_error(exc: BaseException) -> bool:
    """Return True when SQLite reports a transient database lock."""
    message = str(exc).lower()
    return "database is locked" in message or "locking protocol" in message


def is_missing_task_stats_table_error(exc: BaseException) -> bool:
    """Return True when the persisted task stats table is not available yet."""
    return "no such table: scheduled_task_stats" in str(exc).lower()


def is_unusable_persist_error(exc: BaseException) -> bool:
    """Return True when persisted scheduler stats should fail dark."""
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "database disk image is malformed",
            "file is not a database",
            "disk i/o error",
        )
    )


def logical_task_id(job_id: str) -> str:
    """Normalize scheduler one-shot job IDs to their logical task IDs."""
    for suffix in ("_manual", "__continuation", "__exclusive_retry"):
        if job_id.endswith(suffix):
            return job_id[: -len(suffix)]
    return job_id

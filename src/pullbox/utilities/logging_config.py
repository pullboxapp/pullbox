"""Utility logging infrastructure — dual-tier structured logging.

Provides JSON-formatted rotating file logs for utility operations and a
UtilityLogger helper that writes to both the Python logger (file output)
and collects entries for later DB insertion by the queue manager.

Worker processes can't write to the DB directly, so log entries are
collected as tuples and bulk-inserted after each batch completes.
"""

from __future__ import annotations

import json
import logging
import traceback
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path  # noqa: TC003 - used at runtime in function signatures
from typing import Any

_LEVEL_ORDER = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def _normalize_utility_log_level(level: str | None) -> str:
    """Return a valid utility log level name."""
    normalized = str(level or "INFO").upper()
    return normalized if normalized in _LEVEL_ORDER else "INFO"


# ── JSONFormatter ──────────────────────────────────────────────


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON objects.

    Includes standard fields (timestamp, level, logger, message) plus
    optional structured fields (job_id, item_id, file_path, worker_id,
    duration_ms, extra). Missing optional fields are omitted, not null.
    """

    _OPTIONAL_FIELDS = ("job_id", "item_id", "file_path", "worker_id", "duration_ms")

    def format(self, record: logging.LogRecord) -> str:
        from pullbox.core.log_sanitizer import sanitize_log_mapping, sanitize_log_string

        message = record.getMessage()
        if isinstance(message, str):
            message = sanitize_log_string(message)

        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }

        # Add optional structured fields if present on the record
        for field in self._OPTIONAL_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                entry[field] = value

        # Add extra data dict if present and non-empty; sanitize sensitive keys
        extra_data: dict[str, Any] | None = getattr(record, "extra_data", None)
        if extra_data:
            entry["extra"] = sanitize_log_mapping(_make_serializable(extra_data))

        # Format exception info inline
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = "".join(traceback.format_exception(*record.exc_info))

        return json.dumps(entry, ensure_ascii=False, default=str)


def _make_serializable(data: dict[str, Any]) -> dict[str, Any]:
    """Convert non-JSON-serializable values to strings."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        try:
            json.dumps(value)
            result[key] = value
        except (TypeError, ValueError):
            result[key] = str(value)
    return result


# ── configure_utility_logging ──────────────────────────────────


def configure_utility_logging(
    log_dir: Path,
    level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Set up rotating JSON file logging for utility operations.

    Creates a RotatingFileHandler at ``{log_dir}/utilities.log`` attached
    to the ``pullbox.utilities`` logger. Sub-loggers (convert, rename,
    etc.) inherit this handler.

    Args:
        log_dir: Directory for the log file (created if it doesn't exist).
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR).
        max_bytes: Max file size before rotation (default 10MB).
        backup_count: Number of rotated backup files to keep.
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("pullbox.utilities")
    logger.disabled = False

    # Remove any existing handlers to avoid duplicates on reconfigure
    for handler in logger.handlers[:]:
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler)

    log_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(log_level)

    handler = RotatingFileHandler(
        filename=str(log_dir / "utilities.log"),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(JSONFormatter())
    handler.setLevel(log_level)
    logger.addHandler(handler)

    # Don't propagate to root logger (avoid double output)
    logger.propagate = False


def configure_utility_logging_runtime(
    log_dir: Path,
    level: str = "INFO",
) -> None:
    """Runtime wrapper for utility logger reconfiguration."""
    configure_utility_logging(log_dir=log_dir, level=_normalize_utility_log_level(level))


def should_emit_utility_log(
    entry_level: str,
    threshold: str,
) -> bool:
    """Return whether a utility log entry should be persisted."""
    normalized_level = _normalize_utility_log_level(entry_level)
    normalized_threshold = _normalize_utility_log_level(threshold)
    return _LEVEL_ORDER[normalized_level] >= _LEVEL_ORDER[normalized_threshold]


def emit_utility_log_entry(
    *,
    job_id: str,
    level: str,
    message: str,
    item_id: str | None = None,
    file_path: str | None = None,
    worker_id: int | None = None,
    duration_ms: int | None = None,
    extra: dict[str, Any] | None = None,
    logger_name: str = "pullbox.utilities",
) -> None:
    """Write one structured utility log entry to the utility logger."""
    from pullbox.core.log_sanitizer import sanitize_log_mapping, sanitize_log_string

    normalized_level = _normalize_utility_log_level(level)
    logger = logging.getLogger(logger_name)
    sanitized_message = sanitize_log_string(message)
    sanitized_extra = sanitize_log_mapping(_make_serializable(extra or {}))
    logger.log(
        _LEVEL_ORDER[normalized_level],
        sanitized_message,
        extra={
            "job_id": job_id,
            "item_id": item_id,
            "file_path": file_path,
            "worker_id": worker_id,
            "duration_ms": duration_ms,
            "extra_data": sanitized_extra,
        },
    )


def persist_runtime_utility_log(
    session: Any,
    *,
    configured_level: str,
    job_id: str,
    level: str,
    message: str,
    item_id: str | None = None,
    file_path: str | None = None,
    extra: dict[str, Any] | None = None,
    worker_id: int | None = None,
    duration_ms: int | None = None,
) -> bool:
    """Persist one runtime utility log entry and mirror it to utilities.log."""
    from pullbox.core.log_sanitizer import sanitize_log_mapping, sanitize_log_string
    from pullbox.utilities.models import UtilityJobLog

    normalized_level = _normalize_utility_log_level(level)
    if not should_emit_utility_log(normalized_level, configured_level):
        return False

    sanitized_message = sanitize_log_string(message)
    sanitized_extra = sanitize_log_mapping(extra or {})
    timestamp = datetime.now(UTC).isoformat()
    session.add(
        UtilityJobLog(
            job_id=job_id,
            item_id=item_id,
            timestamp=timestamp,
            level=normalized_level,
            message=sanitized_message,
            file_path=file_path,
            extra=json.dumps(sanitized_extra) if sanitized_extra else "{}",
        )
    )
    emit_utility_log_entry(
        job_id=job_id,
        level=normalized_level,
        message=sanitized_message,
        item_id=item_id,
        file_path=file_path,
        worker_id=worker_id,
        duration_ms=duration_ms,
        extra=sanitized_extra,
    )
    return True


# ── UtilityLogger ──────────────────────────────────────────────


class UtilityLogger:
    """Dual-write logger for utility operations.

    Writes to the Python logger (file output via RotatingFileHandler)
    and collects log entries as tuples for later DB insertion by the
    queue manager. Worker processes call drain_entries() to retrieve
    collected entries for IPC back to the main process.

    Each collected entry is a tuple of:
        (level, message, item_id, file_path, extra_dict)
    """

    def __init__(
        self,
        job_id: str,
        logger_name: str = "pullbox.utilities",
        worker_id: int | None = None,
    ) -> None:
        self._job_id = job_id
        self._worker_id = worker_id
        self._logger = logging.getLogger(logger_name)
        self._pending: list[tuple[str, str, str | None, str | None, dict[str, Any]]] = []

    def debug(
        self,
        message: str,
        *,
        item_id: str | None = None,
        file_path: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._log("DEBUG", message, item_id=item_id, file_path=file_path, extra=extra)

    def info(
        self,
        message: str,
        *,
        item_id: str | None = None,
        file_path: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._log("INFO", message, item_id=item_id, file_path=file_path, extra=extra)

    def warning(
        self,
        message: str,
        *,
        item_id: str | None = None,
        file_path: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._log("WARNING", message, item_id=item_id, file_path=file_path, extra=extra)

    def error(
        self,
        message: str,
        *,
        item_id: str | None = None,
        file_path: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._log("ERROR", message, item_id=item_id, file_path=file_path, extra=extra)

    def drain_entries(
        self,
    ) -> list[tuple[str, str, str | None, str | None, dict[str, Any]]]:
        """Return and clear pending log entries for DB insertion.

        Returns:
            List of (level, message, item_id, file_path, extra_dict) tuples.
        """
        entries = self._pending[:]
        self._pending.clear()
        return entries

    def _log(
        self,
        level: str,
        message: str,
        *,
        item_id: str | None = None,
        file_path: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Write to Python logger and collect for DB insertion."""
        log_level = getattr(logging, level, logging.INFO)

        # Build a LogRecord with structured fields for JSONFormatter
        record = self._logger.makeRecord(
            name=self._logger.name,
            level=log_level,
            fn="",
            lno=0,
            msg=message,
            args=(),
            exc_info=None,
        )
        record.job_id = self._job_id
        if item_id is not None:
            record.item_id = item_id
        if file_path is not None:
            record.file_path = file_path
        if self._worker_id is not None:
            record.worker_id = self._worker_id
        if extra:
            record.extra_data = extra

        self._logger.handle(record)

        # Collect for later DB insertion
        self._pending.append((level, message, item_id, file_path, extra or {}))

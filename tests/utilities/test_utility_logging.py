"""Tests for UT-F.4 — utility logging infrastructure.

Verifies JSONFormatter produces valid JSON, configure_utility_logging
sets up rotating file handler, UtilityLogger dual-writes to file and
collects entries for DB insertion, and edge cases (unicode, exceptions,
non-serializable extras) are handled.

Run:
    pytest tests/utilities/test_utility_logging.py -v
"""

from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path

import pytest

from pullbox.utilities.logging_config import (
    JSONFormatter,
    UtilityLogger,
    configure_utility_logging,
    configure_utility_logging_runtime,
    emit_utility_log_entry,
    persist_runtime_utility_log,
    should_emit_utility_log,
)

# ── JSONFormatter ──────────────────────────────────────────────


class TestJSONFormatter:
    """Verify JSONFormatter produces valid JSON with structured fields."""

    def test_produces_valid_json(self) -> None:
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="pullbox.utilities.convert",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="File converted",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "File converted"
        assert "timestamp" in parsed

    def test_includes_structured_fields(self) -> None:
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="pullbox.utilities.convert",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Converted file",
            args=(),
            exc_info=None,
        )
        record.job_id = "job-001"  # type: ignore[attr-defined]
        record.item_id = "item-001"  # type: ignore[attr-defined]
        record.file_path = "/comics/batman.cbr"  # type: ignore[attr-defined]
        record.worker_id = 2  # type: ignore[attr-defined]
        record.duration_ms = 1500  # type: ignore[attr-defined]

        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["job_id"] == "job-001"
        assert parsed["item_id"] == "item-001"
        assert parsed["file_path"] == "/comics/batman.cbr"
        assert parsed["worker_id"] == 2
        assert parsed["duration_ms"] == 1500

    def test_omits_missing_optional_fields(self) -> None:
        """Missing optional fields are not included (not null)."""
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="pullbox.utilities",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "job_id" not in parsed
        assert "item_id" not in parsed
        assert "file_path" not in parsed
        assert "worker_id" not in parsed

    def test_unicode_characters(self) -> None:
        """Unicode in messages (Japanese, em-dashes, curly quotes) produces valid JSON."""
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="pullbox.utilities",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="進撃の巨人 — converted \u201csuccessfully\u201d",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "進撃の巨人" in parsed["message"]
        assert "—" in parsed["message"]

    def test_json_special_characters_escaped(self) -> None:
        """Quotes, backslashes, newlines in messages are properly escaped."""
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="pullbox.utilities",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg='Path: "C:\\comics\\file.cbr"\nLine 2',
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert '"C:\\comics\\file.cbr"' in parsed["message"]
        assert "\\n" in parsed["message"]
        assert "\n" not in parsed["message"]

    def test_very_long_message(self) -> None:
        """10KB+ messages are written in full."""
        formatter = JSONFormatter()
        long_msg = "x" * 12000
        record = logging.LogRecord(
            name="pullbox.utilities",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=long_msg,
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert len(parsed["message"]) == 12000

    def test_exception_info_formatted_inline(self) -> None:
        """Exception traceback is included inline in JSON."""
        formatter = JSONFormatter()
        try:
            raise ValueError("test error with traceback")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="pullbox.utilities",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="Operation failed",
            args=(),
            exc_info=exc_info,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exception" in parsed
        assert "ValueError" in parsed["exception"]
        assert "test error with traceback" in parsed["exception"]

    def test_extra_dict_included(self) -> None:
        """Extra dict is included in output."""
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="pullbox.utilities",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.extra_data = {"pages": 32, "format": "cbz"}  # type: ignore[attr-defined]
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["extra"]["pages"] == 32
        assert parsed["extra"]["format"] == "cbz"

    def test_extra_with_non_serializable_values(self) -> None:
        """Non-serializable values in extra (Path, datetime) are converted to strings."""
        from datetime import datetime

        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="pullbox.utilities",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.extra_data = {  # type: ignore[attr-defined]
            "path": Path("/comics/batman.cbr"),
            "checked_at": datetime(2026, 3, 17, 12, 0, 0),
        }
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "/comics/batman.cbr" in parsed["extra"]["path"]

    def test_empty_extra_dict(self) -> None:
        """Empty extra dict is omitted from output."""
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="pullbox.utilities",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.extra_data = {}  # type: ignore[attr-defined]
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "extra" not in parsed


# ── configure_utility_logging ──────────────────────────────────


class TestConfigureUtilityLogging:
    """Verify rotating file handler is set up correctly."""

    @pytest.mark.parametrize("runtime", [False, True])
    def test_reconfigure_reenables_own_logger_after_migration_logging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: bool
    ) -> None:
        logger = logging.getLogger("pullbox.utilities")
        unrelated_logger = logging.getLogger("pullbox.logging_reconfiguration_unrelated")
        # Alembic's fileConfig disables loggers that already exist in the process.
        monkeypatch.setattr(logger, "disabled", True)
        monkeypatch.setattr(unrelated_logger, "disabled", True)

        if runtime:
            configure_utility_logging_runtime(tmp_path, level="warning")
        else:
            configure_utility_logging(tmp_path, level="WARNING")

        assert logger.disabled is False
        assert unrelated_logger.disabled is True
        emit_utility_log_entry(job_id="job-reconfigured", level="INFO", message="below threshold")
        emit_utility_log_entry(
            job_id="job-reconfigured", level="WARNING", message="visible warning"
        )
        entries = [
            json.loads(line) for line in (tmp_path / "utilities.log").read_text().splitlines()
        ]
        assert [(entry["level"], entry["message"]) for entry in entries] == [
            ("WARNING", "visible warning")
        ]
        assert entries[0]["job_id"] == "job-reconfigured"

    def test_creates_log_file(self, tmp_path: Path) -> None:
        configure_utility_logging(tmp_path, level="DEBUG")
        logger = logging.getLogger("pullbox.utilities")
        logger.info("test message")

        log_file = tmp_path / "utilities.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "test message" in content

    def test_output_is_valid_json_lines(self, tmp_path: Path) -> None:
        configure_utility_logging(tmp_path, level="DEBUG")
        logger = logging.getLogger("pullbox.utilities")
        logger.info("line one")
        logger.warning("line two")

        log_file = tmp_path / "utilities.log"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert "level" in parsed
            assert "message" in parsed

    def test_creates_log_directory(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "nested" / "logs"
        configure_utility_logging(log_dir, level="INFO")
        assert log_dir.is_dir()

    def test_logger_hierarchy(self, tmp_path: Path) -> None:
        """Sub-loggers inherit the utility handler."""
        configure_utility_logging(tmp_path, level="DEBUG")
        child_logger = logging.getLogger("pullbox.utilities.convert")
        child_logger.info("child message")

        log_file = tmp_path / "utilities.log"
        content = log_file.read_text()
        assert "child message" in content

    def test_respects_log_level(self, tmp_path: Path) -> None:
        """Messages below the configured level are not written."""
        configure_utility_logging(tmp_path, level="WARNING")
        logger = logging.getLogger("pullbox.utilities")
        logger.debug("should not appear")
        logger.info("should not appear either")
        logger.warning("should appear")

        log_file = tmp_path / "utilities.log"
        content = log_file.read_text()
        assert "should not appear" not in content
        assert "should appear" in content

    def teardown_method(self) -> None:
        """Clean up handlers added to utility logger."""
        logger = logging.getLogger("pullbox.utilities")
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)

    def test_runtime_reconfigure_uses_normalized_level(self, tmp_path: Path) -> None:
        configure_utility_logging_runtime(tmp_path, level="warning")
        logger = logging.getLogger("pullbox.utilities")
        logger.info("should stay hidden")
        logger.error("should be present")

        log_file = tmp_path / "utilities.log"
        content = log_file.read_text()
        assert "should stay hidden" not in content
        assert "should be present" in content


class TestUtilityLogFiltering:
    """Utility log helpers respect the configured level contract."""

    def test_should_emit_respects_threshold(self) -> None:
        assert should_emit_utility_log("DEBUG", "DEBUG") is True
        assert should_emit_utility_log("INFO", "DEBUG") is True
        assert should_emit_utility_log("WARNING", "WARNING") is True
        assert should_emit_utility_log("ERROR", "WARNING") is True
        assert should_emit_utility_log("INFO", "WARNING") is False
        assert should_emit_utility_log("DEBUG", "ERROR") is False

    def test_emit_utility_log_entry_writes_structured_json_line(self, tmp_path: Path) -> None:
        configure_utility_logging(tmp_path, level="DEBUG")

        emit_utility_log_entry(
            job_id="job-structured-1",
            level="WARNING",
            message="Structured warning",
            item_id="item-9",
            file_path="/comics/test.cbz",
            worker_id=4,
            duration_ms=125,
            extra={"reason": "checksum mismatch"},
        )

        parsed = json.loads((tmp_path / "utilities.log").read_text().strip())
        assert parsed["job_id"] == "job-structured-1"
        assert parsed["item_id"] == "item-9"
        assert parsed["file_path"] == "/comics/test.cbz"
        assert parsed["worker_id"] == 4
        assert parsed["duration_ms"] == 125
        assert parsed["extra"]["reason"] == "checksum mismatch"
        assert parsed["level"] == "WARNING"
        assert parsed["message"] == "Structured warning"

    def test_persist_runtime_utility_log_sanitizes_db_payload(self, tmp_path: Path) -> None:
        configure_utility_logging(tmp_path, level="DEBUG")

        added: list[object] = []

        class _FakeSession:
            def add(self, value: object) -> None:
                added.append(value)

        persisted = persist_runtime_utility_log(
            _FakeSession(),
            configured_level="DEBUG",
            job_id="job-secret-1",
            level="ERROR",
            message="Failed to open postgresql://pullbox:secretpass@db.internal/pullbox",
            extra={
                "api_key": "abc123",
                "url": "http://user:pass@example.com/api?token=secret",
            },
        )

        assert persisted is True
        assert len(added) == 1
        row = added[0]
        assert "***REDACTED***" in row.message
        parsed = json.loads(row.extra)
        assert parsed["api_key"] == "***REDACTED***"
        assert parsed["url"] == "http://***REDACTED***@example.com/api?token=***REDACTED***"

    def teardown_method(self) -> None:
        logger = logging.getLogger("pullbox.utilities")
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)


# ── UtilityLogger ──────────────────────────────────────────────


class TestUtilityLogger:
    """Verify UtilityLogger writes to Python logger and collects entries."""

    def test_info_writes_to_logger(self, tmp_path: Path) -> None:
        configure_utility_logging(tmp_path, level="DEBUG")
        ul = UtilityLogger(job_id="job-001")
        ul.info("Conversion started", file_path="/comics/batman.cbr")

        log_file = tmp_path / "utilities.log"
        content = log_file.read_text()
        assert "Conversion started" in content
        parsed = json.loads(content.strip())
        assert parsed["job_id"] == "job-001"
        assert parsed["file_path"] == "/comics/batman.cbr"

    def test_warning_and_error_levels(self, tmp_path: Path) -> None:
        configure_utility_logging(tmp_path, level="DEBUG")
        ul = UtilityLogger(job_id="job-002")
        ul.warning("Skipping file")
        ul.error("Conversion failed")

        log_file = tmp_path / "utilities.log"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["level"] == "WARNING"
        assert json.loads(lines[1])["level"] == "ERROR"

    def test_debug_level(self, tmp_path: Path) -> None:
        configure_utility_logging(tmp_path, level="DEBUG")
        ul = UtilityLogger(job_id="job-003")
        ul.debug("Verbose detail")

        log_file = tmp_path / "utilities.log"
        content = log_file.read_text()
        assert "Verbose detail" in content

    def test_collects_entries_for_db(self) -> None:
        """Entries are collected for later DB insertion."""
        ul = UtilityLogger(job_id="job-010")
        ul.info("Step 1", file_path="/comics/a.cbr")
        ul.warning("Step 2", extra={"pages": 12})
        ul.error("Step 3", item_id="item-001")

        entries = ul.drain_entries()
        assert len(entries) == 3
        assert entries[0] == ("INFO", "Step 1", None, "/comics/a.cbr", {})
        assert entries[1] == ("WARNING", "Step 2", None, None, {"pages": 12})
        assert entries[2] == ("ERROR", "Step 3", "item-001", None, {})

    def test_drain_clears_entries(self) -> None:
        """drain_entries() returns and clears the pending list."""
        ul = UtilityLogger(job_id="job-011")
        ul.info("msg")
        assert len(ul.drain_entries()) == 1
        assert len(ul.drain_entries()) == 0

    def test_extra_with_non_serializable(self, tmp_path: Path) -> None:
        """Non-serializable extra values are handled gracefully."""
        configure_utility_logging(tmp_path, level="DEBUG")
        ul = UtilityLogger(job_id="job-020")
        ul.info("Test", extra={"path": Path("/comics/batman.cbr")})

        log_file = tmp_path / "utilities.log"
        content = log_file.read_text()
        parsed = json.loads(content.strip())
        assert "/comics/batman.cbr" in parsed["extra"]["path"]

    def test_item_id_and_worker_id(self, tmp_path: Path) -> None:
        configure_utility_logging(tmp_path, level="DEBUG")
        ul = UtilityLogger(job_id="job-030", worker_id=3)
        ul.info("Processing", item_id="item-005")

        log_file = tmp_path / "utilities.log"
        parsed = json.loads(log_file.read_text().strip())
        assert parsed["worker_id"] == 3
        assert parsed["item_id"] == "item-005"

    def teardown_method(self) -> None:
        """Clean up handlers added to utility logger."""
        logger = logging.getLogger("pullbox.utilities")
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)


# ── Pickling ───────────────────────────────────────────────────


class TestLogEntryPickling:
    """Verify log entry tuples survive pickling for worker process IPC."""

    def test_log_entries_are_picklable(self) -> None:
        import pickle

        entries: list[tuple[str, str, str | None, str | None, dict[str, object]]] = [
            ("INFO", "Converted file", "item-001", "/comics/a.cbr", {"pages": 32}),
            ("WARNING", "Large file", None, "/comics/b.cbr", {}),
            ("ERROR", "Failed", "item-002", None, {"error": "corrupt archive"}),
        ]
        pickled = pickle.dumps(entries)
        restored = pickle.loads(pickled)
        assert restored == entries

    def test_entries_with_exception_strings(self) -> None:
        """Exception info stored as string survives pickling."""
        import pickle

        try:
            raise ValueError("test error")
        except ValueError:
            tb_str = traceback.format_exc()

        entries = [("ERROR", "Failed", None, None, {"traceback": tb_str})]
        pickled = pickle.dumps(entries)
        restored = pickle.loads(pickled)
        assert "ValueError" in restored[0][4]["traceback"]

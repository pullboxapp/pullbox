"""Tests for diagnostic debug package generation (C-7.3).

Verifies:
- Package contains system_info.json with expected fields
- Package contains sanitized config (no API keys in plaintext)
- Package contains health status
- Package contains database stats
- API keys are [REDACTED] in config output
- Passwords are [REDACTED] in config output
- Empty logs directory still creates valid package
- ZIP file is valid and extractable
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pullbox.services.diagnostic_service import _redact_value


class TestRedactValue:
    """Secret values are properly redacted."""

    def test_comicvine_api_key_redacted(self) -> None:
        assert _redact_value("comicvine_api_key", "abc123") == "[REDACTED]"

    def test_secret_key_redacted(self) -> None:
        assert _redact_value("secret_key", "mysecret") == "[REDACTED]"

    def test_password_in_key_redacted(self) -> None:
        assert _redact_value("db_password", "hunter2") == "[REDACTED]"

    def test_token_in_key_redacted(self) -> None:
        assert _redact_value("auth_token", "tok_123") == "[REDACTED]"

    def test_api_key_substring_redacted(self) -> None:
        assert _redact_value("some_api_key_field", "val") == "[REDACTED]"

    def test_normal_key_not_redacted(self) -> None:
        assert _redact_value("log_level", "info") == "info"

    def test_search_interval_not_redacted(self) -> None:
        assert _redact_value("search_interval_hours", "6") == "6"

    def test_empty_value_for_secret(self) -> None:
        assert _redact_value("comicvine_api_key", "") == "[REDACTED]"

    def test_case_sensitive_key_match(self) -> None:
        # password is case-insensitive in the substring check
        assert _redact_value("DB_PASSWORD", "val") == "[REDACTED]"

    def test_secret_substring_in_key(self) -> None:
        assert _redact_value("my_secret_thing", "val") == "[REDACTED]"


@pytest.mark.asyncio
async def test_download_history_redacts_airdcpp_remote_path_only() -> None:
    from pullbox.models.download import DownloadClientType, DownloadState
    from pullbox.services.diagnostic_service import _collect_download_history

    air_path = "/srv/airdcpp/completed/private-hub/Secret Comic 001.cbz"
    direct_path = "/downloads/public-example.cbz"
    rows = [
        SimpleNamespace(
            id=1,
            issue_id=11,
            title="Secret Comic 001.cbz",
            state=DownloadState.COMPLETED,
            download_client=DownloadClientType.AIRDCPP,
            file_size=123,
            downloaded_path=air_path,
            final_path=None,
            error_message=None,
            retry_count=0,
            created_at=None,
            completed_at=None,
            imported_at=None,
        ),
        SimpleNamespace(
            id=2,
            issue_id=12,
            title="Public Example.cbz",
            state=DownloadState.COMPLETED,
            download_client=DownloadClientType.DIRECT,
            file_size=456,
            downloaded_path=direct_path,
            final_path=None,
            error_message=None,
            retry_count=0,
            created_at=None,
            completed_at=None,
            imported_at=None,
        ),
    ]
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session = AsyncMock()
    session.execute.return_value = result

    collected = await _collect_download_history(session)

    assert collected[0]["downloaded_path"] == "[REDACTED]"
    assert air_path not in json.dumps(collected)
    assert collected[1]["downloaded_path"] == direct_path


class TestCollectLogFiles:
    """Log file collection handles edge cases."""

    def test_empty_dir_returns_empty(self, tmp_path: object) -> None:
        from pathlib import Path

        from pullbox.services.diagnostic_service import _collect_log_files

        empty_dir = Path(str(tmp_path)) / "logs"
        empty_dir.mkdir()
        result = _collect_log_files(empty_dir)
        assert result == []

    def test_nonexistent_dir_returns_empty(self) -> None:
        from pathlib import Path

        from pullbox.services.diagnostic_service import _collect_log_files

        result = _collect_log_files(Path("/nonexistent/logs/dir"))
        assert result == []

    def test_collects_recent_log_files(self, tmp_path: object) -> None:
        from pullbox.services.diagnostic_service import _collect_log_files

        logs_dir = Path(str(tmp_path))
        (logs_dir / "pullbox.log").write_text("line1\nline2\n")
        (logs_dir / "pullbox.log.1").write_text("old line\n")
        (logs_dir / "startup.log").write_text("boot line\n")

        result = _collect_log_files(logs_dir)
        assert len(result) == 3
        names = {name for name, _ in result}
        assert "pullbox.log" in names
        assert "pullbox.log.1" in names
        assert "startup.log" in names

    def test_truncates_large_log_file(self, tmp_path: object) -> None:
        from pathlib import Path

        from pullbox.services.diagnostic_service import (
            _MAX_LOG_FILE_BYTES,
            _collect_log_files,
        )

        logs_dir = Path(str(tmp_path))
        # Create a file larger than the max
        large_content = b"x" * (_MAX_LOG_FILE_BYTES + 1000)
        (logs_dir / "pullbox.log").write_bytes(large_content)

        result = _collect_log_files(logs_dir)
        assert len(result) == 1
        _, content = result[0]
        assert content.startswith(b"[... truncated ...]")
        assert len(content) <= _MAX_LOG_FILE_BYTES + 100  # header overhead

    def test_skips_non_log_files(self, tmp_path: object) -> None:
        from pathlib import Path

        from pullbox.services.diagnostic_service import _collect_log_files

        logs_dir = Path(str(tmp_path))
        (logs_dir / "pullbox.log").write_text("log content")
        (logs_dir / "readme.txt").write_text("not a log")

        result = _collect_log_files(logs_dir)
        assert len(result) == 1
        assert result[0][0] == "pullbox.log"


@pytest.mark.asyncio
class TestDiagnosticPackageIntegration:
    """Full package creation with a real database session."""

    @staticmethod
    def _patch_runtime_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> object:
        from pullbox.config import PullboxSettings

        data_dir = tmp_path / "data"
        logs_dir = tmp_path / "logs"
        library_root = tmp_path / "library"
        covers_dir = tmp_path / "covers"
        temp_dir = tmp_path / "tmp"
        backup_dir = tmp_path / "backups"
        for directory in (data_dir, logs_dir, library_root, covers_dir, temp_dir, backup_dir):
            directory.mkdir(parents=True, exist_ok=True)

        settings = PullboxSettings(
            db_url="sqlite+aiosqlite:///:memory:",
            data_dir=data_dir,
            logs_dir=logs_dir,
            library_root=library_root,
            covers_dir=covers_dir,
            temp_dir=temp_dir,
            backup_dir=backup_dir,
            secret_key="super-secret-bootstrap-key",
            comicvine_api_key="cv-secret",
            startup_update_check_enabled=False,
            sqlite_journal_mode="DELETE",
        )
        monkeypatch.setattr("pullbox.config.get_settings", lambda: settings)
        return settings

    async def test_package_is_valid_zip(
        self,
        db_session: object,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from pullbox.services.diagnostic_service import create_diagnostic_package

        self._patch_runtime_settings(monkeypatch, tmp_path)
        zip_bytes, filename = await create_diagnostic_package(db_session)  # type: ignore[arg-type]
        assert filename.startswith("pullbox-diagnostic-")
        assert filename.endswith(".zip")

        # Verify it's a valid ZIP
        buf = io.BytesIO(zip_bytes)
        assert zipfile.is_zipfile(buf)

    async def test_package_contains_expected_files(
        self,
        db_session: object,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from pullbox.services.diagnostic_service import create_diagnostic_package

        settings = self._patch_runtime_settings(monkeypatch, tmp_path)
        (settings.logs_dir / "startup.log").write_text("Running database migrations...\n")
        (settings.data_dir / "config.xml").write_text(
            (
                "<?xml version='1.0' encoding='utf-8'?>\n"
                "<Config>\n"
                "  <SecretKey>abc123</SecretKey>\n"
                "</Config>\n"
            ),
            encoding="utf-8",
        )
        zip_bytes, _ = await create_diagnostic_package(db_session)  # type: ignore[arg-type]
        buf = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            # Check for expected JSON files (prefix varies by timestamp)
            base_files = {n.split("/", 1)[1] for n in names if "/" in n}
            assert "system_info.json" in base_files
            assert "bootstrap_settings.json" in base_files
            assert "container_runtime.json" in base_files
            assert "config.json" in base_files
            assert "config_xml.xml" in base_files
            assert "health_status.json" in base_files
            assert "health_history.json" in base_files
            assert "database_stats.json" in base_files
            assert "sqlite_runtime.json" in base_files
            assert "download_history.json" in base_files
            assert "installed_packages.json" in base_files
            assert "utility_jobs.json" in base_files
            assert "utility_job_logs.json" in base_files
            assert any(name.endswith("/logs/startup.log") for name in names)

    async def test_system_info_has_expected_fields(
        self,
        db_session: object,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from pullbox.services.diagnostic_service import create_diagnostic_package

        self._patch_runtime_settings(monkeypatch, tmp_path)
        zip_bytes, _ = await create_diagnostic_package(db_session)  # type: ignore[arg-type]
        buf = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(buf) as zf:
            for name in zf.namelist():
                if name.endswith("system_info.json"):
                    data = json.loads(zf.read(name))
                    assert "pullbox_version" in data
                    assert "python_version" in data
                    assert "platform" in data
                    assert "cpu_count" in data
                    assert "collected_at" in data
                    break
            else:
                pytest.fail("system_info.json not found in package")

    async def test_config_redacts_secrets(
        self,
        db_session: object,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from pullbox.models.config import SystemConfig
        from pullbox.services.diagnostic_service import create_diagnostic_package

        settings = self._patch_runtime_settings(monkeypatch, tmp_path)
        (settings.data_dir / "config.xml").write_text(
            (
                "<?xml version='1.0' encoding='utf-8'?>\n"
                "<Config>\n"
                "  <SecretKey>topsecret</SecretKey>\n"
                "</Config>\n"
            ),
            encoding="utf-8",
        )
        # Add a secret config entry
        db_session.add(  # type: ignore[union-attr]
            SystemConfig(
                key="comicvine_api_key",
                value="super_secret_key_value",
                value_type="secret",
            )
        )
        await db_session.flush()  # type: ignore[union-attr]

        zip_bytes, _ = await create_diagnostic_package(db_session)  # type: ignore[arg-type]
        buf = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(buf) as zf:
            found_config_json = False
            found_config_xml = False
            for name in zf.namelist():
                if name.endswith("config.json"):
                    found_config_json = True
                    data = json.loads(zf.read(name))
                    cv_entries = [e for e in data if e["key"] == "comicvine_api_key"]
                    assert len(cv_entries) == 1
                    assert cv_entries[0]["value"] == "[REDACTED]"
                    assert "super_secret_key_value" not in json.dumps(data)
                if name.endswith("config_xml.xml"):
                    found_config_xml = True
                    xml_text = zf.read(name).decode("utf-8")
                    assert "[REDACTED]" in xml_text
                    assert "topsecret" not in xml_text
            assert found_config_json is True
            assert found_config_xml is True

    async def test_database_stats_has_row_counts(
        self,
        db_session: object,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from pullbox.services.diagnostic_service import create_diagnostic_package

        self._patch_runtime_settings(monkeypatch, tmp_path)
        zip_bytes, _ = await create_diagnostic_package(db_session)  # type: ignore[arg-type]
        buf = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(buf) as zf:
            for name in zf.namelist():
                if name.endswith("database_stats.json"):
                    data = json.loads(zf.read(name))
                    assert "row_counts" in data
                    assert "series" in data["row_counts"]
                    assert "issues" in data["row_counts"]
                    break
            else:
                pytest.fail("database_stats.json not found")

    async def test_package_size_is_reasonable(
        self,
        db_session: object,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from pullbox.services.diagnostic_service import create_diagnostic_package

        self._patch_runtime_settings(monkeypatch, tmp_path)
        zip_bytes, _ = await create_diagnostic_package(db_session)  # type: ignore[arg-type]
        # Should be well under 50MB for an empty/test database
        assert len(zip_bytes) < 50 * 1024 * 1024

    async def test_package_includes_utility_jobs_and_logs_as_json(
        self,
        db_session: object,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from pullbox.services.diagnostic_service import create_diagnostic_package
        from pullbox.utilities.models import UtilityJob, UtilityJobLog

        self._patch_runtime_settings(monkeypatch, tmp_path)
        db_session.add(  # type: ignore[union-attr]
            UtilityJob(
                id="diag-utility-job-1",
                job_type="integrity_check",
                display_name="Diagnostic Utility Job",
                state="FAILED",
                config="{}",
                total_items=3,
                completed_items=1,
                failed_items=2,
                skipped_items=0,
                warning_count=1,
                queue_position=None,
                created_at="2026-04-05T08:00:00+00:00",
                started_at="2026-04-05T08:01:00+00:00",
                completed_at="2026-04-05T08:02:00+00:00",
                error_message="Failed integrity scan",
            )
        )
        db_session.add(  # type: ignore[union-attr]
            UtilityJobLog(
                job_id="diag-utility-job-1",
                timestamp="2026-04-05T08:01:30+00:00",
                level="DEBUG",
                message="Checksum mismatch detail",
                file_path="/comics/test.cbz",
                extra='{"expected":"abc","actual":"def"}',
            )
        )
        await db_session.flush()  # type: ignore[union-attr]

        zip_bytes, _ = await create_diagnostic_package(db_session)  # type: ignore[arg-type]
        buf = io.BytesIO(zip_bytes)

        with zipfile.ZipFile(buf) as zf:
            utility_jobs = None
            utility_logs = None
            for name in zf.namelist():
                if name.endswith("utility_jobs.json"):
                    utility_jobs = json.loads(zf.read(name))
                elif name.endswith("utility_job_logs.json"):
                    utility_logs = json.loads(zf.read(name))

        assert utility_jobs is not None
        assert utility_logs is not None
        assert utility_jobs[0]["id"] == "diag-utility-job-1"
        assert utility_jobs[0]["job_type"] == "integrity_check"
        assert utility_logs[0]["job_id"] == "diag-utility-job-1"
        assert utility_logs[0]["level"] == "DEBUG"
        assert utility_logs[0]["extra"]["expected"] == "abc"

    async def test_bootstrap_and_runtime_artifacts_are_sanitized(
        self,
        db_session: object,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from pullbox.services.diagnostic_service import create_diagnostic_package

        self._patch_runtime_settings(monkeypatch, tmp_path)
        zip_bytes, _ = await create_diagnostic_package(db_session)  # type: ignore[arg-type]
        buf = io.BytesIO(zip_bytes)

        with zipfile.ZipFile(buf) as zf:
            bootstrap = None
            sqlite_runtime = None
            container_runtime = None
            for name in zf.namelist():
                if name.endswith("bootstrap_settings.json"):
                    bootstrap = json.loads(zf.read(name))
                elif name.endswith("sqlite_runtime.json"):
                    sqlite_runtime = json.loads(zf.read(name))
                elif name.endswith("container_runtime.json"):
                    container_runtime = json.loads(zf.read(name))

        assert bootstrap is not None
        assert container_runtime is not None
        assert sqlite_runtime is not None
        assert bootstrap["secret_key"] == "[REDACTED]"
        assert bootstrap["comicvine_api_key"] == "[REDACTED]"
        assert "db_url" in sqlite_runtime
        assert "quick_check" in sqlite_runtime
        assert "mount_paths" in container_runtime

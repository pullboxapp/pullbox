"""Unit tests for import job ORM models and enums."""

from sqlalchemy import inspect

from pullbox.models.import_job import (
    ImportedSeries,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)


class TestEnums:
    """Verify all import enums have the expected members."""

    def test_import_source_type_members(self) -> None:
        assert ImportSourceType.FILESYSTEM == "filesystem"
        assert ImportSourceType.MYLAR3 == "mylar3"
        assert len(ImportSourceType) == 2

    def test_import_job_status_members(self) -> None:
        expected = {
            "pending",
            "scanning",
            "analyzing",
            "matching",
            "file_matching",
            "review",
            "importing",
            "stalled",
            "pausing",
            "paused",
            "cancelling",
            "rolling_back",
            "rolled_back",
            "completed",
            "failed",
            "cancelled",
        }
        assert {s.value for s in ImportJobStatus} == expected

    def test_import_series_status_members(self) -> None:
        expected = {
            "pending",
            "matched",
            "no_match",
            "recovery_pending",
            "duplicate",
            "confirmed",
            "skipped",
            "importing",
            "imported",
            "failed",
        }
        assert {s.value for s in ImportSeriesStatus} == expected


class TestImportJobModel:
    """Verify ImportJob model structure."""

    def test_tablename(self) -> None:
        assert ImportJob.__tablename__ == "import_jobs"

    def test_column_defaults(self) -> None:
        """Verify server-side defaults are configured (applied at INSERT, not construction)."""
        cols = {c.name: c for c in ImportJob.__table__.columns}
        assert cols["status"].default.arg is ImportJobStatus.PENDING
        assert cols["scan_total_files"].default.arg == 0
        assert cols["scan_total_dirs"].default.arg == 0
        assert cols["series_found"].default.arg == 0
        assert cols["series_duplicate"].default.arg == 0
        assert cols["series_matched"].default.arg == 0
        assert cols["series_no_match"].default.arg == 0
        assert cols["series_new"].default.arg == 0
        assert cols["series_imported"].default.arg == 0
        assert cols["series_failed"].default.arg == 0

    def test_settings_defaults(self) -> None:
        """Verify server-side defaults for settings columns."""
        cols = {c.name: c for c in ImportJob.__table__.columns}
        assert cols["monitored"].default.arg is False
        assert cols["search_on_add"].default.arg is False
        assert cols["cv_match_threshold"].default.arg == 0.70
        assert cols["auto_accept_high_confidence"].default.arg is True
        assert cols["skip_no_match"].default.arg is False

    def test_nullable_timestamps(self) -> None:
        job = ImportJob(source_path="/tmp/comics", source_type=ImportSourceType.FILESYSTEM)
        assert job.scan_started_at is None
        assert job.scan_completed_at is None
        assert job.match_started_at is None
        assert job.match_completed_at is None
        assert job.import_started_at is None
        assert job.import_completed_at is None

    def test_nullable_error_message(self) -> None:
        job = ImportJob(source_path="/tmp/comics", source_type=ImportSourceType.FILESYSTEM)
        assert job.error_message is None

    def test_series_relationship_exists(self) -> None:
        mapper = inspect(ImportJob)
        rel_names = {r.key for r in mapper.relationships}
        assert "series" in rel_names
        assert "logs" in rel_names
        assert "target_library_root" in rel_names

    def test_mylar3_path_map_default(self) -> None:
        cols = {c.name: c for c in ImportJob.__table__.columns}
        assert cols["mylar3_path_map"].default.is_callable
        assert str(cols["mylar3_path_map"].server_default.arg) == "{}"
        assert cols["mylar3_path_map_confirmed"].default.arg is False
        assert str(cols["mylar3_path_map_confirmed"].server_default.arg) == "0"


class TestImportedSeriesModel:
    """Verify ImportedSeries model structure."""

    def test_tablename(self) -> None:
        assert ImportedSeries.__tablename__ == "import_series"

    def test_column_defaults(self) -> None:
        """Verify server-side defaults are configured (applied at INSERT, not construction)."""
        cols = {c.name: c for c in ImportedSeries.__table__.columns}
        assert cols["status"].default.arg is ImportSeriesStatus.PENDING
        assert cols["file_count"].default.arg == 0
        assert cols["has_files"].default.arg is True
        assert cols["sample_paths"].default.is_callable
        assert str(cols["sample_paths"].server_default.arg) == "[]"

    def test_nullable_cv_fields(self) -> None:
        series = ImportedSeries(import_job_id=1, raw_series_name="Batman")
        assert series.cv_id is None
        assert series.cv_title is None
        assert series.cv_year is None
        assert series.cv_publisher is None
        assert series.cv_issue_count is None
        assert series.cv_url is None
        assert series.cv_match_score is None
        assert series.cv_match_method is None
        assert series.user_selected_cv_id is None

    def test_relationship_exists(self) -> None:
        mapper = inspect(ImportedSeries)
        rel_names = {r.key for r in mapper.relationships}
        assert "import_job" in rel_names
        assert "series" in rel_names

    def test_table_args_index(self) -> None:
        indexes = {idx.name for idx in ImportedSeries.__table__.indexes}
        assert "ix_import_series_job_status" in indexes


class TestImportJobLogModel:
    """Verify ImportJobLog model structure."""

    def test_tablename(self) -> None:
        assert ImportJobLog.__tablename__ == "import_job_logs"

    def test_column_defaults(self) -> None:
        """Verify server-side defaults are configured (applied at INSERT, not construction)."""
        cols = {c.name: c for c in ImportJobLog.__table__.columns}
        assert cols["data"].default.is_callable
        assert str(cols["data"].server_default.arg) == "{}"

    def test_relationship_exists(self) -> None:
        mapper = inspect(ImportJobLog)
        rel_names = {r.key for r in mapper.relationships}
        assert "import_job" in rel_names

    def test_table_args_index(self) -> None:
        indexes = {idx.name for idx in ImportJobLog.__table__.indexes}
        assert "ix_import_job_logs_job_id_ts" in indexes

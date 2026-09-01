"""Tests for Task R-3.2 — ImportedFile model, enums, and schemas.

Verifies the ImportedFile ORM model, ImportedFileStatus enum,
relationships, cascade deletes, file counter fields, and Pydantic schemas.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
)
from pullbox.schemas.import_job import (
    ConflictResolution,
    FileConflictGroup,
    FileMatchOverride,
    ImportedFileRead,
)


class TestImportedFileStatusEnum:
    """ImportedFileStatus has correct values."""

    def test_all_members(self) -> None:
        expected = {
            "pending",
            "matched",
            "safety_blocked",
            "safety_approved",
            "duplicate_file",
            "already_owned",
            "conflict",
            "no_match",
            "confirmed",
            "skipped",
            "imported",
            "failed",
        }
        assert {s.value for s in ImportedFileStatus} == expected

    def test_member_count(self) -> None:
        assert len(ImportedFileStatus) == 12


class TestFileMatchingJobStatus:
    """ImportJobStatus includes FILE_MATCHING."""

    def test_file_matching_exists(self) -> None:
        assert ImportJobStatus.FILE_MATCHING == "file_matching"

    def test_all_statuses(self) -> None:
        expected = {
            "pending",
            "scanning",
            "pausing",
            "paused",
            "analyzing",
            "matching",
            "file_matching",
            "review",
            "importing",
            "stalled",
            "cancelling",
            "rolling_back",
            "rolled_back",
            "completed",
            "failed",
            "cancelled",
        }
        assert {s.value for s in ImportJobStatus} == expected


class TestImportedFileModel:
    """ImportedFile model structure and defaults."""

    def test_tablename(self) -> None:
        assert ImportedFile.__tablename__ == "import_files"

    def test_all_columns_exist(self) -> None:
        cols = {c.name for c in ImportedFile.__table__.columns}
        expected = {
            "id",
            "import_job_id",
            "import_series_id",
            "file_path",
            "file_name",
            "file_size",
            "file_format",
            "parsed_series",
            "parsed_issue_number",
            "parsed_year",
            "has_comicinfo",
            "comicvine_issue_id",
            "issue_number_raw",
            "status",
            "matched_issue_id",
            "match_confidence",
            "match_method",
            "conflict_group_id",
            "is_preferred",
            "library_file_id",
            "error_message",
            "diagnostics",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols)

    def test_column_defaults(self) -> None:
        cols = {c.name: c for c in ImportedFile.__table__.columns}
        assert cols["status"].default.arg is ImportedFileStatus.PENDING
        assert cols["has_comicinfo"].default.arg is False
        assert cols["is_preferred"].default.arg is False

    def test_nullable_match_fields(self) -> None:
        f = ImportedFile(
            import_job_id=1,
            import_series_id=1,
            file_path="/tmp/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=1024,
            file_format="cbz",
        )
        assert f.matched_issue_id is None
        assert f.match_confidence is None
        assert f.match_method is None
        assert f.conflict_group_id is None
        assert f.library_file_id is None
        assert f.error_message is None
        assert f.diagnostics in (None, {})

    def test_relationship_to_import_job(self) -> None:
        mapper = inspect(ImportedFile)
        rel_names = {r.key for r in mapper.relationships}
        assert "import_job" in rel_names

    def test_relationship_to_import_series(self) -> None:
        mapper = inspect(ImportedFile)
        rel_names = {r.key for r in mapper.relationships}
        assert "import_series" in rel_names

    def test_index_on_job_id(self) -> None:
        indexes = {idx.name for idx in ImportedFile.__table__.indexes}
        assert "ix_import_files_job_series" in indexes

    def test_index_on_matched_issue_id(self) -> None:
        indexes = {idx.name for idx in ImportedFile.__table__.indexes}
        assert "ix_import_files_matched_issue_id" in indexes

    def test_index_on_import_series_id(self) -> None:
        indexes = {idx.name for idx in ImportedFile.__table__.indexes}
        assert "ix_import_files_import_series_id" in indexes

    def test_index_on_duplicate_file_reference(self) -> None:
        indexes = {idx.name for idx in ImportedFile.__table__.indexes}
        assert "ix_import_files_duplicate_of_file_id" in indexes


class TestImportedSeriesFileCounters:
    """ImportedSeries has file counter fields."""

    def test_file_counter_columns_exist(self) -> None:
        cols = {c.name for c in ImportedSeries.__table__.columns}
        assert "files_total" in cols
        assert "files_matched" in cols
        assert "files_conflict" in cols
        assert "files_no_match" in cols
        assert "files_imported" in cols
        assert "files_failed" in cols
        assert "diagnostics" in cols

    def test_file_counter_defaults(self) -> None:
        cols = {c.name: c for c in ImportedSeries.__table__.columns}
        assert cols["files_total"].default.arg == 0
        assert cols["files_matched"].default.arg == 0
        assert cols["files_conflict"].default.arg == 0
        assert cols["files_no_match"].default.arg == 0
        assert cols["files_imported"].default.arg == 0
        assert cols["files_failed"].default.arg == 0

    def test_files_relationship(self) -> None:
        mapper = inspect(ImportedSeries)
        rel_names = {r.key for r in mapper.relationships}
        assert "files" in rel_names


class TestImportJobFileCounters:
    """ImportJob has file counter fields."""

    def test_file_counter_columns_exist(self) -> None:
        cols = {c.name for c in ImportJob.__table__.columns}
        assert "total_files_found" in cols
        assert "total_files_matched" in cols
        assert "total_files_conflict" in cols
        assert "total_files_no_match" in cols
        assert "total_files_imported" in cols
        assert "total_files_failed" in cols

    def test_file_counter_defaults(self) -> None:
        cols = {c.name: c for c in ImportJob.__table__.columns}
        assert cols["total_files_found"].default.arg == 0
        assert cols["total_files_matched"].default.arg == 0
        assert cols["total_files_conflict"].default.arg == 0
        assert cols["total_files_no_match"].default.arg == 0
        assert cols["total_files_imported"].default.arg == 0
        assert cols["total_files_failed"].default.arg == 0

    def test_files_relationship(self) -> None:
        mapper = inspect(ImportJob)
        rel_names = {r.key for r in mapper.relationships}
        assert "files" in rel_names

    def test_staged_scan_relationships_use_database_side_cascade_deletes(self) -> None:
        job_relationships = inspect(ImportJob).relationships
        assert all(
            job_relationships[name].passive_deletes is True
            for name in ("series", "files", "logs", "actions", "story_arcs")
        )
        assert inspect(ImportedSeries).relationships["files"].passive_deletes is True


class TestImportedFileReadSchema:
    """ImportedFileRead Pydantic schema serialization."""

    def test_all_fields_serialized(self) -> None:
        now = datetime.now(tz=UTC)
        data = ImportedFileRead(
            id=1,
            import_job_id=1,
            import_series_id=10,
            file_path="/comics/Batman/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=5242880,
            file_format="cbz",
            parsed_series="Batman",
            parsed_issue_number=1.0,
            parsed_year=2016,
            has_comicinfo=True,
            comicvine_issue_id=12345,
            issue_number_raw="001",
            status=ImportedFileStatus.MATCHED,
            matched_issue_id=42,
            match_confidence="high",
            match_method="comicvine_id",
            conflict_group_id=None,
            is_preferred=False,
            library_file_id=None,
            error_message=None,
            diagnostics={"kind": "file_conflict"},
            created_at=now,
        )
        assert data.id == 1
        assert data.file_format == "cbz"
        assert data.matched_issue_id == 42
        assert data.match_confidence == "high"
        assert data.diagnostics["kind"] == "file_conflict"

    def test_minimal_fields(self) -> None:
        now = datetime.now(tz=UTC)
        data = ImportedFileRead(
            id=1,
            import_job_id=1,
            import_series_id=10,
            file_path="/tmp/scan.cbz",
            file_name="scan.cbz",
            file_size=0,
            file_format="cbz",
            parsed_series=None,
            parsed_issue_number=None,
            parsed_year=None,
            has_comicinfo=False,
            comicvine_issue_id=None,
            issue_number_raw=None,
            status=ImportedFileStatus.PENDING,
            matched_issue_id=None,
            match_confidence=None,
            match_method=None,
            conflict_group_id=None,
            is_preferred=False,
            library_file_id=None,
            error_message=None,
            diagnostics={},
            created_at=now,
        )
        assert data.parsed_series is None
        assert data.status == ImportedFileStatus.PENDING


class TestFileConflictGroupSchema:
    """FileConflictGroup Pydantic schema."""

    def test_valid_group(self) -> None:
        now = datetime.now(tz=UTC)
        file1 = ImportedFileRead(
            id=1,
            import_job_id=1,
            import_series_id=10,
            file_path="/comics/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_series="Batman",
            parsed_issue_number=1.0,
            parsed_year=None,
            has_comicinfo=True,
            comicvine_issue_id=None,
            issue_number_raw="001",
            status=ImportedFileStatus.CONFLICT,
            matched_issue_id=42,
            match_confidence="high",
            match_method="issue_number",
            conflict_group_id=1,
            is_preferred=True,
            library_file_id=None,
            error_message=None,
            diagnostics={"kind": "file_conflict"},
            created_at=now,
        )
        file2 = ImportedFileRead(
            id=2,
            import_job_id=1,
            import_series_id=10,
            file_path="/comics/Batman 001 (2).cbz",
            file_name="Batman 001 (2).cbz",
            file_size=512,
            file_format="cbz",
            parsed_series="Batman",
            parsed_issue_number=1.0,
            parsed_year=None,
            has_comicinfo=False,
            comicvine_issue_id=None,
            issue_number_raw="001",
            status=ImportedFileStatus.CONFLICT,
            matched_issue_id=42,
            match_confidence="medium",
            match_method="issue_number",
            conflict_group_id=1,
            is_preferred=False,
            library_file_id=None,
            error_message=None,
            diagnostics={"kind": "file_conflict"},
            created_at=now,
        )
        group = FileConflictGroup(
            conflict_group_id=1,
            matched_issue_id=42,
            issue_title="Batman #1",
            files=[file1, file2],
        )
        assert group.conflict_group_id == 1
        assert len(group.files) == 2
        assert group.issue_title == "Batman #1"

    def test_empty_files_rejected(self) -> None:
        with pytest.raises(ValidationError, match="files"):
            FileConflictGroup(
                conflict_group_id=1,
                matched_issue_id=42,
                issue_title="Batman #1",
                files=[],
            )


class TestConflictResolutionSchema:
    """ConflictResolution Pydantic schema."""

    def test_valid_resolution(self) -> None:
        res = ConflictResolution(
            conflict_group_id=1,
            chosen_file_id=5,
        )
        assert res.conflict_group_id == 1
        assert res.chosen_file_id == 5

    def test_missing_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ConflictResolution(conflict_group_id=1)  # type: ignore[call-arg]


class TestFileMatchOverrideSchema:
    """FileMatchOverride Pydantic schema."""

    def test_valid_override(self) -> None:
        override = FileMatchOverride(
            imported_file_id=1,
            issue_id=42,
        )
        assert override.imported_file_id == 1
        assert override.issue_id == 42

    def test_positive_ids_required(self) -> None:
        with pytest.raises(ValidationError):
            FileMatchOverride(imported_file_id=0, issue_id=42)
        with pytest.raises(ValidationError):
            FileMatchOverride(imported_file_id=1, issue_id=0)

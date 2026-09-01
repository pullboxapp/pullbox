"""Unit tests for import job Pydantic schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from pullbox.models.import_job import (
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.schemas.import_job import (
    ConfirmImportRequest,
    ImportedSeriesRead,
    ImportJobCreate,
    ImportJobListItem,
    ImportJobLogEntry,
    ImportJobLogsResponse,
    ImportJobRead,
    ImportPreviewResponse,
    ImportProgressEvent,
    SeriesSearchOverride,
)


class TestImportJobCreate:
    """Validate ImportJobCreate request schema."""

    def test_valid_create(self, tmp_path: object) -> None:
        data = ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
        )
        assert data.source_type == ImportSourceType.FILESYSTEM
        assert data.monitored is False
        assert data.search_on_add is None

    def test_missing_source_path(self) -> None:
        with pytest.raises(ValidationError, match="source_path"):
            ImportJobCreate(source_type=ImportSourceType.FILESYSTEM)  # type: ignore[call-arg]

    def test_missing_source_type(self, tmp_path: object) -> None:
        with pytest.raises(ValidationError, match="source_type"):
            ImportJobCreate(source_path=str(tmp_path))  # type: ignore[call-arg]

    def test_invalid_source_type(self, tmp_path: object) -> None:
        with pytest.raises(ValidationError):
            ImportJobCreate(
                source_path=str(tmp_path),
                source_type="invalid",  # type: ignore[arg-type]
            )

    def test_nonexistent_path_rejected(self) -> None:
        with pytest.raises(ValidationError, match="does not exist"):
            ImportJobCreate(
                source_path="/nonexistent/path/that/does/not/exist",
                source_type=ImportSourceType.FILESYSTEM,
            )

    def test_path_resolved(self, tmp_path: object) -> None:
        data = ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
        )
        assert "/" in data.source_path  # resolved to absolute

    def test_mylar3_path_map_requires_confirmation_even_when_empty(self, tmp_path: object) -> None:
        with pytest.raises(ValidationError, match="confirm the Mylar path mapping"):
            ImportJobCreate(
                source_path=str(tmp_path),
                source_type=ImportSourceType.MYLAR3,
            )

    def test_mylar3_path_map_accepts_confirmed_empty_identity_snapshot(
        self, tmp_path: object
    ) -> None:
        data = ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.MYLAR3,
            mylar3_path_map_confirmed=True,
        )
        assert data.mylar3_path_map == {}

    def test_mylar3_path_map_populated(self, tmp_path: object) -> None:
        data = ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.MYLAR3,
            mylar3_path_map={"/data": "/mnt/storage"},
            mylar3_path_map_confirmed=True,
        )
        assert data.mylar3_path_map == {"/data": "/mnt/storage"}

    def test_mylar3_path_map_requires_explicit_confirmation(self, tmp_path: object) -> None:
        with pytest.raises(ValidationError, match="confirm the Mylar path mapping"):
            ImportJobCreate(
                source_path=str(tmp_path),
                source_type=ImportSourceType.MYLAR3,
                mylar3_path_map={"/data": "/mnt/storage"},
            )

    @pytest.mark.parametrize(
        "path_map",
        [
            {"relative": "/mnt/storage"},
            {"/data": "relative"},
            {"/data/../private": "/mnt/storage"},
            {"/data": "/mnt/storage/../private"},
            {"/data\x00": "/mnt/storage"},
        ],
    )
    def test_mylar3_path_map_rejects_unsafe_paths(
        self,
        tmp_path: object,
        path_map: dict[str, str],
    ) -> None:
        with pytest.raises(ValidationError, match="Mylar path mapping"):
            ImportJobCreate(
                source_path=str(tmp_path),
                source_type=ImportSourceType.MYLAR3,
                mylar3_path_map=path_map,
                mylar3_path_map_confirmed=True,
            )

    def test_mylar3_path_map_rejects_conflicting_nested_mappings(
        self,
        tmp_path: object,
    ) -> None:
        with pytest.raises(ValidationError, match="overlapping"):
            ImportJobCreate(
                source_path=str(tmp_path),
                source_type=ImportSourceType.MYLAR3,
                mylar3_path_map={
                    "/data": "/library",
                    "/data/archive": "/other-library",
                },
                mylar3_path_map_confirmed=True,
            )

    def test_mylar3_path_map_accepts_compatible_nested_mappings(
        self,
        tmp_path: object,
    ) -> None:
        data = ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.MYLAR3,
            mylar3_path_map={
                "/data": "/library",
                "/data/archive": "/library/archive",
            },
            mylar3_path_map_confirmed=True,
        )

        assert len(data.mylar3_path_map) == 2

    def test_mylar3_path_map_is_bounded(self, tmp_path: object) -> None:
        with pytest.raises(ValidationError, match="at most 16"):
            ImportJobCreate(
                source_path=str(tmp_path),
                source_type=ImportSourceType.MYLAR3,
                mylar3_path_map={f"/stored/{index}": f"/library/{index}" for index in range(17)},
                mylar3_path_map_confirmed=True,
            )

    def test_mylar3_path_map_rejected_for_folder_import(self, tmp_path: object) -> None:
        with pytest.raises(ValidationError, match="only supported for Mylar"):
            ImportJobCreate(
                source_path=str(tmp_path),
                source_type=ImportSourceType.FILESYSTEM,
                mylar3_path_map={"/data": "/mnt/storage"},
                mylar3_path_map_confirmed=True,
            )

    def test_advanced_options_defaults(self, tmp_path: object) -> None:
        data = ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
        )
        assert data.cv_match_threshold == 0.70
        assert data.min_files_per_series == 1
        assert data.file_formats is None

    def test_advanced_options_custom(self, tmp_path: object) -> None:
        data = ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            cv_match_threshold=0.85,
            min_files_per_series=3,
            file_formats="cbz, cbr",
        )
        assert data.cv_match_threshold == 0.85
        assert data.min_files_per_series == 3
        assert data.file_formats == "cbz, cbr"

    def test_cv_match_threshold_bounds(self, tmp_path: object) -> None:
        with pytest.raises(ValidationError, match="cv_match_threshold"):
            ImportJobCreate(
                source_path=str(tmp_path),
                source_type=ImportSourceType.FILESYSTEM,
                cv_match_threshold=0.49,
            )
        with pytest.raises(ValidationError, match="cv_match_threshold"):
            ImportJobCreate(
                source_path=str(tmp_path),
                source_type=ImportSourceType.FILESYSTEM,
                cv_match_threshold=1.01,
            )

    def test_min_files_per_series_bounds(self, tmp_path: object) -> None:
        with pytest.raises(ValidationError, match="min_files_per_series"):
            ImportJobCreate(
                source_path=str(tmp_path),
                source_type=ImportSourceType.FILESYSTEM,
                min_files_per_series=0,
            )
        with pytest.raises(ValidationError, match="min_files_per_series"):
            ImportJobCreate(
                source_path=str(tmp_path),
                source_type=ImportSourceType.FILESYSTEM,
                min_files_per_series=51,
            )

    def test_file_formats_normalization(self, tmp_path: object) -> None:
        data = ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            file_formats=" .CBZ , .cbr , PDF ",
        )
        assert data.file_formats == "cbz, cbr, pdf"

    def test_file_formats_empty_string_normalized_to_none(self, tmp_path: object) -> None:
        data = ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            file_formats="  , ,  ",
        )
        assert data.file_formats is None

    def test_file_formats_none_stays_none(self, tmp_path: object) -> None:
        data = ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            file_formats=None,
        )
        assert data.file_formats is None


class TestConfirmImportRequest:
    """Validate ConfirmImportRequest schema."""

    def test_valid_request(self) -> None:
        req = ConfirmImportRequest(series_ids=[1, 2, 3])
        assert req.series_ids == [1, 2, 3]

    def test_empty_series_ids_allowed_for_duplicate_file_imports(self) -> None:
        req = ConfirmImportRequest(series_ids=[])
        assert req.series_ids == []

    def test_negative_ids_rejected(self) -> None:
        with pytest.raises(ValidationError, match="positive"):
            ConfirmImportRequest(series_ids=[1, -1, 3])

    def test_zero_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="positive"):
            ConfirmImportRequest(series_ids=[0])

    def test_deduplication(self) -> None:
        req = ConfirmImportRequest(series_ids=[1, 2, 2, 3, 3])
        assert sorted(req.series_ids) == [1, 2, 3]

    def test_optional_overrides(self) -> None:
        req = ConfirmImportRequest(
            series_ids=[1],
            target_library_root_id=5,
            search_on_add=True,
        )
        assert req.target_library_root_id == 5
        assert req.search_on_add is True


class TestSeriesSearchOverride:
    """Validate SeriesSearchOverride schema."""

    def test_valid_override(self) -> None:
        override = SeriesSearchOverride(imported_series_id=1, cv_id=12345)
        assert override.imported_series_id == 1
        assert override.cv_id == 12345

    def test_missing_fields(self) -> None:
        with pytest.raises(ValidationError):
            SeriesSearchOverride(imported_series_id=1)  # type: ignore[call-arg]


class TestImportedSeriesRead:
    """Validate ImportedSeriesRead response schema."""

    def test_all_fields_serialized(self) -> None:
        data = ImportedSeriesRead(
            id=1,
            status=ImportSeriesStatus.MATCHED,
            raw_series_name="Batman",
            raw_year=2016,
            raw_publisher="DC Comics",
            file_count=12,
            has_files=True,
            sample_paths=["/comics/Batman/001.cbz"],
            source_folder="/comics/Batman",
            cv_id=97508,
            cv_title="Batman",
            cv_year=2016,
            cv_publisher="DC Comics",
            cv_issue_count=85,
            cv_url="https://comicvine.gamespot.com/batman/4050-97508/",
            cv_match_score=0.95,
            cv_match_method="exact",
            user_selected_cv_id=None,
            series_id=None,
            error_message=None,
        )
        assert data.id == 1
        assert data.cv_match_score == 0.95
        assert data.sample_paths == ["/comics/Batman/001.cbz"]

    def test_minimal_fields(self) -> None:
        data = ImportedSeriesRead(
            id=1,
            status=ImportSeriesStatus.PENDING,
            raw_series_name="Unknown",
            raw_year=None,
            raw_publisher=None,
            file_count=0,
            has_files=False,
            sample_paths=[],
            source_folder=None,
            cv_id=None,
            cv_title=None,
            cv_year=None,
            cv_publisher=None,
            cv_issue_count=None,
            cv_url=None,
            cv_match_score=None,
            cv_match_method=None,
            user_selected_cv_id=None,
            series_id=None,
            error_message=None,
        )
        assert data.raw_series_name == "Unknown"


class TestImportJobRead:
    """Validate ImportJobRead response schema."""

    def test_full_response(self) -> None:
        now = datetime.now(tz=UTC)
        data = ImportJobRead(
            id=1,
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.COMPLETED,
            scan_total_files=100,
            scan_total_dirs=10,
            series_found=8,
            series_duplicate=2,
            series_matched=5,
            series_no_match=1,
            series_new=0,
            series_imported=5,
            series_failed=0,
            created_at=now,
            scan_started_at=now,
            scan_completed_at=now,
            match_started_at=now,
            match_completed_at=now,
            import_started_at=now,
            import_completed_at=now,
            target_library_root_id=1,
            monitored=True,
            search_on_add=True,
            cv_match_threshold=0.80,
            min_files_per_series=2,
            file_formats="cbz, cbr",
            error_message=None,
        )
        assert data.status == ImportJobStatus.COMPLETED
        assert data.series_imported == 5
        assert data.cv_match_threshold == 0.80
        assert data.min_files_per_series == 2
        assert data.file_formats == "cbz, cbr"


class TestImportJobListItem:
    """Validate ImportJobListItem summary schema."""

    def test_summary_fields_only(self) -> None:
        now = datetime.now(tz=UTC)
        item = ImportJobListItem(
            id=1,
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
            series_found=8,
            series_imported=0,
            series_failed=0,
            created_at=now,
            import_completed_at=None,
        )
        assert item.id == 1
        assert item.status == ImportJobStatus.REVIEW
        # Verify no nested series attribute
        assert not hasattr(item, "series")


class TestImportPreviewResponse:
    """Validate ImportPreviewResponse schema."""

    def test_preview_with_items(self) -> None:
        now = datetime.now(tz=UTC)
        job = ImportJobRead(
            id=1,
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
            scan_total_files=50,
            scan_total_dirs=5,
            series_found=2,
            series_duplicate=0,
            series_matched=2,
            series_no_match=0,
            series_new=0,
            series_imported=0,
            series_failed=0,
            created_at=now,
            scan_started_at=now,
            scan_completed_at=now,
            match_started_at=now,
            match_completed_at=now,
            import_started_at=None,
            import_completed_at=None,
            target_library_root_id=None,
            monitored=False,
            search_on_add=False,
            cv_match_threshold=0.70,
            min_files_per_series=1,
            file_formats=None,
            error_message=None,
        )
        series_item = ImportedSeriesRead(
            id=1,
            status=ImportSeriesStatus.MATCHED,
            raw_series_name="Batman",
            raw_year=2016,
            raw_publisher="DC Comics",
            file_count=12,
            has_files=True,
            sample_paths=[],
            source_folder="/comics/Batman",
            cv_id=97508,
            cv_title="Batman",
            cv_year=2016,
            cv_publisher="DC Comics",
            cv_issue_count=85,
            cv_url=None,
            cv_match_score=0.92,
            cv_match_method="fuzzy",
            user_selected_cv_id=None,
            series_id=None,
            error_message=None,
        )
        preview = ImportPreviewResponse(
            job=job,
            items=[series_item],
            total=1,
            page=1,
            page_size=25,
        )
        assert preview.total == 1
        assert len(preview.items) == 1
        assert preview.items[0].raw_series_name == "Batman"


class TestImportProgressEvent:
    """Validate ImportProgressEvent SSE schema."""

    def test_full_event(self) -> None:
        event = ImportProgressEvent(
            job_id=1,
            status=ImportJobStatus.MATCHING,
            phase="matching",
            progress=42,
            message="Matching Batman (2016)...",
            current_series="Batman",
            current_series_status=ImportSeriesStatus.MATCHED,
            estimated_seconds_remaining=30,
        )
        assert event.job_id == 1
        assert event.phase == "matching"
        assert event.progress == 42
        assert event.current_series == "Batman"
        assert event.estimated_seconds_remaining == 30

    def test_minimal_event(self) -> None:
        event = ImportProgressEvent(
            job_id=1,
            status=ImportJobStatus.SCANNING,
            phase="scanning",
            progress=0,
        )
        assert event.message == ""
        assert event.current_series is None
        assert event.estimated_seconds_remaining is None

    def test_progress_clamped(self) -> None:
        with pytest.raises(ValidationError, match="progress"):
            ImportProgressEvent(
                job_id=1,
                status=ImportJobStatus.SCANNING,
                phase="scanning",
                progress=101,
            )

    def test_progress_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="progress"):
            ImportProgressEvent(
                job_id=1,
                status=ImportJobStatus.SCANNING,
                phase="scanning",
                progress=-1,
            )


class TestImportJobLogEntry:
    """Validate ImportJobLogEntry schema."""

    def test_all_fields_serialized(self) -> None:
        now = datetime.now(tz=UTC)
        entry = ImportJobLogEntry(
            id=1,
            logged_at=now,
            level="INFO",
            event="import_cv_match_found",
            message="Matched Batman to CV 97508",
            data={"raw_series_name": "Batman", "cv_id": 97508},
        )
        assert entry.id == 1
        assert entry.level == "INFO"
        assert entry.event == "import_cv_match_found"
        assert entry.data["cv_id"] == 97508

    def test_nullable_message(self) -> None:
        now = datetime.now(tz=UTC)
        entry = ImportJobLogEntry(
            id=2,
            logged_at=now,
            level="DEBUG",
            event="import_scan_dir",
            message=None,
            data={},
        )
        assert entry.message is None
        assert entry.data == {}


class TestImportJobLogsResponse:
    """Validate ImportJobLogsResponse pagination schema."""

    def test_response_with_items(self) -> None:
        now = datetime.now(tz=UTC)
        entry = ImportJobLogEntry(
            id=1,
            logged_at=now,
            level="INFO",
            event="import_started",
            message="Import job started",
            data={},
        )
        resp = ImportJobLogsResponse(
            job_id=1,
            items=[entry],
            total=1,
            page=1,
            page_size=50,
        )
        assert resp.job_id == 1
        assert len(resp.items) == 1
        assert resp.total == 1
        assert resp.page_size == 50

    def test_empty_response(self) -> None:
        resp = ImportJobLogsResponse(
            job_id=1,
            items=[],
            total=0,
            page=1,
            page_size=50,
        )
        assert resp.items == []
        assert resp.total == 0

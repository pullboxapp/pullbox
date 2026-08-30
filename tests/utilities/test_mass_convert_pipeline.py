"""Tests for UT-2.2 — mass convert pipeline executor.

Verifies the multi-step pipeline (convert → metadata → verify),
partial pipelines, step failure handling, and rollback.

Run:
    pytest tests/utilities/test_mass_convert_pipeline.py -v
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import py7zr
import pytest

from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series
from pullbox.utilities.base_executor import ItemResult
from pullbox.utilities.executors.mass_convert_pipeline import MassConvertPipelineExecutor

# ── Helpers ────────────────────────────────────────────────────


def _create_test_cb7(path: Path, page_count: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(path, "w") as archive:
        for i in range(page_count):
            tmp = path.parent / f"_tmp_{i}.jpg"
            tmp.write_bytes(b"\xff\xd8" + b"X" * 500)
            archive.write(tmp, f"page_{i:03d}.jpg")
            tmp.unlink()
    return path


def _create_test_cbr_as_zip(path: Path, page_count: int = 3) -> Path:
    """Create a .cbr that's actually a ZIP (for testing without unrar)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(page_count):
            zf.writestr(f"page_{i:03d}.jpg", b"\xff\xd8" + b"X" * 500)
    return path


def _create_test_cbz(path: Path, page_count: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(page_count):
            zf.writestr(f"page_{i:03d}.jpg", b"\xff\xd8" + b"X" * 500)
    return path


async def _create_tracked_library_file(
    db_session,
    file_path: Path,
    *,
    library_root_path: Path,
    series_title: str = "Batman",
    issue_number: float = 1.0,
    year_start: int = 2016,
) -> LibraryFile:
    publisher = Publisher(name="DC")
    library_root = LibraryRoot(name="Library", path=str(library_root_path), enabled=True)
    series = Series(
        title=series_title,
        sort_title=series_title,
        year_start=year_start,
        path=str(file_path.parent),
        publisher=publisher,
        library_root=library_root,
        monitored=True,
    )
    issue = Issue(
        series=series,
        issue_number=issue_number,
        title="Issue Title",
        description="A sample issue",
        page_count=24,
        release_date=date(year_start, 1, 1),
    )
    library_file = LibraryFile(
        file_path=str(file_path),
        file_name=file_path.name,
        file_size=file_path.stat().st_size,
        file_format=FileFormat(file_path.suffix.lstrip(".")),
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        issue=issue,
        library_root=library_root,
        has_comicinfo=False,
    )
    db_session.add(library_file)
    await db_session.flush()
    return library_file


# ── Config Validation ──────────────────────────────────────────


class TestValidateConfig:
    """Verify pipeline config validation."""

    def test_valid_config(self) -> None:
        executor = MassConvertPipelineExecutor()
        errors = executor.validate_config(
            {
                "steps": [1, 4],
                "scope": "manual",
                "file_paths": ["/comics/test.cb7"],
            }
        )
        assert errors == []

    def test_missing_steps(self) -> None:
        executor = MassConvertPipelineExecutor()
        errors = executor.validate_config({"scope": "manual"})
        assert any("steps" in e.lower() for e in errors)

    def test_empty_steps(self) -> None:
        executor = MassConvertPipelineExecutor()
        errors = executor.validate_config({"steps": []})
        assert any("steps" in e.lower() for e in errors)

    def test_step_1_required(self) -> None:
        """Step 1 (convert) must always be included."""
        executor = MassConvertPipelineExecutor()
        errors = executor.validate_config({"steps": [2, 4]})
        assert any("step 1" in e.lower() or "convert" in e.lower() for e in errors)

    def test_process_item_fails_safely_when_step_1_is_missing(self, tmp_path: Path) -> None:
        """Direct executor calls should not mutate files when config validation is bypassed."""
        source = _create_test_cbz(tmp_path / "already.cbz")
        trash_dir = tmp_path / ".trash"

        executor = MassConvertPipelineExecutor()
        result = executor.process_item(
            item_data={
                "id": "invalid-steps",
                "file_path": str(source),
                "operation": "pipeline",
                "metadata": {"Series": "Already"},
            },
            job_config={
                "steps": [2],
                "trash_folder": str(trash_dir),
            },
        )

        assert result.result == ItemResult.FAILED
        assert "step 1" in (result.error_message or "").lower()
        assert source.exists()
        assert not trash_dir.exists()

    def test_valid_full_pipeline(self) -> None:
        executor = MassConvertPipelineExecutor()
        errors = executor.validate_config(
            {
                "steps": [1, 2, 4],
                "scope": "manual",
                "file_paths": ["/comics/test.cb7"],
            }
        )
        assert errors == []

    def test_folder_scope_accepts_multiple_scan_folders(self) -> None:
        executor = MassConvertPipelineExecutor()
        errors = executor.validate_config(
            {
                "steps": [1, 4],
                "scope": "folder",
                "scan_folders": ["/comics/Batman (2016)", "/comics/Saga (2012)"],
            }
        )
        assert errors == []


# ── Generate Items ─────────────────────────────────────────────


class TestGenerateItems:
    """Verify item discovery."""

    @pytest.mark.asyncio
    async def test_manual_scope_discovers_files(self, db_session, tmp_path: Path) -> None:
        f1 = _create_test_cb7(tmp_path / "batman.cb7")
        f2 = _create_test_cb7(tmp_path / "saga.cb7")

        executor = MassConvertPipelineExecutor()
        executor._session = db_session  # type: ignore[attr-defined]
        items = await executor.generate_items(
            {
                "steps": [1, 4],
                "scope": "manual",
                "file_paths": [str(f1), str(f2)],
            }
        )

        assert len(items) == 2
        assert all(item["operation"] == "pipeline" for item in items)

    @pytest.mark.asyncio
    async def test_nonexistent_files_excluded(self, db_session, tmp_path: Path) -> None:
        f1 = _create_test_cb7(tmp_path / "exists.cb7")

        executor = MassConvertPipelineExecutor()
        executor._session = db_session  # type: ignore[attr-defined]
        items = await executor.generate_items(
            {
                "steps": [1],
                "scope": "manual",
                "file_paths": [str(f1), "/ghost/nope.cbr"],
            }
        )

        assert len(items) == 1

    @pytest.mark.asyncio
    async def test_empty_file_paths_returns_empty(self, db_session) -> None:
        executor = MassConvertPipelineExecutor()
        executor._session = db_session  # type: ignore[attr-defined]
        items = await executor.generate_items(
            {
                "steps": [1],
                "scope": "manual",
                "file_paths": [],
            }
        )
        assert items == []

    @pytest.mark.asyncio
    async def test_folder_scope_discovers_files_from_multiple_folders(
        self,
        db_session,
        tmp_path: Path,
    ) -> None:
        folder_one = tmp_path / "Batman (2016)"
        folder_two = tmp_path / "Saga (2012)"
        file_one = _create_test_cb7(folder_one / "Batman 001.cb7")
        file_two = _create_test_cb7(folder_two / "Saga 001.cb7")

        executor = MassConvertPipelineExecutor()
        executor._session = db_session  # type: ignore[attr-defined]
        items = await executor.generate_items(
            {
                "steps": [1],
                "scope": "folder",
                "scan_folders": [str(folder_one), str(folder_two)],
            }
        )

        paths = {item["file_path"] for item in items}
        assert str(file_one) in paths
        assert str(file_two) in paths

    @pytest.mark.asyncio
    async def test_library_scope_uses_tracked_files_and_builds_metadata(
        self,
        db_session,
        tmp_path: Path,
    ) -> None:
        library_root = tmp_path / "library"
        series_dir = library_root / "Batman (2016)"
        tracked_file = _create_test_cbr_as_zip(series_dir / "Batman 001.cbr")
        _create_test_cbr_as_zip(series_dir / "ignored.epub")
        await _create_tracked_library_file(
            db_session,
            tracked_file,
            library_root_path=library_root,
        )

        executor = MassConvertPipelineExecutor()
        executor._session = db_session  # type: ignore[attr-defined]
        items = await executor.generate_items(
            {
                "steps": [1, 2],
                "scope": "library",
            }
        )

        assert len(items) == 1
        assert items[0]["file_path"] == str(tracked_file)
        assert items[0]["library_file_id"] is not None
        assert items[0]["metadata_source"] == "library"
        assert items[0]["metadata"]["Series"] == "Batman"
        assert items[0]["metadata"]["Year"] == 2016
        assert items[0]["metadata"]["Title"] == "Issue Title"

    @pytest.mark.asyncio
    async def test_folder_scope_scans_recursively_and_skips_trash_folder(
        self,
        db_session,
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "library"
        target_folder = root / "Batman (2016)"
        nested_file = _create_test_cb7(target_folder / "Nested" / "Batman 001.cb7")
        trash_file = _create_test_cbr_as_zip(target_folder / ".trash" / "Skipped.cbr")
        await _create_tracked_library_file(
            db_session,
            nested_file,
            library_root_path=root,
        )

        executor = MassConvertPipelineExecutor()
        executor._session = db_session  # type: ignore[attr-defined]
        items = await executor.generate_items(
            {
                "steps": [1],
                "scope": "folder",
                "scan_folder": str(target_folder),
                "trash_folder": str(target_folder / ".trash"),
            }
        )

        paths = {item["file_path"] for item in items}
        assert str(nested_file) in paths
        assert str(trash_file) not in paths


# ── Process Item: Full Pipeline ────────────────────────────────


class TestProcessItem:
    """Verify per-item pipeline execution."""

    def test_convert_and_verify_pipeline(self, tmp_path: Path) -> None:
        """Steps [1, 4]: convert CB7→CBZ then verify integrity."""
        source = _create_test_cb7(tmp_path / "batman.cb7", page_count=3)
        trash_dir = tmp_path / ".trash"

        executor = MassConvertPipelineExecutor()
        result = executor.process_item(
            item_data={
                "id": "item-001",
                "file_path": str(source),
                "operation": "pipeline",
            },
            job_config={
                "steps": [1, 4],
                "trash_folder": str(trash_dir),
            },
        )

        assert result.result == ItemResult.COMPLETED
        output = Path(result.after_state.get("path", ""))
        assert output.exists()
        assert output.suffix == ".cbz"
        # Verify it's a valid ZIP
        with zipfile.ZipFile(output) as zf:
            assert len(zf.namelist()) >= 3

    def test_convert_with_metadata_pipeline(self, tmp_path: Path) -> None:
        """Steps [1, 2]: convert then embed ComicInfo.xml."""
        source = _create_test_cb7(tmp_path / "saga.cb7", page_count=2)
        trash_dir = tmp_path / ".trash"

        executor = MassConvertPipelineExecutor()
        result = executor.process_item(
            item_data={
                "id": "item-002",
                "file_path": str(source),
                "operation": "pipeline",
                "metadata": {
                    "Series": "Saga",
                    "Number": "1",
                    "Writer": "Brian K. Vaughan",
                },
            },
            job_config={
                "steps": [1, 2],
                "metadata_source": "item",
                "trash_folder": str(trash_dir),
            },
        )

        assert result.result == ItemResult.COMPLETED
        output = Path(result.after_state.get("path", ""))
        with zipfile.ZipFile(output) as zf:
            assert "ComicInfo.xml" in zf.namelist()
            content = zf.read("ComicInfo.xml").decode("utf-8")
            root = ET.fromstring(content)
            assert root.findtext("Series") == "Saga"
            assert root.findtext("Writer") == "Brian K. Vaughan"

        debug_entries = [entry for entry in result.log_entries if entry[0] == "DEBUG"]
        assert any("metadata source" in entry[1].lower() for entry in debug_entries)
        assert any("item" in entry[1].lower() for entry in debug_entries)

    def test_log_step_labels_follow_enabled_pipeline_order(self, tmp_path: Path) -> None:
        """User-facing log labels should reflect the enabled steps, not internal ids."""
        source = _create_test_cb7(tmp_path / "detective.cb7", page_count=2)
        trash_dir = tmp_path / ".trash"

        executor = MassConvertPipelineExecutor()
        result = executor.process_item(
            item_data={
                "id": "item-step-order",
                "file_path": str(source),
                "operation": "pipeline",
                "metadata": {
                    "Series": "Detective Comics",
                    "Number": "1050",
                },
                "metadata_source": "item",
            },
            job_config={
                "steps": [1, 2, 4],
                "trash_folder": str(trash_dir),
            },
        )

        assert result.result == ItemResult.COMPLETED
        debug_messages = [entry[1] for entry in result.log_entries if entry[0] == "DEBUG"]
        assert any("Step 1/3" in message for message in debug_messages)
        assert any("Step 2/3" in message for message in debug_messages)
        assert any("Step 3/3" in message for message in debug_messages)
        assert not any("Step 4/4" in message for message in debug_messages)

    def test_convert_only_pipeline(self, tmp_path: Path) -> None:
        """Steps [1]: just convert, no metadata or verify."""
        source = _create_test_cb7(tmp_path / "xmen.cb7", page_count=2)
        trash_dir = tmp_path / ".trash"

        executor = MassConvertPipelineExecutor()
        result = executor.process_item(
            item_data={
                "id": "item-003",
                "file_path": str(source),
                "operation": "pipeline",
            },
            job_config={"steps": [1], "trash_folder": str(trash_dir)},
        )

        assert result.result == ItemResult.COMPLETED
        output = Path(result.after_state.get("path", ""))
        assert output.exists()
        assert output.suffix == ".cbz"

    def test_missing_file_returns_failed(self, tmp_path: Path) -> None:
        executor = MassConvertPipelineExecutor()
        result = executor.process_item(
            item_data={
                "id": "item-004",
                "file_path": str(tmp_path / "ghost.cb7"),
                "operation": "pipeline",
            },
            job_config={"steps": [1]},
        )

        assert result.result == ItemResult.FAILED
        assert result.error_message is not None

    def test_trash_folder_receives_original(self, tmp_path: Path) -> None:
        source = _create_test_cb7(tmp_path / "comics" / "test.cb7")
        trash_dir = tmp_path / ".trash"

        executor = MassConvertPipelineExecutor()
        result = executor.process_item(
            item_data={
                "id": "item-005",
                "file_path": str(source),
                "operation": "pipeline",
            },
            job_config={
                "steps": [1],
                "trash_folder": str(trash_dir),
            },
        )

        assert result.result == ItemResult.COMPLETED
        assert not source.exists()
        assert len(list(trash_dir.rglob("*.cb7"))) == 1

    def test_repack_cbz_moves_original_to_trash(self, tmp_path: Path) -> None:
        source = _create_test_cbz(tmp_path / "batman.cbz", page_count=2)
        trash_dir = tmp_path / ".trash"

        executor = MassConvertPipelineExecutor()
        result = executor.process_item(
            item_data={
                "id": "item-006",
                "file_path": str(source),
                "operation": "pipeline",
            },
            job_config={
                "steps": [1, 4],
                "trash_folder": str(trash_dir),
            },
        )

        assert result.result == ItemResult.COMPLETED
        assert result.after_state.get("original_path") == str(trash_dir / source.name)
        assert source.exists()
        assert (trash_dir / source.name).exists()

    def test_trash_destination_preserves_relative_path(self, tmp_path: Path) -> None:
        source = _create_test_cb7(tmp_path / "library" / "Series" / "Issue 001.cb7")
        trash_dir = tmp_path / ".trash"

        executor = MassConvertPipelineExecutor()
        result = executor.process_item(
            item_data={
                "id": "item-007",
                "file_path": str(source),
                "operation": "pipeline",
                "trash_relative_path": "Series/Issue 001.cb7",
            },
            job_config={
                "steps": [1],
                "trash_folder": str(trash_dir),
            },
        )

        assert result.result == ItemResult.COMPLETED
        assert result.after_state.get("original_path") == str(
            trash_dir / "Series" / "Issue 001.cb7"
        )
        assert (trash_dir / "Series" / "Issue 001.cb7").exists()


# ── Rollback ───────────────────────────────────────────────────


class TestRollback:
    """Verify rollback restores original state."""

    def test_rollback_restores_original(self, tmp_path: Path) -> None:
        # Simulate post-conversion state
        converted = tmp_path / "comics" / "test.cbz"
        converted.parent.mkdir(parents=True)
        converted.write_text("converted")

        trashed = tmp_path / ".trash" / "test.cb7"
        trashed.parent.mkdir(parents=True)
        trashed.write_text("original")

        executor = MassConvertPipelineExecutor()
        result = executor.rollback_item(
            item_data={
                "id": "rb-001",
                "after_state": {
                    "path": str(converted),
                    "original_path": str(trashed),
                },
                "before_state": {
                    "path": str(tmp_path / "comics" / "test.cb7"),
                },
            },
            job_config={},
        )

        assert result.result == ItemResult.COMPLETED
        assert not converted.exists()
        assert (tmp_path / "comics" / "test.cb7").exists()

    def test_rollback_restores_original_for_repacked_cbz(self, tmp_path: Path) -> None:
        source = _create_test_cbz(tmp_path / "batman.cbz", page_count=2)
        trash_dir = tmp_path / ".trash"

        executor = MassConvertPipelineExecutor()
        processed = executor.process_item(
            item_data={
                "id": "rb-cbz-001",
                "file_path": str(source),
                "operation": "pipeline",
            },
            job_config={
                "steps": [1, 4],
                "trash_folder": str(trash_dir),
            },
        )
        assert processed.result == ItemResult.COMPLETED

        result = executor.rollback_item(
            item_data={
                "id": "rb-cbz-001",
                "after_state": processed.after_state,
                "before_state": processed.before_state,
            },
            job_config={},
        )

        assert result.result == ItemResult.COMPLETED
        assert source.exists()
        assert not (trash_dir / source.name).exists()
        info_messages = [entry[1] for entry in result.log_entries if entry[0] == "INFO"]
        assert any("removed converted output" in message.lower() for message in info_messages)
        assert any("restored original from trash" in message.lower() for message in info_messages)

    def test_rollback_fails_when_original_missing_from_trash(self, tmp_path: Path) -> None:
        converted = tmp_path / "comics" / "test.cbz"
        converted.parent.mkdir(parents=True)
        converted.write_text("converted")

        missing_trash = tmp_path / ".trash" / "test.cb7"

        executor = MassConvertPipelineExecutor()
        result = executor.rollback_item(
            item_data={
                "id": "rb-missing-trash",
                "after_state": {
                    "path": str(converted),
                    "original_path": str(missing_trash),
                },
                "before_state": {
                    "path": str(tmp_path / "comics" / "test.cb7"),
                },
            },
            job_config={},
        )

        assert result.result == ItemResult.FAILED
        assert converted.exists()
        assert any("missing from trash" in entry[1].lower() for entry in result.log_entries)


# ── Pipeline Failure Edge Cases ───────────────────────────────


class TestPipelineFailureEdgeCases:
    """Verify failure handling at individual pipeline steps."""

    def test_step1_succeeds_step2_fails_no_trash(self, tmp_path: Path) -> None:
        """Convert succeeds but metadata fails. Original must NOT be in trash."""
        source = _create_test_cb7(tmp_path / "batman.cb7", page_count=2)
        trash_dir = tmp_path / ".trash"

        # Use steps [1, 2] with metadata that will cause embed to fail
        # by making the CBZ read-only after conversion. Since pipeline
        # catches all exceptions, the item returns FAILED.
        executor = MassConvertPipelineExecutor()

        # We'll test that when step 2 fails, the original is NOT moved to trash.
        # Force step 2 to fail by providing metadata but patching embed to raise.
        import unittest.mock

        with unittest.mock.patch(
            "pullbox.utilities.comicinfo.embed_comicinfo_in_cbz",
            side_effect=OSError("Permission denied"),
        ):
            result = executor.process_item(
                item_data={
                    "id": "item-fail-meta",
                    "file_path": str(source),
                    "operation": "pipeline",
                    "metadata": {"Series": "Batman"},
                },
                job_config={
                    "steps": [1, 2],
                    "metadata_source": "item",
                    "trash_folder": str(trash_dir),
                },
            )

        assert result.result == ItemResult.FAILED
        # Original should NOT be in trash (pipeline failed before trash step)
        assert not trash_dir.exists() or len(list(trash_dir.rglob("*.cb7"))) == 0
        assert not (tmp_path / "batman.cbz").exists()

    def test_metadata_step_skipped_when_no_metadata(self, tmp_path: Path) -> None:
        """Step 2 with empty metadata produces WARNING, pipeline continues."""
        source = _create_test_cb7(tmp_path / "saga.cb7", page_count=2)
        trash_dir = tmp_path / ".trash"

        executor = MassConvertPipelineExecutor()
        result = executor.process_item(
            item_data={
                "id": "item-no-meta",
                "file_path": str(source),
                "operation": "pipeline",
                # No metadata key at all
            },
            job_config={
                "steps": [1, 2],
                "metadata_source": "item",
                "trash_folder": str(trash_dir),
            },
        )

        assert result.result == ItemResult.COMPLETED
        # Check that a WARNING log entry about no metadata was emitted
        warning_entries = [e for e in result.log_entries if e[0] == "WARNING"]
        assert len(warning_entries) >= 1
        assert "no metadata" in warning_entries[0][1].lower()

    def test_step4_detects_corruption(self, tmp_path: Path) -> None:
        """Create a CBZ that passes conversion but has a bad zip entry. Step 4 should catch it."""
        source = _create_test_cb7(tmp_path / "corrupt.cb7", page_count=2)

        executor = MassConvertPipelineExecutor()

        # Patch zipfile.ZipFile.testzip to simulate a corrupt entry
        import unittest.mock

        with unittest.mock.patch.object(
            zipfile.ZipFile,
            "testzip",
            return_value="page_000.jpg",  # Indicates this entry is corrupt
        ):
            result = executor.process_item(
                item_data={
                    "id": "item-corrupt",
                    "file_path": str(source),
                    "operation": "pipeline",
                },
                job_config={"steps": [1, 4]},
            )

        assert result.result == ItemResult.FAILED
        assert "corrupt" in (result.error_message or "").lower()


# ── Additional Pipeline Edge Cases ─────────────────────────────


class TestPipelineAdditionalEdgeCases:
    """Additional edge cases for mid-pipeline failures."""

    def test_metadata_step_skipped_when_no_metadata(self, tmp_path: Path) -> None:
        """Step 2 with empty metadata produces WARNING, pipeline continues."""
        source = _create_test_cb7(tmp_path / "no_meta.cb7", page_count=2)
        trash_dir = tmp_path / ".trash"

        executor = MassConvertPipelineExecutor()
        result = executor.process_item(
            item_data={
                "id": "fail-001",
                "file_path": str(source),
                "operation": "pipeline",
                "metadata": {},  # empty metadata
            },
            job_config={
                "steps": [1, 2],
                "metadata_source": "item",
                "trash_folder": str(trash_dir),
            },
        )

        assert result.result == ItemResult.COMPLETED
        # Should have a WARNING about no metadata
        warnings = [e for e in result.log_entries if e[0] == "WARNING"]
        assert len(warnings) >= 1

    def test_convert_only_defaults_to_library_trash(self, tmp_path: Path, monkeypatch) -> None:
        """Blank trash config falls back to the effective {library}/.trash path."""
        source = _create_test_cb7(tmp_path / "keep.cb7", page_count=2)
        fake_library_root = tmp_path / "library-root"
        fake_data_dir = tmp_path / "data"
        fake_library_root.mkdir()
        fake_data_dir.mkdir()

        class _Settings:
            library_root = fake_library_root
            data_dir = fake_data_dir

        monkeypatch.setattr(
            "pullbox.config.get_settings",
            lambda: _Settings(),
        )

        executor = MassConvertPipelineExecutor()
        result = executor.process_item(
            item_data={
                "id": "fail-002",
                "file_path": str(source),
                "operation": "pipeline",
            },
            job_config={"steps": [1]},  # no trash_folder
        )

        assert result.result == ItemResult.COMPLETED
        assert not source.exists()
        assert (fake_library_root / ".trash" / "keep.cb7").exists()

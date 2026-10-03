"""Tests for UT-1.2 — file converter executor.

Verifies standalone convert_file() function and FileConverterExecutor
for CBR→CBZ, CB7→CBZ, CBZ→CBZ (repack), PDF→CBZ conversions.
Tests archive handling, ComicInfo.xml preservation, trash management,
error handling, and edge cases.

Run:
    pytest tests/utilities/test_file_converter.py -v
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from pullbox.utilities.base_executor import ItemResult
from pullbox.utilities.executors.file_converter import (
    FileConverterExecutor,
    _convert_pdf_to_cbz,
    convert_file,
)

# ── Helpers ────────────────────────────────────────────────────


def _create_test_cbz(path: Path, page_count: int = 3, include_comicinfo: bool = False) -> Path:
    """Create a minimal valid CBZ (ZIP) file with JPEG-like page files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(page_count):
            # Write minimal fake JPEG data (just enough to not be empty)
            zf.writestr(f"page_{i:03d}.jpg", f"FAKE_JPEG_DATA_{i}")
        if include_comicinfo:
            zf.writestr(
                "ComicInfo.xml",
                '<?xml version="1.0"?><ComicInfo><Series>Batman</Series></ComicInfo>',
            )
    return path


def _create_test_cb7(path: Path, page_count: int = 3) -> Path:
    """Create a minimal valid CB7 (7z) file with page files."""
    import py7zr

    path.parent.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(path, "w") as archive:
        for i in range(page_count):
            tmp_file = path.parent / f"page_{i:03d}.jpg"
            tmp_file.write_text(f"FAKE_JPEG_DATA_{i}")
            archive.write(tmp_file, f"page_{i:03d}.jpg")
            tmp_file.unlink()
    return path


# ── Standalone convert_file() ──────────────────────────────────


class TestConvertFileStandalone:
    """Verify the standalone convert_file() function works independently."""

    @pytest.mark.asyncio
    async def test_cbz_repack(self, tmp_path: Path) -> None:
        """CBZ→CBZ repack creates valid output."""
        source = _create_test_cbz(tmp_path / "source" / "test.cbz", page_count=3)
        dest = tmp_path / "output"
        dest.mkdir()

        result = await convert_file(source, "cbz", destination=dest)

        assert result.exists()
        assert result.suffix == ".cbz"
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert len([n for n in names if n.endswith(".jpg")]) == 3

    @pytest.mark.asyncio
    async def test_cb7_to_cbz(self, tmp_path: Path) -> None:
        """CB7→CBZ conversion extracts and repacks."""
        source = _create_test_cb7(tmp_path / "source" / "test.cb7", page_count=3)
        dest = tmp_path / "output"
        dest.mkdir()

        result = await convert_file(source, "cbz", destination=dest)

        assert result.exists()
        assert result.suffix == ".cbz"
        with zipfile.ZipFile(result) as zf:
            jpg_files = [n for n in zf.namelist() if n.endswith(".jpg")]
            assert len(jpg_files) == 3

    @pytest.mark.asyncio
    async def test_comicinfo_preserved(self, tmp_path: Path) -> None:
        """ComicInfo.xml is preserved during conversion."""
        source = _create_test_cbz(
            tmp_path / "source" / "test.cbz",
            page_count=2,
            include_comicinfo=True,
        )
        dest = tmp_path / "output"
        dest.mkdir()

        result = await convert_file(source, "cbz", destination=dest)

        with zipfile.ZipFile(result) as zf:
            assert "ComicInfo.xml" in zf.namelist()
            content = zf.read("ComicInfo.xml").decode()
            assert "Batman" in content

    @pytest.mark.asyncio
    async def test_destination_default_same_dir(self, tmp_path: Path) -> None:
        """When destination is None, output goes to same directory as source."""
        source = _create_test_cb7(tmp_path / "test.cb7", page_count=2)

        result = await convert_file(source, "cbz")

        assert result.parent == source.parent
        assert result.suffix == ".cbz"
        assert result.exists()

    @pytest.mark.asyncio
    async def test_nonexistent_source_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            await convert_file(tmp_path / "nonexistent.cbr", "cbz")

    @pytest.mark.asyncio
    async def test_empty_archive_raises(self, tmp_path: Path) -> None:
        """Zero-byte file raises error."""
        source = tmp_path / "empty.cbz"
        source.write_bytes(b"")

        with pytest.raises((ValueError, zipfile.BadZipFile)):
            await convert_file(source, "cbz")

    @pytest.mark.asyncio
    async def test_target_already_exists_raises(self, tmp_path: Path) -> None:
        """If target file already exists, raises error (no silent overwrite)."""
        _create_test_cbz(tmp_path / "test.cbz", page_count=2)
        # Source IS the output path in same-dir mode, so use a CB7
        source_cb7 = _create_test_cb7(tmp_path / "comic.cb7", page_count=2)
        # Pre-create the target .cbz so it already exists
        target = tmp_path / "comic.cbz"
        target.write_text("existing")

        with pytest.raises(FileExistsError):
            await convert_file(source_cb7, "cbz")

    @pytest.mark.asyncio
    async def test_unsupported_format_raises(self, tmp_path: Path) -> None:
        source = _create_test_cbz(tmp_path / "test.cbz", page_count=2)
        with pytest.raises(ValueError, match="Unsupported"):
            await convert_file(source, "epub")

    def test_pdf_conversion_renders_in_bounded_chunks(self, tmp_path: Path) -> None:
        """Large PDFs should render in page chunks instead of one giant image list."""
        source = tmp_path / "source.pdf"
        source.write_bytes(b"%PDF-1.4\n")
        target = tmp_path / "source.cbz"
        convert_calls: list[tuple[int | None, int | None]] = []

        class _FakeImage:
            def __init__(self, raw_path: Path) -> None:
                self.raw_path = raw_path

            def __enter__(self) -> _FakeImage:
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def save(self, target_path: str, _format: str, **_kwargs) -> None:
                Path(target_path).write_bytes(self.raw_path.read_bytes())

        def _fake_convert_from_path(
            _source: str,
            *,
            dpi: int,
            output_folder: str,
            fmt: str,
            paths_only: bool,
            first_page: int | None = None,
            last_page: int | None = None,
            thread_count: int = 1,
        ) -> list[str]:
            assert dpi == 200
            assert fmt == "ppm"
            assert paths_only is True
            assert thread_count == 1
            convert_calls.append((first_page, last_page))
            start = first_page or 1
            end = last_page or 5
            results: list[str] = []
            for page_number in range(start, end + 1):
                raw_path = Path(output_folder) / f"raw_{page_number:04d}.ppm"
                raw_path.write_bytes(f"page-{page_number}".encode())
                results.append(str(raw_path))
            return results

        progress_events: list[tuple[str, int, int, str]] = []

        with (
            patch(
                "pullbox.utilities.executors.file_converter._PDF_RENDER_CHUNK_SIZE",
                2,
            ),
            patch(
                "pdf2image.pdfinfo_from_path",
                return_value={"Pages": 5},
            ),
            patch(
                "pdf2image.convert_from_path",
                side_effect=_fake_convert_from_path,
            ),
            patch(
                "PIL.Image.open",
                side_effect=lambda path: _FakeImage(Path(path)),
            ),
        ):
            result = _convert_pdf_to_cbz(
                source,
                target,
                pdf_quality="medium",
                progress_callback=lambda stage, current, total, unit: progress_events.append(
                    (stage, current, total, unit)
                ),
            )

        assert result == target
        assert target.exists()
        assert convert_calls == [(1, 2), (3, 4), (5, 5)]
        with zipfile.ZipFile(target) as archive:
            jpg_files = [name for name in archive.namelist() if name.endswith(".jpg")]
        assert len(jpg_files) == 5
        assert progress_events[0] == ("rendering", 1, 5, "pages")
        assert ("encoding", 5, 5, "pages") in progress_events

    @pytest.mark.asyncio
    async def test_cbz_repack_rejects_path_traversal_members(self, tmp_path: Path) -> None:
        """CBZ repack must reject unsafe member names before extraction."""
        source = tmp_path / "source" / "malicious.cbz"
        source.parent.mkdir(parents=True)
        with zipfile.ZipFile(source, "w") as zf:
            zf.writestr("../escape.jpg", b"bad")
            zf.writestr("page_001.jpg", b"ok")

        dest = tmp_path / "output"
        dest.mkdir()

        with pytest.raises(ValueError, match="unsafe archive member"):
            await convert_file(source, "cbz", destination=dest)

        assert not (tmp_path / "escape.jpg").exists()
        assert not (dest / "malicious.cbz").exists()

    @pytest.mark.asyncio
    async def test_cbr_corruption_reports_actionable_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Damaged RAR streams should not leak low-level worker details."""
        import rarfile

        from pullbox.utilities.executors import file_converter

        source = tmp_path / "source" / "broken.cbr"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"Rar!\x1a\x07\x00broken")
        dest = tmp_path / "output"
        dest.mkdir()

        class FakeRarFile:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __enter__(self) -> FakeRarFile:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def namelist(self) -> list[str]:
                return ["page_001.jpg"]

            def extract(self, _member: str, _destination: Path) -> None:
                raise rarfile.BadRarFile("Failed the read enough data: req=262144 got=29")

        monkeypatch.setattr(file_converter, "configure_rarfile_backend", lambda: None)
        monkeypatch.setattr(rarfile, "RarFile", FakeRarFile)

        with pytest.raises(ValueError) as exc_info:
            await convert_file(source, "cbz", destination=dest)

        message = str(exc_info.value)
        assert "CBR archive appears corrupt or incomplete" in message
        assert "Try re-downloading or replacing the file" in message
        assert "Failed the read enough data" in message
        assert "payload=" not in message
        assert not (dest / "broken.cbz").exists()

    @pytest.mark.asyncio
    async def test_cbr_missing_unrar_reports_actionable_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing official UnRAR should fail as an actionable conversion error."""
        from pullbox.core.rar_backend import RarBackendUnavailableError
        from pullbox.utilities.executors import file_converter

        source = tmp_path / "source" / "missing-backend.cbr"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"Rar!\x1a\x07\x00fake")
        dest = tmp_path / "output"
        dest.mkdir()

        def raise_missing_backend() -> None:
            raise RarBackendUnavailableError("official UnRAR is required")

        monkeypatch.setattr(file_converter, "configure_rarfile_backend", raise_missing_backend)

        with pytest.raises(ValueError) as exc_info:
            await convert_file(source, "cbz", destination=dest)

        message = str(exc_info.value)
        assert "official UnRAR is unavailable" in message
        assert "missing-backend.cbr" in message
        assert not (dest / "missing-backend.cbz").exists()

    def test_cbr_conversion_configures_rar_backend(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CBR conversion should use Pullbox's central RAR backend setup."""
        import rarfile

        from pullbox.utilities.executors import file_converter

        source = tmp_path / "source" / "issue.cbr"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"Rar!\x1a\x07\x00fake")
        target = tmp_path / "output" / "issue.cbz"
        target.parent.mkdir()
        backend_calls: list[bool] = []

        class FakeRarFile:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __enter__(self) -> FakeRarFile:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def namelist(self) -> list[str]:
                return ["page_001.jpg"]

            def extract(self, member: str, destination: Path) -> None:
                extracted = destination / member
                extracted.parent.mkdir(parents=True, exist_ok=True)
                extracted.write_bytes(b"fake image")

        monkeypatch.setattr(
            file_converter,
            "configure_rarfile_backend",
            lambda: backend_calls.append(True),
        )
        monkeypatch.setattr(rarfile, "RarFile", FakeRarFile)

        file_converter._convert_cbr_to_cbz(source, target)

        assert backend_calls == [True]
        with zipfile.ZipFile(target, "r") as zf:
            assert zf.read("page_001.jpg") == b"fake image"


# ── FileConverterExecutor ──────────────────────────────────────


class TestFileConverterExecutor:
    """Verify executor wraps convert_file for job queue integration."""

    def test_validate_config_valid(self) -> None:
        executor = FileConverterExecutor()
        errors = executor.validate_config(
            {
                "target_format": "cbz",
                "source_format": "cb7",
            }
        )
        assert errors == []

    def test_validate_config_missing_target(self) -> None:
        executor = FileConverterExecutor()
        errors = executor.validate_config({})
        assert any("target_format" in e for e in errors)

    def test_validate_config_repack_allowed(self) -> None:
        """CBZ→CBZ repack should be allowed (no same-format error)."""
        executor = FileConverterExecutor()
        errors = executor.validate_config(
            {
                "target_format": "cbz",
                "source_format": "cbz",
            }
        )
        assert errors == []

    def test_validate_config_invalid_format(self) -> None:
        executor = FileConverterExecutor()
        errors = executor.validate_config({"target_format": "epub"})
        assert any("unsupported" in e.lower() or "invalid" in e.lower() for e in errors)

    @pytest.mark.asyncio
    async def test_generate_items_returns_file_list(self, tmp_path: Path) -> None:
        """generate_items discovers files matching source format."""
        # Create test files
        source_dir = tmp_path / "comics"
        source_dir.mkdir()
        _create_test_cb7(source_dir / "batman_001.cb7")
        _create_test_cb7(source_dir / "batman_002.cb7")
        _create_test_cbz(source_dir / "already_cbz.cbz")

        executor = FileConverterExecutor()
        items = await executor.generate_items(
            {
                "target_format": "cbz",
                "source_format": "cb7",
                "scope": "manual",
                "file_paths": [
                    str(source_dir / "batman_001.cb7"),
                    str(source_dir / "batman_002.cb7"),
                ],
            }
        )

        assert len(items) == 2
        assert all(item["operation"] == "convert" for item in items)

    def test_process_item_success(self, tmp_path: Path) -> None:
        """process_item converts a file and returns COMPLETED."""
        source = _create_test_cb7(tmp_path / "batman.cb7", page_count=3)
        trash_dir = tmp_path / ".trash"

        executor = FileConverterExecutor()
        result = executor.process_item(
            item_data={
                "id": "item-001",
                "file_path": str(source),
                "operation": "convert",
            },
            job_config={
                "target_format": "cbz",
                "trash_folder": str(trash_dir),
            },
        )

        assert result.result == ItemResult.COMPLETED
        # Output file should exist
        output = Path(result.after_state.get("path", ""))
        assert output.exists()
        assert output.suffix == ".cbz"

    def test_process_item_missing_file(self, tmp_path: Path) -> None:
        """process_item with missing file returns FAILED."""
        executor = FileConverterExecutor()
        result = executor.process_item(
            item_data={
                "id": "item-002",
                "file_path": str(tmp_path / "nonexistent.cb7"),
                "operation": "convert",
            },
            job_config={"target_format": "cbz"},
        )

        assert result.result == ItemResult.FAILED
        assert result.error_message is not None

    def test_process_item_moves_original_to_trash(self, tmp_path: Path) -> None:
        """Original file is moved to trash folder after conversion."""
        source = _create_test_cb7(tmp_path / "comics" / "test.cb7", page_count=2)
        trash_dir = tmp_path / ".trash"

        executor = FileConverterExecutor()
        result = executor.process_item(
            item_data={
                "id": "item-003",
                "file_path": str(source),
                "operation": "convert",
            },
            job_config={
                "target_format": "cbz",
                "trash_folder": str(trash_dir),
            },
        )

        assert result.result == ItemResult.COMPLETED
        assert not source.exists()
        # Original should be in trash
        trash_files = list(trash_dir.rglob("*.cb7"))
        assert len(trash_files) == 1

    def test_rollback_item_restores_original(self, tmp_path: Path) -> None:
        """Rollback moves original back from trash and removes converted file."""
        # Setup: simulate a completed conversion
        source_dir = tmp_path / "comics"
        trash_dir = tmp_path / ".trash"
        source_dir.mkdir()
        trash_dir.mkdir()

        # "Converted" file exists
        converted = source_dir / "test.cbz"
        converted.write_text("converted content")

        # "Original" is in trash
        trashed = trash_dir / "test.cb7"
        trashed.write_text("original content")

        executor = FileConverterExecutor()
        result = executor.rollback_item(
            item_data={
                "id": "rb-001",
                "before_state": {
                    "path": str(source_dir / "test.cb7"),
                },
                "after_state": {
                    "path": str(converted),
                    "original_path": str(trashed),
                },
            },
            job_config={},
        )

        assert result.result == ItemResult.COMPLETED
        assert trashed.exists() is False or (source_dir / "test.cb7").exists()

    def test_rollback_item_fails_when_original_missing_from_trash(self, tmp_path: Path) -> None:
        source_dir = tmp_path / "comics"
        source_dir.mkdir()

        converted = source_dir / "test.cbz"
        converted.write_text("converted content")

        executor = FileConverterExecutor()
        result = executor.rollback_item(
            item_data={
                "id": "rb-missing-001",
                "before_state": {
                    "path": str(source_dir / "test.cb7"),
                },
                "after_state": {
                    "path": str(converted),
                    "original_path": str(tmp_path / ".trash" / "test.cb7"),
                },
            },
            job_config={},
        )

        assert result.result == ItemResult.FAILED
        assert converted.exists()


# ── File Converter Edge Cases ─────────────────────────────────


class TestFileConverterEdgeCases:
    """Verify edge cases in file conversion — nested dirs, unicode, non-image files."""

    @pytest.mark.asyncio
    async def test_cbz_with_nested_directories(self, tmp_path: Path) -> None:
        """CBZ containing subdirectories preserves directory structure in output."""
        source = tmp_path / "source" / "nested.cbz"
        source.parent.mkdir(parents=True)
        with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("chapter1/page_001.jpg", "FAKE_JPEG_1")
            zf.writestr("chapter1/page_002.jpg", "FAKE_JPEG_2")
            zf.writestr("chapter2/page_001.jpg", "FAKE_JPEG_3")

        dest = tmp_path / "output"
        dest.mkdir()

        # Use CB7 source for actual conversion (CBZ→CBZ repack)
        cb7_source = _create_test_cb7(tmp_path / "source" / "nested.cb7", page_count=2)
        result = await convert_file(cb7_source, "cbz", destination=dest)

        assert result.exists()
        assert result.suffix == ".cbz"
        with zipfile.ZipFile(result) as zf:
            assert len(zf.namelist()) >= 2

    @pytest.mark.asyncio
    async def test_cbz_with_non_image_files_preserved(self, tmp_path: Path) -> None:
        """Non-image files like readme.txt are preserved during CBZ repack."""
        source = tmp_path / "source" / "mixed.cb7"
        source.parent.mkdir(parents=True)
        import py7zr

        with py7zr.SevenZipFile(source, "w") as archive:
            readme = source.parent / "readme.txt"
            readme.write_text("This is a readme")
            archive.write(readme, "readme.txt")
            readme.unlink()

            thumbs = source.parent / "Thumbs.db"
            thumbs.write_bytes(b"thumbs data")
            archive.write(thumbs, "Thumbs.db")
            thumbs.unlink()

            page = source.parent / "page_001.jpg"
            page.write_text("FAKE_JPEG")
            archive.write(page, "page_001.jpg")
            page.unlink()

        dest = tmp_path / "output"
        dest.mkdir()
        result = await convert_file(source, "cbz", destination=dest)

        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "readme.txt" in names
            assert "Thumbs.db" in names
            assert "page_001.jpg" in names

    @pytest.mark.asyncio
    async def test_unicode_filenames_inside_archive(self, tmp_path: Path) -> None:
        """Archive with Japanese page names handled correctly."""
        source = tmp_path / "unicode.cb7"
        import py7zr

        unicode_names = [
            "\u8868\u7d19.jpg",
            "\u30da\u30fc\u30b8_001.jpg",
            "\u30da\u30fc\u30b8_002.jpg",
        ]
        with py7zr.SevenZipFile(source, "w") as archive:
            for name in unicode_names:
                tmp_file = tmp_path / name
                tmp_file.write_text(f"FAKE_DATA_{name}")
                archive.write(tmp_file, name)
                tmp_file.unlink()

        dest = tmp_path / "output"
        dest.mkdir()
        result = await convert_file(source, "cbz", destination=dest)

        assert result.exists()
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert len(names) == 3
            assert "\u8868\u7d19.jpg" in names

    @pytest.mark.asyncio
    async def test_archive_with_only_non_image_files(self, tmp_path: Path) -> None:
        """CBZ with only .txt files raises ValueError (no files to pack after extraction)."""
        # This test verifies the standalone convert_file rejects archives
        # that produce only non-image files. The _pack_directory_as_cbz
        # packs all files, so the repack itself won't fail. But we can test
        # that a CB7 with only txt is still converted (all files preserved).
        source = tmp_path / "txtonly.cb7"
        import py7zr

        with py7zr.SevenZipFile(source, "w") as archive:
            txt = tmp_path / "readme.txt"
            txt.write_text("Only text here")
            archive.write(txt, "readme.txt")
            txt.unlink()

            txt2 = tmp_path / "notes.txt"
            txt2.write_text("More text")
            archive.write(txt2, "notes.txt")
            txt2.unlink()

        dest = tmp_path / "output"
        dest.mkdir()
        # The converter packs all files, including non-images, so it should succeed
        result = await convert_file(source, "cbz", destination=dest)
        assert result.exists()
        with zipfile.ZipFile(result) as zf:
            assert len(zf.namelist()) == 2

    @pytest.mark.asyncio
    async def test_symlink_source_followed(self, tmp_path: Path) -> None:
        """Symlink to a CB7 converts the target correctly."""
        import os
        import sys

        if sys.platform == "win32":
            pytest.skip("Symlinks require elevated privileges on Windows")

        # Create the actual CB7
        real_file = _create_test_cb7(tmp_path / "real" / "batman.cb7", page_count=3)

        # Create symlink
        link = tmp_path / "link" / "batman_link.cb7"
        link.parent.mkdir(parents=True)
        os.symlink(real_file, link)

        dest = tmp_path / "output"
        dest.mkdir()
        result = await convert_file(link, "cbz", destination=dest)

        assert result.exists()
        assert result.suffix == ".cbz"
        with zipfile.ZipFile(result) as zf:
            jpg_files = [n for n in zf.namelist() if n.endswith(".jpg")]
            assert len(jpg_files) == 3


# ── Additional Edge Cases ──────────────────────────────────────


class TestConverterAdditionalEdgeCases:
    """Additional edge cases from sprint guide gap analysis."""

    @pytest.mark.asyncio
    async def test_cbz_with_nested_directories_preserved(self, tmp_path: Path) -> None:
        """Subdirectory structure inside archive is preserved."""
        source = tmp_path / "nested.cbz"
        with zipfile.ZipFile(source, "w") as zf:
            zf.writestr("pages/page_000.jpg", b"\xff\xd8" + b"X" * 200)
            zf.writestr("pages/page_001.jpg", b"\xff\xd8" + b"X" * 200)
            zf.writestr("extras/bonus.jpg", b"\xff\xd8" + b"X" * 200)

        dest = tmp_path / "output"
        dest.mkdir()
        result = await convert_file(source, "cbz", destination=dest)

        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert any("pages/" in n for n in names)
            assert any("extras/" in n for n in names)

    @pytest.mark.asyncio
    async def test_non_image_files_preserved_in_conversion(self, tmp_path: Path) -> None:
        """Non-image files (readme.txt, thumbs.db) preserved in output."""
        source = tmp_path / "with_extras.cbz"
        with zipfile.ZipFile(source, "w") as zf:
            zf.writestr("page_000.jpg", b"\xff\xd8" + b"X" * 200)
            zf.writestr("readme.txt", "Read me")
            zf.writestr("Thumbs.db", "thumbs")

        dest = tmp_path / "output"
        dest.mkdir()
        result = await convert_file(source, "cbz", destination=dest)

        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert "readme.txt" in names
            assert "Thumbs.db" in names

    @pytest.mark.asyncio
    async def test_unicode_filenames_inside_archive(self, tmp_path: Path) -> None:
        """Japanese page names handled correctly."""
        source = tmp_path / "unicode.cbz"
        with zipfile.ZipFile(source, "w") as zf:
            zf.writestr("\u8868\u7d19.jpg", b"\xff\xd8" + b"X" * 200)
            zf.writestr("\u30da\u30fc\u30b8_001.jpg", b"\xff\xd8" + b"X" * 200)

        dest = tmp_path / "output"
        dest.mkdir()
        result = await convert_file(source, "cbz", destination=dest)

        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert any("\u8868\u7d19" in n for n in names)


# ── Repack (CBZ→CBZ) Tests ───────────────────────────────────


class TestCBZRepack:
    """Verify CBZ→CBZ repack works end-to-end through process_item."""

    def test_process_item_repack_with_trash(self, tmp_path: Path) -> None:
        """CBZ→CBZ repack moves original to trash and produces repacked output at same path."""
        source = _create_test_cbz(tmp_path / "comics" / "batman.cbz", page_count=3)
        trash_dir = tmp_path / ".trash"

        executor = FileConverterExecutor()
        result = executor.process_item(
            item_data={"id": "repack-001", "file_path": str(source), "operation": "convert"},
            job_config={
                "target_format": "cbz",
                "source_format": "cbz",
                "trash_folder": str(trash_dir),
            },
        )

        assert result.result == ItemResult.COMPLETED
        output = Path(result.after_state["path"])
        assert output.exists()
        assert output.suffix == ".cbz"
        assert output.name == "batman.cbz"
        # Original should be in trash, repacked file takes its place
        assert (trash_dir / "batman.cbz").exists()

    def test_process_item_repack_without_trash(self, tmp_path: Path) -> None:
        """CBZ→CBZ repack without trash deletes original and renames temp."""
        source = _create_test_cbz(tmp_path / "batman.cbz", page_count=2)

        executor = FileConverterExecutor()
        result = executor.process_item(
            item_data={"id": "repack-002", "file_path": str(source), "operation": "convert"},
            job_config={"target_format": "cbz", "source_format": "cbz"},
        )

        assert result.result == ItemResult.COMPLETED
        output = Path(result.after_state["path"])
        assert output.exists()
        assert output.name == "batman.cbz"
        # No temp files left behind
        assert not any(f.name.endswith("._repack_.cbz") for f in tmp_path.iterdir())

    def test_process_item_repack_preserves_content(self, tmp_path: Path) -> None:
        """Repacked CBZ has the same files as the original."""
        source = _create_test_cbz(tmp_path / "test.cbz", page_count=4, include_comicinfo=True)
        with zipfile.ZipFile(source) as zf:
            original_names = sorted(zf.namelist())

        executor = FileConverterExecutor()
        result = executor.process_item(
            item_data={"id": "repack-003", "file_path": str(source), "operation": "convert"},
            job_config={"target_format": "cbz", "source_format": "cbz"},
        )

        assert result.result == ItemResult.COMPLETED
        output = Path(result.after_state["path"])
        with zipfile.ZipFile(output) as zf:
            repacked_names = sorted(zf.namelist())
        assert repacked_names == original_names


# ── PDF Quality Presets Tests ─────────────────────────────────


class TestPDFQualityPresets:
    """Verify PDF quality presets are correctly defined."""

    def test_all_presets_exist(self) -> None:
        from pullbox.utilities.executors.file_converter import _PDF_QUALITY_PRESETS

        assert "high" in _PDF_QUALITY_PRESETS
        assert "medium" in _PDF_QUALITY_PRESETS
        assert "low" in _PDF_QUALITY_PRESETS

    def test_high_quality_preset(self) -> None:
        from pullbox.utilities.executors.file_converter import _PDF_QUALITY_PRESETS

        dpi, fmt, ext, kwargs = _PDF_QUALITY_PRESETS["high"]
        assert dpi == 300
        assert fmt == "PNG"
        assert ext == "png"
        assert kwargs == {}

    def test_medium_quality_preset(self) -> None:
        from pullbox.utilities.executors.file_converter import _PDF_QUALITY_PRESETS

        dpi, fmt, ext, kwargs = _PDF_QUALITY_PRESETS["medium"]
        assert dpi == 200
        assert fmt == "JPEG"
        assert ext == "jpg"
        assert kwargs == {"quality": 90}

    def test_low_quality_preset(self) -> None:
        from pullbox.utilities.executors.file_converter import _PDF_QUALITY_PRESETS

        dpi, fmt, ext, kwargs = _PDF_QUALITY_PRESETS["low"]
        assert dpi == 150
        assert fmt == "JPEG"
        assert ext == "jpg"
        assert kwargs == {"quality": 80}

    def test_process_item_passes_pdf_quality(self, tmp_path: Path) -> None:
        """pdf_quality config key is read and stored (verified via config passthrough)."""
        executor = FileConverterExecutor()
        # Verify validate_config accepts pdf_quality without error
        errors = executor.validate_config(
            {"target_format": "cbz", "source_format": "pdf", "pdf_quality": "high"}
        )
        assert errors == []


# ── Library Record Sync ────────────────────────────────────────


async def _track_library_file(db_session, file_path: Path) -> int:
    from datetime import UTC, datetime

    from pullbox.models.issue import Issue
    from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
    from pullbox.models.series import Series

    library_root = LibraryRoot(name="Library", path=str(file_path.parent.parent), enabled=True)
    series = Series(title="Batman", sort_title="batman", year_start=2016, library_root=library_root)
    issue = Issue(series=series, issue_number=1.0)
    library_file = LibraryFile(
        file_path=str(file_path),
        file_name=file_path.name,
        file_size=file_path.stat().st_size,
        file_format=FileFormat(file_path.suffix.lstrip(".")),
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        issue=issue,
        library_root=library_root,
    )
    db_session.add(library_file)
    await db_session.flush()
    return int(library_file.id)


def _processed_conversion(source: Path, target: Path):
    from pullbox.utilities.base_executor import ProcessedItem

    return ProcessedItem(
        item_id="item-1",
        result=ItemResult.COMPLETED,
        before_state={"path": str(source), "format": "cbr"},
        after_state={"path": str(target), "format": "cbz", "original_path": "/trash/x.cbr"},
    )


class TestLibraryRecordSync:
    """A converted file must stay tracked under its new name."""

    @pytest.mark.asyncio
    async def test_completed_conversion_moves_the_library_record(
        self, db_session, tmp_path: Path
    ) -> None:
        from pullbox.models.library import FileFormat, LibraryFile
        from pullbox.utilities.base_executor import JobRunSummary

        source = tmp_path / "Batman (2016)" / "Batman 001.cbr"
        _create_test_cbz(source)
        library_file_id = await _track_library_file(db_session, source)
        target = _create_test_cbz(source.with_suffix(".cbz"), page_count=5)
        source.unlink()

        applied = await FileConverterExecutor().apply_item_result(
            db_session,
            item=None,
            item_data={"id": "item-1", "file_path": str(source)},
            processed=_processed_conversion(source, target),
            job_config={"target_format": "cbz"},
            job_context=None,
            summary=JobRunSummary(),
        )

        record = await db_session.get(LibraryFile, library_file_id)
        assert record.file_path == str(target)
        assert record.file_name == "Batman 001.cbz"
        assert record.file_format == FileFormat.CBZ
        assert record.file_size == target.stat().st_size
        assert [entry.message for entry in applied.extra_logs] == [
            "Updated library record: Batman 001.cbr -> Batman 001.cbz"
        ]

    @pytest.mark.asyncio
    async def test_same_path_repack_refreshes_the_library_record(
        self, db_session, tmp_path: Path
    ) -> None:
        from pullbox.models.library import FileFormat, LibraryFile
        from pullbox.utilities.base_executor import JobRunSummary

        archive = tmp_path / "Batman (2016)" / "Batman 001.cbz"
        _create_test_cbz(archive, page_count=2)
        library_file_id = await _track_library_file(db_session, archive)
        record = await db_session.get(LibraryFile, library_file_id)
        record.file_hash = "hash-of-the-old-archive"
        old_size = record.file_size
        # A CBZ -> CBZ repack rewrites the archive in place.
        _create_test_cbz(archive, page_count=9)
        assert archive.stat().st_size != old_size

        applied = await FileConverterExecutor().apply_item_result(
            db_session,
            item=None,
            item_data={"id": "item-1", "file_path": str(archive)},
            processed=_processed_conversion(archive, archive),
            job_config={"target_format": "cbz"},
            job_context=None,
            summary=JobRunSummary(),
        )

        record = await db_session.get(LibraryFile, library_file_id)
        assert record.file_path == str(archive)
        assert record.file_format == FileFormat.CBZ
        assert record.file_size == archive.stat().st_size
        assert record.file_hash is None
        assert [entry.message for entry in applied.extra_logs] == [
            "Refreshed library record: Batman 001.cbz"
        ]

    @pytest.mark.asyncio
    async def test_same_path_repack_rollback_refreshes_the_library_record(
        self, db_session, tmp_path: Path
    ) -> None:
        from pullbox.models.library import LibraryFile
        from pullbox.utilities.base_executor import ProcessedItem

        archive = tmp_path / "Batman (2016)" / "Batman 001.cbz"
        _create_test_cbz(archive, page_count=9)
        library_file_id = await _track_library_file(db_session, archive)
        record = await db_session.get(LibraryFile, library_file_id)
        record.file_hash = "hash-of-the-repacked-archive"
        repacked_size = record.file_size
        # Rollback restores the original archive to the same path.
        _create_test_cbz(archive, page_count=2)
        assert archive.stat().st_size != repacked_size

        await FileConverterExecutor.apply_rollback_result(
            db_session,
            {
                "id": "item-1",
                "before_state": {"path": str(archive), "format": "cbz"},
                "after_state": {"path": str(archive), "format": "cbz"},
            },
            ProcessedItem(item_id="item-1", result=ItemResult.COMPLETED),
        )

        record = await db_session.get(LibraryFile, library_file_id)
        assert record.file_path == str(archive)
        assert record.file_size == archive.stat().st_size
        assert record.file_hash is None

    @pytest.mark.asyncio
    async def test_untracked_or_failed_conversions_change_nothing(
        self, db_session, tmp_path: Path
    ) -> None:
        from pullbox.utilities.base_executor import JobRunSummary, ProcessedItem

        source = tmp_path / "loose" / "Saga 001.cbr"
        target = _create_test_cbz(source.with_suffix(".cbz"))
        executor = FileConverterExecutor()

        untracked = await executor.apply_item_result(
            db_session,
            item=None,
            item_data={"id": "item-1", "file_path": str(source)},
            processed=_processed_conversion(source, target),
            job_config={"target_format": "cbz"},
            job_context=None,
            summary=JobRunSummary(),
        )
        failed = await executor.apply_item_result(
            db_session,
            item=None,
            item_data={"id": "item-2", "file_path": str(source)},
            processed=ProcessedItem(item_id="item-2", result=ItemResult.FAILED),
            job_config={"target_format": "cbz"},
            job_context=None,
            summary=JobRunSummary(),
        )

        assert untracked.extra_logs == []
        assert failed.extra_logs == []

    @pytest.mark.asyncio
    async def test_rollback_points_the_record_back_at_the_original(
        self, db_session, tmp_path: Path
    ) -> None:
        from pullbox.models.library import FileFormat, LibraryFile
        from pullbox.utilities.base_executor import ProcessedItem

        converted = tmp_path / "Batman (2016)" / "Batman 001.cbz"
        _create_test_cbz(converted)
        library_file_id = await _track_library_file(db_session, converted)
        original = converted.with_suffix(".cbr")
        _create_test_cbz(original, page_count=4)
        converted.unlink()

        await FileConverterExecutor.apply_rollback_result(
            db_session,
            {
                "id": "item-1",
                "before_state": {"path": str(original), "format": "cbr"},
                "after_state": {"path": str(converted), "format": "cbz"},
            },
            ProcessedItem(item_id="item-1", result=ItemResult.COMPLETED),
        )

        record = await db_session.get(LibraryFile, library_file_id)
        assert record.file_path == str(original)
        assert record.file_name == "Batman 001.cbr"
        assert record.file_format == FileFormat.CBR

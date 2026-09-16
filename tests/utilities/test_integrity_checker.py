"""Tests for UT-6.1 — file integrity checker executor.

Verifies standalone check_file_integrity() function and
IntegrityCheckerExecutor for quick and deep scans on CBZ/CB7 archives.

Run:
    pytest tests/utilities/test_integrity_checker.py -v
"""

from __future__ import annotations

import io
import sys
import tarfile
import types
import zipfile
from pathlib import Path
from typing import ClassVar

import py7zr
import pytest

from pullbox.utilities.base_executor import ItemResult
from pullbox.utilities.executors import integrity_checks
from pullbox.utilities.executors.integrity_checker import (
    IntegrityCheckerExecutor,
    IntegrityResult,
    check_file_integrity,
)

# ── Helpers ────────────────────────────────────────────────────


def _valid_jpeg_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 255, 255)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _create_valid_cbz(path: Path, page_count: int = 5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _valid_jpeg_bytes()
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(page_count):
            zf.writestr(f"page_{i:03d}.jpg", payload)
    return path


def _create_valid_cb7(path: Path, page_count: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _valid_jpeg_bytes()
    with py7zr.SevenZipFile(path, "w") as archive:
        for i in range(page_count):
            tmp = path.parent / f"_tmp_{i}.jpg"
            tmp.write_bytes(payload)
            archive.write(tmp, f"page_{i:03d}.jpg")
            tmp.unlink()
    return path


def _create_cbz_no_images(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("readme.txt", "No images here")
        zf.writestr("thumbs.db", "fake")
    return path


def _create_valid_cbt(path: Path, page_count: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _valid_jpeg_bytes()
    with tarfile.open(path, "w") as archive:
        for i in range(page_count):
            info = tarfile.TarInfo(name=f"page_{i:03d}.jpg")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def _create_traversal_cbt(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _valid_jpeg_bytes()
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo(name="../escaped.jpg")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return path


# ── IntegrityResult ────────────────────────────────────────────


class TestIntegrityResult:
    """Verify IntegrityResult dataclass."""

    def test_healthy_result(self) -> None:
        result = IntegrityResult(status="healthy", page_count=32)
        assert result.status == "healthy"
        assert result.page_count == 32
        assert result.warnings == []
        assert result.errors == []

    def test_failed_result(self) -> None:
        result = IntegrityResult(
            status="corrupt",
            page_count=0,
            errors=["Corrupt archive header"],
        )
        assert result.status == "corrupt"
        assert len(result.errors) == 1


# ── Quick Scan (standalone function) ───────────────────────────


class TestQuickScan:
    """Verify check_file_integrity() quick scan mode."""

    @pytest.mark.asyncio
    async def test_valid_cbz_passes(self, tmp_path: Path) -> None:
        cbz = _create_valid_cbz(tmp_path / "good.cbz")
        result = await check_file_integrity(cbz, deep=False)
        assert result.status == "healthy"
        assert result.page_count == 5

    @pytest.mark.asyncio
    async def test_valid_cb7_passes(self, tmp_path: Path) -> None:
        cb7 = _create_valid_cb7(tmp_path / "good.cb7")
        result = await check_file_integrity(cb7, deep=False)
        assert result.status == "healthy"
        assert result.page_count == 3

    @pytest.mark.asyncio
    async def test_valid_cbr_configures_rar_backend(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import rarfile

        from pullbox.utilities.executors import integrity_checks

        cbr = tmp_path / "good.cbr"
        cbr.write_bytes(b"Rar!\x1a\x07\x00fake")
        backend_calls: list[bool] = []

        class FakeRarFile:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __enter__(self) -> FakeRarFile:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def namelist(self) -> list[str]:
                return ["page_001.jpg", "page_002.jpg"]

        monkeypatch.setattr(
            integrity_checks,
            "configure_rarfile_backend",
            lambda: backend_calls.append(True),
        )
        monkeypatch.setattr(rarfile, "RarFile", FakeRarFile)

        result = await check_file_integrity(cbr, deep=False)

        assert result.status == "healthy"
        assert result.page_count == 2
        assert backend_calls == [True]

    @pytest.mark.asyncio
    async def test_valid_cbt_passes(self, tmp_path: Path) -> None:
        cbt = _create_valid_cbt(tmp_path / "good.cbt")
        result = await check_file_integrity(cbt, deep=False)
        assert result.status == "healthy"
        assert result.page_count == 3

    @pytest.mark.asyncio
    async def test_zero_byte_file_fails(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.cbz"
        empty.write_bytes(b"")
        result = await check_file_integrity(empty, deep=False)
        assert result.status == "corrupt"
        assert any("empty" in e.lower() for e in result.errors)

    @pytest.mark.asyncio
    async def test_corrupt_archive_fails(self, tmp_path: Path) -> None:
        corrupt = tmp_path / "corrupt.cbz"
        corrupt.write_bytes(b"NOT_A_ZIP_FILE_AT_ALL")
        result = await check_file_integrity(corrupt, deep=False)
        assert result.status == "corrupt"

    @pytest.mark.asyncio
    async def test_nonexistent_file_fails(self, tmp_path: Path) -> None:
        result = await check_file_integrity(tmp_path / "ghost.cbz", deep=False)
        assert result.status == "corrupt"
        assert any("not found" in e.lower() for e in result.errors)

    @pytest.mark.asyncio
    async def test_no_images_fails(self, tmp_path: Path) -> None:
        cbz = _create_cbz_no_images(tmp_path / "no_images.cbz")
        result = await check_file_integrity(cbz, deep=False)
        assert result.status == "corrupt"
        assert any("no images" in e.lower() or "no valid" in e.lower() for e in result.errors)

    @pytest.mark.asyncio
    async def test_single_image_passes(self, tmp_path: Path) -> None:
        cbz = _create_valid_cbz(tmp_path / "single.cbz", page_count=1)
        result = await check_file_integrity(cbz, deep=False)
        assert result.status == "healthy"
        assert result.page_count == 1


# ── Deep Scan ──────────────────────────────────────────────────


class TestDeepScan:
    """Verify check_file_integrity() deep scan mode."""

    @pytest.mark.asyncio
    async def test_deep_valid_cbz_passes(self, tmp_path: Path) -> None:
        """Deep scan with synthetic images may warn but still counts pages."""
        cbz = _create_valid_cbz(tmp_path / "good.cbz", page_count=3)
        result = await check_file_integrity(cbz, deep=True)
        # Synthetic test images may produce warnings from Pillow verify
        assert result.status in ("healthy", "warning")
        assert result.page_count == 3

    @pytest.mark.asyncio
    async def test_deep_truncated_image_fails(self, tmp_path: Path) -> None:
        """Image that can't be decoded should fail deep integrity scans."""
        path = tmp_path / "bad_image.cbz"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("page_000.jpg", b"\xff\xd8\xff")  # truncated JPEG
            zf.writestr("page_001.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 500)
        result = await check_file_integrity(path, deep=True)
        assert result.status == "corrupt"
        assert any("verification failed" in error.lower() for error in result.errors)

    @pytest.mark.asyncio
    async def test_deep_cbt_rejects_path_traversal_members(self, tmp_path: Path) -> None:
        cbt = _create_traversal_cbt(tmp_path / "unsafe.cbt")
        escaped = tmp_path / "escaped.jpg"

        result = await check_file_integrity(cbt, deep=True)

        assert result.status == "corrupt"
        assert any("unsafe archive member" in error.lower() for error in result.errors)
        assert not escaped.exists()


# ── Config Validation ──────────────────────────────────────────


class TestValidateConfig:
    """Verify config validation."""

    def test_valid_config(self) -> None:
        executor = IntegrityCheckerExecutor()
        errors = executor.validate_config({"scan_depth": "quick"})
        assert errors == []

    def test_invalid_scan_depth(self) -> None:
        executor = IntegrityCheckerExecutor()
        errors = executor.validate_config({"scan_depth": "turbo"})
        assert any("scan_depth" in e.lower() for e in errors)


class TestGenerateItems:
    """Verify folder and manual scope item discovery."""

    @pytest.mark.asyncio
    async def test_folder_scope_recurses_into_nested_subfolders(self, tmp_path: Path) -> None:
        root_file = _create_valid_cbz(tmp_path / "root.cbz")
        nested_file = _create_valid_cbz(tmp_path / "series" / "nested.cbz")
        deep_file = _create_valid_cbz(tmp_path / "series" / "annuals" / "deep.cbz")
        ignored = tmp_path / "ignored" / "note.txt"
        ignored.parent.mkdir(parents=True, exist_ok=True)
        ignored.write_text("not a comic archive")

        executor = IntegrityCheckerExecutor()
        items = await executor.generate_items(
            {
                "scope": "folder",
                "scan_folder": str(tmp_path),
            }
        )

        assert [item["file_path"] for item in items] == [
            str(root_file),
            str(nested_file),
            str(deep_file),
        ]

    @pytest.mark.asyncio
    async def test_folder_scope_accepts_multiple_scan_folders(self, tmp_path: Path) -> None:
        folder_one = tmp_path / "Batman (2016)"
        folder_two = tmp_path / "Saga (2012)"
        file_one = _create_valid_cbz(folder_one / "batman-001.cbz")
        file_two = _create_valid_cbz(folder_two / "saga-001.cbz")

        executor = IntegrityCheckerExecutor()
        items = await executor.generate_items(
            {
                "scope": "folder",
                "scan_folders": [str(folder_one), str(folder_two)],
            }
        )

        assert [item["file_path"] for item in items] == [
            str(file_one),
            str(file_two),
        ]


# ── Process Item ───────────────────────────────────────────────


class TestProcessItem:
    """Verify executor process_item wraps standalone function."""

    def test_healthy_file(self, tmp_path: Path) -> None:
        cbz = _create_valid_cbz(tmp_path / "healthy.cbz")
        executor = IntegrityCheckerExecutor()
        result = executor.process_item(
            item_data={"id": "item-001", "file_path": str(cbz), "operation": "check"},
            job_config={"scan_depth": "quick"},
        )
        assert result.result == ItemResult.COMPLETED
        assert result.after_state.get("status") == "healthy"

    def test_corrupt_file(self, tmp_path: Path) -> None:
        corrupt = tmp_path / "corrupt.cbz"
        corrupt.write_bytes(b"GARBAGE")
        executor = IntegrityCheckerExecutor()
        result = executor.process_item(
            item_data={"id": "item-002", "file_path": str(corrupt), "operation": "check"},
            job_config={"scan_depth": "quick"},
        )
        assert result.result == ItemResult.FAILED
        assert result.after_state.get("status") == "corrupt"

    def test_missing_file(self, tmp_path: Path) -> None:
        executor = IntegrityCheckerExecutor()
        result = executor.process_item(
            item_data={
                "id": "item-003",
                "file_path": str(tmp_path / "nope.cbz"),
                "operation": "check",
            },
            job_config={"scan_depth": "quick"},
        )
        assert result.result == ItemResult.FAILED

    def test_corrupt_file_can_be_quarantined(self, tmp_path: Path) -> None:
        corrupt = tmp_path / "bad.cbz"
        corrupt.write_bytes(b"GARBAGE")
        trash_dir = tmp_path / ".trash"

        executor = IntegrityCheckerExecutor()
        result = executor.process_item(
            item_data={"id": "item-004", "file_path": str(corrupt), "operation": "check"},
            job_config={
                "scan_depth": "quick",
                "corrupt_action": "quarantine",
                "trash_folder": str(trash_dir),
            },
        )

        assert result.result == ItemResult.COMPLETED
        assert result.before_state.get("path") == str(corrupt)
        assert result.after_state.get("status") == "corrupt"
        assert result.after_state.get("action") == "quarantine"
        assert Path(result.after_state["trash_path"]).exists()
        assert not corrupt.exists()

    def test_corrupt_referenced_file_is_reported_without_quarantine(self, tmp_path: Path) -> None:
        corrupt = tmp_path / "referenced-bad.cbz"
        original = b"GARBAGE"
        corrupt.write_bytes(original)

        result = IntegrityCheckerExecutor().process_item(
            item_data={
                "id": "item-referenced",
                "file_path": str(corrupt),
                "operation": "check",
                "storage_mode": "referenced",
            },
            job_config={
                "scan_depth": "quick",
                "corrupt_action": "quarantine",
                "trash_folder": str(tmp_path / ".trash"),
            },
        )

        assert result.result == ItemResult.FAILED
        assert result.after_state["action"] == "report"
        assert result.after_state["quarantine_blocked_reason"] == "referenced_file"
        assert corrupt.read_bytes() == original
        assert not (tmp_path / ".trash").exists()


# ── Rollback ───────────────────────────────────────────────────


class TestRollback:
    """Verify rollback resets integrity flags."""

    def test_rollback_returns_completed(self) -> None:
        executor = IntegrityCheckerExecutor()
        result = executor.rollback_item(
            item_data={"id": "rb-001"},
            job_config={},
        )
        assert result.result == ItemResult.COMPLETED

    def test_rollback_restores_quarantined_file(self, tmp_path: Path) -> None:
        corrupt = tmp_path / "restore-me.cbz"
        original_bytes = b"GARBAGE"
        corrupt.write_bytes(original_bytes)
        trash_dir = tmp_path / ".trash"

        executor = IntegrityCheckerExecutor()
        processed = executor.process_item(
            item_data={"id": "rb-002", "file_path": str(corrupt), "operation": "check"},
            job_config={
                "scan_depth": "quick",
                "corrupt_action": "quarantine",
                "trash_folder": str(trash_dir),
            },
        )

        result = executor.rollback_item(
            item_data={
                "id": "rb-002",
                "before_state": processed.before_state,
                "after_state": processed.after_state,
            },
            job_config={},
        )

        assert result.result == ItemResult.COMPLETED
        assert corrupt.exists()
        assert corrupt.read_bytes() == original_bytes


# ── Standalone Import ──────────────────────────────────────────


class TestStandaloneImport:
    """Verify the standalone function is importable (cross-sprint constraint)."""

    def test_importable(self) -> None:
        from pullbox.utilities.executors.integrity_checker import check_file_integrity

        assert callable(check_file_integrity)


# ── Integrity Edge Cases ──────────────────────────────────────


class TestIntegrityEdgeCases:
    """Verify edge cases in integrity checking."""

    @pytest.mark.asyncio
    async def test_permission_error_on_read(self, tmp_path: Path) -> None:
        """File exists but not readable returns corrupt with error message."""
        import os
        import sys

        if sys.platform == "win32":
            pytest.skip("chmod not effective on Windows")

        unreadable = tmp_path / "locked.cbz"
        _create_valid_cbz(unreadable, page_count=3)
        os.chmod(unreadable, 0o000)

        try:
            result = await check_file_integrity(unreadable, deep=False)
            assert result.status == "corrupt"
            assert len(result.errors) >= 1
        finally:
            os.chmod(unreadable, 0o644)

    @pytest.mark.asyncio
    async def test_archive_with_mixed_image_formats(self, tmp_path: Path) -> None:
        """CBZ with .jpg, .png, .gif all counted as pages."""
        cbz = tmp_path / "mixed_images.cbz"
        with zipfile.ZipFile(cbz, "w") as zf:
            zf.writestr("cover.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 500)
            zf.writestr("page_001.png", b"\x89PNG" + b"\x00" * 500)
            zf.writestr("page_002.gif", b"GIF89a" + b"\x00" * 500)

        result = await check_file_integrity(cbz, deep=False)
        assert result.status == "healthy"
        assert result.page_count == 3

    @pytest.mark.asyncio
    async def test_single_image_archive_passes(self, tmp_path: Path) -> None:
        """Archive with exactly one image is healthy."""
        cbz = _create_valid_cbz(tmp_path / "single.cbz", page_count=1)
        result = await check_file_integrity(cbz, deep=False)
        assert result.status == "healthy"
        assert result.page_count == 1

    @pytest.mark.asyncio
    async def test_cbz_with_comicinfo_not_counted_as_image(self, tmp_path: Path) -> None:
        """ComicInfo.xml doesn't inflate page_count."""
        cbz = tmp_path / "with_ci.cbz"
        with zipfile.ZipFile(cbz, "w") as zf:
            for i in range(5):
                zf.writestr(f"page_{i:03d}.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 500)
            zf.writestr(
                "ComicInfo.xml",
                '<?xml version="1.0"?><ComicInfo><Series>Test</Series></ComicInfo>',
            )

        result = await check_file_integrity(cbz, deep=False)
        assert result.status == "healthy"
        assert result.page_count == 5  # ComicInfo.xml NOT counted


# ── Standalone Checker Branch Coverage ─────────────────────────


class TestStandaloneIntegrityBranches:
    """Cover defensive branches in the standalone integrity helpers."""

    def test_relative_path_helper(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        child = root / "child"

        assert integrity_checks._is_relative_to(child, root) is True
        assert integrity_checks._is_relative_to(root, child) is False

    def test_unknown_archive_suffix_uses_zip_checker(self, tmp_path: Path) -> None:
        path = _create_valid_cbz(tmp_path / "unknown.ext", page_count=1)

        result = integrity_checks._check_sync(path, deep=False)

        assert result.status == "healthy"
        assert result.page_count == 1
        assert result.file_hash

    def test_pdf_suffix_uses_pdf_checker(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "comic.pdf"
        path.write_bytes(b"%PDF")
        monkeypatch.setattr(
            integrity_checks,
            "_check_pdf",
            lambda path, deep, warnings, errors: integrity_checks.IntegrityResult(
                status="healthy",
                page_count=3,
            ),
        )

        result = integrity_checks._check_sync(path, deep=True)

        assert result.status == "healthy"
        assert result.page_count == 3
        assert result.file_hash

    def test_zip_bad_entry_is_corrupt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "bad-entry.cbz"
        path.write_bytes(b"zip")

        class FakeZip:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                return None

            def __enter__(self) -> FakeZip:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def testzip(self) -> str:
                return "page_001.jpg"

        monkeypatch.setattr(integrity_checks.zipfile, "ZipFile", FakeZip)

        result = integrity_checks._check_zip(path, False, [], [])

        assert result.status == "corrupt"
        assert result.errors == ["Corrupt entry: page_001.jpg"]

    def test_zip_deep_verification_errors_are_corrupt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = _create_valid_cbz(tmp_path / "deep-error.cbz", page_count=1)
        monkeypatch.setattr(
            integrity_checks,
            "_deep_verify_zip_images",
            lambda archive, image_names, warnings: ["page failed"],
        )

        result = integrity_checks._check_zip(path, True, [], [])

        assert result.status == "corrupt"
        assert result.page_count == 1
        assert result.errors == ["page failed"]

    def test_deep_zip_warns_when_pillow_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = _create_valid_cbz(tmp_path / "missing-pillow.cbz", page_count=1)
        warnings: list[str] = []
        original_import = __import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "PIL":
                raise ImportError("no pillow")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        with zipfile.ZipFile(path, "r") as archive:
            errors = integrity_checks._deep_verify_zip_images(archive, ["page_000.jpg"], warnings)

        assert errors == []
        assert warnings == ["Pillow not available for deep image verification"]

    def test_7z_branches(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / "archive.cb7"
        path.write_bytes(b"7z")

        class FakeSevenZip:
            names: ClassVar[list[str]] = ["page_001.jpg"]

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                return None

            def __enter__(self) -> FakeSevenZip:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def getnames(self) -> list[str]:
                return self.names

            def extract(self, *, path: str, targets: list[str]) -> None:
                _ = path, targets

        monkeypatch.setattr(py7zr, "SevenZipFile", FakeSevenZip)
        result = integrity_checks._check_7z(path, False, [], [])
        assert result.status == "healthy"
        assert result.page_count == 1

        FakeSevenZip.names = ["readme.txt"]
        assert integrity_checks._check_7z(path, False, [], []).status == "corrupt"

        FakeSevenZip.names = ["../escaped.jpg"]
        unsafe = integrity_checks._check_7z(path, False, [], [])
        assert unsafe.status == "corrupt"
        assert unsafe.errors == ["Unsafe archive member path: ../escaped.jpg"]

        FakeSevenZip.names = ["page_001.jpg"]
        monkeypatch.setattr(
            integrity_checks,
            "_deep_verify_extracted_images",
            lambda extract_dir, image_names, warnings: ["bad extracted image"],
        )
        deep = integrity_checks._check_7z(path, True, [], [])
        assert deep.status == "corrupt"
        assert deep.errors == ["bad extracted image"]

        monkeypatch.setattr(
            py7zr,
            "SevenZipFile",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cannot open")),
        )
        failed = integrity_checks._check_7z(path, False, [], [])
        assert failed.status == "corrupt"
        assert failed.errors == ["Cannot open 7z archive: cannot open"]

    def test_rar_branches(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import rarfile

        path = tmp_path / "archive.cbr"
        path.write_bytes(b"rar")

        class FakeRar:
            names: ClassVar[list[str]] = ["page_001.jpg"]

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                return None

            def __enter__(self) -> FakeRar:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def namelist(self) -> list[str]:
                return self.names

            def extract(self, image_name: str, path: str) -> None:
                destination = Path(path) / image_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(_valid_jpeg_bytes())

        monkeypatch.setattr(integrity_checks, "configure_rarfile_backend", lambda: None)
        monkeypatch.setattr(rarfile, "RarFile", FakeRar)
        assert integrity_checks._check_rar(path, False, [], []).status == "healthy"

        FakeRar.names = ["readme.txt"]
        assert integrity_checks._check_rar(path, False, [], []).errors == [
            "No valid images found in archive"
        ]

        FakeRar.names = ["../escaped.jpg"]
        unsafe = integrity_checks._check_rar(path, False, [], [])
        assert unsafe.errors == ["Unsafe archive member path: ../escaped.jpg"]

        FakeRar.names = ["page_001.jpg"]
        monkeypatch.setattr(
            integrity_checks,
            "_deep_verify_extracted_images",
            lambda extract_dir, image_names, warnings: ["bad rar image"],
        )
        deep = integrity_checks._check_rar(path, True, [], [])
        assert deep.status == "corrupt"
        assert deep.errors == ["bad rar image"]

        monkeypatch.setattr(
            rarfile,
            "RarFile",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rar boom")),
        )
        failed = integrity_checks._check_rar(path, False, [], [])
        assert failed.errors == ["Cannot open RAR archive: rar boom"]

    def test_tar_error_and_deep_error_branches(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        no_images = tmp_path / "no-images.cbt"
        with tarfile.open(no_images, "w") as archive:
            data = b"hello"
            info = tarfile.TarInfo(name="readme.txt")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        assert integrity_checks._check_tar(no_images, False, [], []).errors == [
            "No valid images found in archive"
        ]

        valid = _create_valid_cbt(tmp_path / "deep.cbt", page_count=1)
        monkeypatch.setattr(
            integrity_checks,
            "_deep_verify_extracted_images",
            lambda extract_dir, image_names, warnings: ["bad tar image"],
        )
        deep = integrity_checks._check_tar(valid, True, [], [])
        assert deep.status == "corrupt"
        assert deep.errors == ["bad tar image"]

        invalid = tmp_path / "invalid.cbt"
        invalid.write_bytes(b"not tar")
        failed = integrity_checks._check_tar(invalid, False, [], [])
        assert failed.status == "corrupt"
        assert failed.errors[0].startswith("Cannot open TAR archive:")

    def test_safe_extract_tar_members_rejects_missing_sources(self, tmp_path: Path) -> None:
        class FakeArchive:
            def extractfile(self, _member: tarfile.TarInfo) -> None:
                return None

        member = tarfile.TarInfo(name="page_001.jpg")
        member.size = 1

        with pytest.raises(tarfile.TarError, match="Cannot extract archive member"):
            integrity_checks._safe_extract_tar_members(FakeArchive(), [member], tmp_path)

    def test_safe_extract_tar_members_rejects_unsafe_paths(self, tmp_path: Path) -> None:
        class FakeArchive:
            def extractfile(self, _member: tarfile.TarInfo) -> io.BytesIO:
                return io.BytesIO(b"data")

        traversal = tarfile.TarInfo(name="../escaped.jpg")
        traversal.size = 4
        absolute_escape = tarfile.TarInfo(name="/tmp/escaped.jpg")
        absolute_escape.size = 4

        with pytest.raises(tarfile.TarError, match="Unsafe archive member path"):
            integrity_checks._safe_extract_tar_members(FakeArchive(), [traversal], tmp_path)
        with pytest.raises(tarfile.TarError, match="Unsafe archive member path"):
            integrity_checks._safe_extract_tar_members(FakeArchive(), [absolute_escape], tmp_path)

    def test_pdf_branches(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pdf = tmp_path / "comic.pdf"
        pdf.write_bytes(b"%PDF")

        module = types.ModuleType("pdf2image")
        module.pdfinfo_from_path = lambda _path: {"Pages": 0}
        module.convert_from_path = lambda *_args, **_kwargs: []
        monkeypatch.setitem(sys.modules, "pdf2image", module)
        no_pages = integrity_checks._check_pdf(pdf, False, [], [])
        assert no_pages.status == "corrupt"
        assert no_pages.errors == ["PDF has no pages"]

        module.pdfinfo_from_path = lambda _path: {"Pages": 2}
        module.convert_from_path = lambda *_args, **_kwargs: [object()]
        warning = integrity_checks._check_pdf(pdf, True, [], [])
        assert warning.status == "warning"
        assert warning.warnings == ["Expected 2 pages but rendered 1"]

        module.convert_from_path = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("render boom")
        )
        render_failed = integrity_checks._check_pdf(pdf, True, [], [])
        assert render_failed.status == "corrupt"
        assert render_failed.errors == ["PDF page rendering failed: render boom"]

        module.pdfinfo_from_path = lambda _path: (_ for _ in ()).throw(RuntimeError("info boom"))
        read_failed = integrity_checks._check_pdf(pdf, False, [], [])
        assert read_failed.status == "corrupt"
        assert read_failed.errors == ["Cannot read PDF: info boom"]

    def test_pdf_reports_missing_pdf2image(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pdf = tmp_path / "comic.pdf"
        pdf.write_bytes(b"%PDF")
        original_import = __import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "pdf2image":
                raise ImportError("missing")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)

        result = integrity_checks._check_pdf(pdf, False, [], [])

        assert result.status == "corrupt"
        assert result.errors == ["pdf2image not available for PDF verification"]

    def test_deep_verify_extracted_images_reports_missing_pillow(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        warnings: list[str] = []
        original_import = __import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "PIL":
                raise ImportError("no pillow")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        assert (
            integrity_checks._deep_verify_extracted_images(
                tmp_path,
                ["missing.jpg"],
                warnings,
            )
            == []
        )
        assert warnings == ["Pillow not available for deep image verification"]

    def test_deep_verify_extracted_images_reports_file_errors(self, tmp_path: Path) -> None:
        warnings: list[str] = []
        bad_image = tmp_path / "bad.jpg"
        bad_image.write_bytes(b"not an image")
        errors = integrity_checks._deep_verify_extracted_images(
            tmp_path,
            ["missing.jpg", "bad.jpg"],
            warnings,
        )

        assert errors[0] == "missing.jpg: extracted file not found"
        assert errors[1].startswith("bad.jpg: image verification failed")

"""Archive safety contracts for import and post-processing boundaries."""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING, Any

import pytest

from pullbox.core.file_safety import (
    FileSafetyError,
    ResourceSafetyBlock,
    check_archive_contents_for_dangerous_files,
    check_archive_path_traversal,
    check_archive_size,
    check_download_safety,
    classify_resource_safety_exception,
    ensure_zip_archive_inspectable,
    has_archive_member_path_traversal,
    inspect_zip_archive_safety,
    is_resource_safety_exception_allowed,
    run_safety_checks,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_zip_path_traversal_detection_handles_posix_and_windows_entries(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "malicious.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../outside.jpg", b"bad")
        zf.writestr(r"..\outside.jpg", b"bad")
        zf.writestr("/absolute.jpg", b"bad")
        zf.writestr(r"C:\absolute.jpg", b"bad")
        zf.writestr("safe/page.jpg", b"ok")

    assert check_archive_path_traversal(archive) == [
        "../outside.jpg",
        r"..\outside.jpg",
        "/absolute.jpg",
        r"C:\absolute.jpg",
    ]


@pytest.mark.parametrize(
    ("entry_name", "expected"),
    [
        ("safe/page001.jpg", False),
        ("../outside.jpg", True),
        (r"..\outside.jpg", True),
        ("/absolute.jpg", True),
        (r"\absolute.jpg", True),
        (r"C:\absolute.jpg", True),
    ],
)
def test_archive_member_path_traversal_detection(entry_name: str, expected: bool) -> None:
    assert has_archive_member_path_traversal(entry_name) is expected


def test_run_safety_checks_rejects_unreadable_zip_archive(tmp_path: Path) -> None:
    archive = tmp_path / "corrupt.cbz"
    archive.write_bytes(b"not a zip archive")

    with pytest.raises(FileSafetyError, match="Archive could not be inspected"):
        run_safety_checks(
            archive,
            block_dangerous=True,
            max_archive_size=2000 * 1024 * 1024,
        )


def test_ensure_zip_archive_inspectable_ignores_non_zip_and_accepts_valid_zip(
    tmp_path: Path,
) -> None:
    non_zip = tmp_path / "issue.cbr"
    non_zip.write_bytes(b"rar-ish")
    archive = tmp_path / "issue.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("page001.jpg", b"ok")

    ensure_zip_archive_inspectable(non_zip)
    ensure_zip_archive_inspectable(archive)


def test_run_safety_checks_inspects_zip_archive_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "safe.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("safe/page001.jpg", b"ok")
        zf.writestr("safe/page002.jpg", b"ok")
        zf.writestr(
            "metadata/ComicInfo.xml",
            "<ComicInfo><Series>Batman</Series><Number>1</Number></ComicInfo>",
        )

    open_count = 0
    original_zip_file = zipfile.ZipFile

    def counting_zip_file(*args: Any, **kwargs: Any) -> Any:
        nonlocal open_count
        open_count += 1
        return original_zip_file(*args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", counting_zip_file)

    inspection = run_safety_checks(
        archive,
        block_dangerous=True,
        max_archive_size=2000 * 1024 * 1024,
    )

    assert open_count == 1
    assert len(inspection.archives) == 1
    report = inspection.archives[0]
    assert report.archive_path == archive
    assert report.comicinfo is not None
    assert report.comicinfo.series == "Batman"
    assert report.comicinfo.number == "1"
    assert report.comicinfo_entry == "metadata/ComicInfo.xml"
    assert report.comicinfo_entry_count == 1
    assert report.comicinfo_error is None
    assert report.entry_names == (
        "safe/page001.jpg",
        "safe/page002.jpg",
        "metadata/ComicInfo.xml",
    )


def test_run_safety_checks_rejects_dangerous_files_on_disk(tmp_path: Path) -> None:
    payload = tmp_path / "nested" / "setup.exe"
    payload.parent.mkdir()
    payload.write_text("bad", encoding="utf-8")

    with pytest.raises(FileSafetyError, match="dangerous file"):
        run_safety_checks(tmp_path, block_dangerous=True, max_archive_size=100)


def test_run_safety_checks_rejects_traversal_size_and_archive_payloads(
    tmp_path: Path,
) -> None:
    traversal = tmp_path / "traversal.cbz"
    with zipfile.ZipFile(traversal, "w") as zf:
        zf.writestr("../outside.jpg", b"bad")
    with pytest.raises(FileSafetyError, match="path traversal"):
        run_safety_checks(traversal, block_dangerous=True, max_archive_size=100)

    oversized = tmp_path / "oversized.cbz"
    with zipfile.ZipFile(oversized, "w") as zf:
        zf.writestr("page001.jpg", b"too-large")
    with pytest.raises(FileSafetyError, match="Archive decompressed size"):
        run_safety_checks(oversized, block_dangerous=True, max_archive_size=1)

    dangerous_archive = tmp_path / "dangerous.cbz"
    with zipfile.ZipFile(dangerous_archive, "w") as zf:
        zf.writestr("setup.exe", b"bad")
    with pytest.raises(FileSafetyError, match="dangerous file"):
        run_safety_checks(dangerous_archive, block_dangerous=True, max_archive_size=100)


def test_archive_helpers_report_size_and_dangerous_entries(tmp_path: Path) -> None:
    archive = tmp_path / "mixed.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("safe/page001.jpg", b"ok")
        zf.writestr("scripts/setup.exe", b"bad")

    assert check_archive_size(archive, max_bytes=100) == 5
    assert check_archive_size(tmp_path / "issue.cbr", max_bytes=100) is None
    assert check_archive_contents_for_dangerous_files(archive) == ["scripts/setup.exe"]

    report = inspect_zip_archive_safety(archive, block_dangerous=True)
    assert report is not None
    assert report.total_size == 5
    assert report.traversal_entries == []
    assert report.dangerous_entries == ["scripts/setup.exe"]
    assert report.entry_names == ("safe/page001.jpg", "scripts/setup.exe")
    assert report.comicinfo is None
    assert report.comicinfo_entry_count == 0
    assert inspect_zip_archive_safety(tmp_path / "issue.cbr", block_dangerous=True) is None


def test_archive_legacy_helpers_fail_open_for_corrupt_zip(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.cbz"
    corrupt.write_bytes(b"not a zip")

    assert check_archive_path_traversal(corrupt) == []
    assert check_archive_contents_for_dangerous_files(corrupt) == []


def test_archive_size_helper_blocks_large_zip_and_ignores_corrupt_zip(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "large.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("page001.jpg", b"too-large")
    corrupt = tmp_path / "corrupt.cbz"
    corrupt.write_bytes(b"not a zip")

    with pytest.raises(FileSafetyError, match="Archive decompressed size"):
        check_archive_size(archive, max_bytes=1)
    assert check_archive_size(corrupt, max_bytes=1) is None


def test_archive_size_error_is_overrideable_resource_safety_exception() -> None:
    block = classify_resource_safety_exception(
        FileSafetyError(
            "Archive decompressed size (4,248,234,210 bytes) exceeds limit (2,097,152,000 bytes)",
            details=["/imports/Dark Nights Death Metal Omnibus.cbz"],
        )
    )

    assert block is not None
    assert block.kind == "archive_decompressed_size"
    assert block.overrideable is True
    assert block.details == ["/imports/Dark Nights Death Metal Omnibus.cbz"]


def test_non_resource_safety_errors_are_not_overrideable() -> None:
    traversal = classify_resource_safety_exception(
        FileSafetyError(
            "Archive contains path traversal entries — entire release rejected",
            details=["../outside.jpg"],
        )
    )
    dangerous = classify_resource_safety_exception(
        FileSafetyError(
            "Archive contains 1 dangerous file(s) — entire release rejected",
            details=["setup.exe"],
        )
    )

    assert traversal is None
    assert dangerous is None


def test_pillow_decompression_bomb_error_is_overrideable_resource_exception() -> None:
    block = classify_resource_safety_exception(
        RuntimeError(
            "Archive worker failed during convert: DecompressionBombError: "
            "Image size exceeds limit of 178956970 pixels"
        )
    )

    assert block is not None
    assert block.kind == "pillow_decompression_bomb"
    assert "safe image processing limit" in block.reason


def test_resource_safety_exception_classification_walks_exception_chain() -> None:
    inner = FileSafetyError(
        "Archive decompressed size (20 bytes) exceeds limit (10 bytes)",
        details=["/imports/large.cbz"],
    )
    outer = RuntimeError("worker failed")
    outer.__cause__ = inner

    block = classify_resource_safety_exception(outer)

    assert block is not None
    assert block.to_diagnostics() == {
        "kind": "archive_decompressed_size",
        "reason": "Archive decompressed size (20 bytes) exceeds limit (10 bytes)",
        "details": ["/imports/large.cbz"],
        "source": "file_safety",
        "overrideable": True,
    }


@pytest.mark.parametrize(
    ("diagnostics", "expected"),
    [
        (None, False),
        ({}, False),
        ({"safety_exception": {"allowed_once": False}}, False),
        ({"safety_exception": {"allowed_once": True}}, False),
        (
            {
                "safety_exception": {
                    "allowed_once": True,
                    "previous_block": ResourceSafetyBlock(
                        kind="archive_decompressed_size",
                        reason="large",
                        details=[],
                    ).to_diagnostics(),
                }
            },
            True,
        ),
        (
            {
                "safety_exception": {
                    "allowed_once": True,
                    "previous_block": {"overrideable": False},
                }
            },
            False,
        ),
    ],
)
def test_resource_safety_exception_allowed_requires_reviewable_previous_block(
    diagnostics: dict[str, object] | None,
    expected: bool,
) -> None:
    assert is_resource_safety_exception_allowed(diagnostics) is expected


@pytest.mark.asyncio
async def test_check_download_safety_prefetches_config_then_runs_sync_checks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pullbox.core import file_safety

    calls: list[tuple[Path, bool, int]] = []

    async def block_dangerous(_session: object) -> bool:
        return False

    async def archive_limit(_session: object) -> int:
        return 123

    def run_checks(download_path: Path, *, block_dangerous: bool, max_archive_size: int) -> None:
        calls.append((download_path, block_dangerous, max_archive_size))

    monkeypatch.setattr(file_safety, "is_dangerous_file_blocking_enabled", block_dangerous)
    monkeypatch.setattr(file_safety, "get_archive_size_limit_bytes", archive_limit)
    monkeypatch.setattr(file_safety, "run_safety_checks", run_checks)

    await check_download_safety(object(), tmp_path / "download")

    assert calls == [(tmp_path / "download", False, 123)]

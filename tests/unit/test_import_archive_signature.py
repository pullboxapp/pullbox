"""Mislabeled archives must use the same reader and safety checks."""

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from pullbox.core.archive import ArchiveReader
from pullbox.core.file_safety import FileSafetyError, run_safety_checks
from pullbox.services.import_content_inspection import inspect_import_content


def test_zip_named_cbr_uses_zip_reader_and_content_inspection(tmp_path: Path) -> None:
    source = tmp_path / "Example.cbr"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("01.jpg", b"first")
        archive.writestr("02.jpg", b"second")
    before = source.read_bytes()
    assert ArchiveReader(source).format == "cbz"
    safety = run_safety_checks(source, block_dangerous=True, max_archive_size=1000)
    assert safety.archives[0].archive_path == source
    assert inspect_import_content(source, safety)["archive_format"] == {
        "declared": "cbr",
        "detected": "cbz",
        "mismatch": True,
    }
    assert source.read_bytes() == before


@pytest.mark.parametrize("member", ["../bad.jpg", "payload.exe"])
def test_disguised_zip_cannot_bypass_safety(tmp_path: Path, member: str) -> None:
    source = tmp_path / "unsafe.cbr"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(member, b"bad")
    with pytest.raises(FileSafetyError):
        run_safety_checks(source, block_dangerous=True, max_archive_size=1000)


def test_disguised_zip_cannot_bypass_size_review(tmp_path: Path) -> None:
    source = tmp_path / "large.cbr"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("01.jpg", b"x" * 2000)
    with pytest.raises(FileSafetyError, match="exceeds"):
        run_safety_checks(source, block_dangerous=True, max_archive_size=1000)


@pytest.mark.parametrize("name", ["../outside.jpg", "script.exe"])
def test_non_zip_with_cbz_suffix_keeps_member_safety_checks(tmp_path: Path, name: str):
    source = tmp_path / "disguised.cbz"
    with tarfile.open(source, "w", format=tarfile.USTAR_FORMAT) as archive:
        entry = tarfile.TarInfo(name)
        entry.size = 1
        archive.addfile(entry, io.BytesIO(b"x"))
    with pytest.raises(FileSafetyError):
        run_safety_checks(source, block_dangerous=True, max_archive_size=1000)

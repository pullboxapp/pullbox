"""Read access is harmless; content and mutation metadata changes are not."""

import os
from pathlib import Path

import pytest

from tests.file_preservation import file_preservation_snapshot


def changed_stat(original: os.stat_result, field: str) -> os.stat_result:
    values = list(original)
    attributes = {
        name: getattr(original, name)
        for name in (
            "st_atime",
            "st_mtime",
            "st_ctime",
            "st_atime_ns",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    }
    if field == "st_atime":
        values[7] += 1
        attributes["st_atime"] += 1
        attributes["st_atime_ns"] += 1_000_000_000
    elif field.endswith("_ns"):
        # Whole-second stat tuple equality cannot detect this mutation.
        attributes[field] += 1
    else:
        index = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size").index(
            field
        )
        values[index] += 1
    return os.stat_result(values, attributes)


def test_file_preservation_snapshot_allows_access_time_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.cb7"
    source.write_bytes(b"original")
    before = file_preservation_snapshot(source)
    accessed = changed_stat(source.stat(), "st_atime")
    monkeypatch.setattr(Path, "stat", lambda self, **kwargs: accessed)

    assert file_preservation_snapshot(source) == before, "Reading a source is not a mutation"


@pytest.mark.parametrize(
    "field",
    [
        "st_mode",
        "st_ino",
        "st_dev",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    ],
)
def test_file_preservation_snapshot_detects_metadata_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    source = tmp_path / "source.cb7"
    source.write_bytes(b"original")
    before = file_preservation_snapshot(source)
    mutated = changed_stat(source.stat(), field)
    monkeypatch.setattr(Path, "stat", lambda self, **kwargs: mutated)

    assert file_preservation_snapshot(source) != before, f"Mutation to {field} went undetected"


def test_file_preservation_snapshot_detects_same_size_content_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.cb7"
    source.write_bytes(b"original")
    before = file_preservation_snapshot(source)
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"modified")

    assert file_preservation_snapshot(source) != before, "Changed source bytes went undetected"

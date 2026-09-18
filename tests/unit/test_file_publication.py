"""Atomic publication must never trade compatibility for no-overwrite safety."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pullbox.core import file_publication


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_publication_consumes_stage_without_changing_its_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool, kind: str
) -> None:
    source = tmp_path / "source.cbz"
    source.write_bytes(b"comic")
    stage = tmp_path / "stage"
    if kind == "file":
        stage.hardlink_to(source)
    else:
        stage.symlink_to(source.name)
    if fallback:
        monkeypatch.setattr(
            file_publication, "_native_rename_without_overwrite", lambda *args: False
        )
    target = tmp_path / "target.cbz"

    file_publication.publish_file_without_overwrite(stage, target)

    assert not os.path.lexists(stage)
    assert target.read_bytes() == b"comic"
    assert target.is_symlink() == (kind == "symlink")
    assert target.samefile(source)
    if kind == "symlink":
        assert os.readlink(target) == source.name


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("kind", ["file", "directory", "dangling_symlink"])
def test_publication_preserves_every_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool, kind: str
) -> None:
    stage = tmp_path / "stage"
    stage.write_bytes(b"new comic")
    target = tmp_path / "target"
    if kind == "file":
        target.write_bytes(b"existing comic")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to("missing")
    original = target.lstat()
    if fallback:
        monkeypatch.setattr(
            file_publication, "_native_rename_without_overwrite", lambda *args: False
        )

    with pytest.raises(FileExistsError):
        file_publication.publish_file_without_overwrite(stage, target)

    assert stage.read_bytes() == b"new comic"
    assert target.lstat() == original
    if kind == "file":
        assert target.read_bytes() == b"existing comic"


@pytest.mark.parametrize(
    "platform,symbol,flags",
    [
        ("linux", "renameat2", 1),
        ("darwin", "renamex_np", 4),
    ],
)
@pytest.mark.parametrize(
    "error_number",
    [
        0,
        errno.ENOSYS,
        errno.EINVAL,
        errno.ENOTSUP,
        errno.EPERM,
        errno.EEXIST,
        errno.EIO,
        errno.ENOSPC,
    ],
)
def test_native_rename_abi_and_errors(
    monkeypatch: pytest.MonkeyPatch, platform: str, symbol: str, flags: int, error_number: int
) -> None:
    native = Mock(return_value=-1 if error_number else 0)
    library = Mock(return_value=SimpleNamespace(**{symbol: native}))
    monkeypatch.setattr(file_publication, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(ctypes, "CDLL", library)
    monkeypatch.setattr(ctypes, "get_errno", lambda: error_number)
    source, target = Path("/library/stage"), Path("/library/target")

    if error_number and error_number not in file_publication._UNSUPPORTED_RENAME:
        with pytest.raises(OSError) as error:
            file_publication._native_rename_without_overwrite(source, target)
        assert error.value.errno == error_number
        assert error.value.filename == str(source)
        assert error.value.filename2 == str(target)
    else:
        assert file_publication._native_rename_without_overwrite(source, target) is (
            error_number == 0
        )

    library.assert_called_once_with(None, use_errno=True)
    if platform == "linux":
        native.assert_called_once_with(-100, b"/library/stage", -100, b"/library/target", flags)
        assert native.argtypes == [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
    else:
        native.assert_called_once_with(b"/library/stage", b"/library/target", flags)
        assert native.argtypes == [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    assert native.restype == ctypes.c_int


@pytest.mark.parametrize("error_number", [errno.EPERM, errno.EIO, errno.ENOSPC])
def test_real_errors_do_not_try_another_publication_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    stage, target = tmp_path / "stage", tmp_path / "target"
    stage.write_bytes(b"comic")
    native = Mock(side_effect=OSError(error_number, os.strerror(error_number)))
    link = Mock()
    monkeypatch.setattr(file_publication, "_native_rename_without_overwrite", native)
    monkeypatch.setattr(os, "link", link)

    with pytest.raises(OSError) as error:
        file_publication.publish_file_without_overwrite(stage, target)

    assert error.value.errno == error_number
    link.assert_not_called()
    assert stage.read_bytes() == b"comic"
    assert not target.exists()


def test_publication_rejects_a_directory_stage(tmp_path: Path) -> None:
    stage, target = tmp_path / "stage", tmp_path / "target"
    stage.mkdir()
    with pytest.raises(OSError, match="regular file or symlink"):
        file_publication.publish_file_without_overwrite(stage, target)
    assert stage.is_dir()
    assert not target.exists()


def test_native_missing_symbol_uses_link_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())
    file_publication.probe_file_publication(tmp_path)
    assert not list(tmp_path.iterdir())


def test_native_rejects_null_bytes_in_target(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.write_bytes(b"comic")
    with pytest.raises(ValueError, match="null byte"):
        file_publication.publish_file_without_overwrite(stage, tmp_path / "target\0suffix")
    assert stage.read_bytes() == b"comic"
    assert not (tmp_path / "target").exists()


def test_probe_checks_no_overwrite_contract_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_publication, "publish_file_without_overwrite", os.replace)
    with pytest.raises(OSError, match="preserve an existing destination"):
        file_publication.probe_file_publication(tmp_path)
    assert not list(tmp_path.iterdir())


def test_windows_uses_exclusive_os_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    rename = Mock()
    monkeypatch.setattr(file_publication, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(os, "rename", rename)
    source, target = Path("stage"), Path("target")
    assert file_publication._native_rename_without_overwrite(source, target)
    rename.assert_called_once_with(source, target)

"""Strict completed-file mapping for untrusted AirDC++ targets."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from pullbox.services.airdcpp_path_mapping import (
    AirDcppPathMappingError,
    map_airdcpp_completed_path,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_completed_path_maps_exact_child_under_resolved_local_root(tmp_path: Path) -> None:
    local_root = tmp_path / "airdcpp"
    local_root.mkdir()
    completed = local_root / "Example Comic 001 (2026).cbz"
    completed.write_bytes(b"synthetic")

    mapped = map_airdcpp_completed_path(
        remote_target="/Downloads/Example Comic 001 (2026).cbz",
        remote_root="/Downloads",
        local_root=str(local_root),
    )

    assert mapped == completed.resolve(strict=True)


@pytest.mark.parametrize(
    "remote_target",
    [
        "/Downloads",
        "/Downloads2/Example.cbz",
        "/Downloads/../Secrets/Example.cbz",
        "Downloads/Example.cbz",
    ],
)
def test_completed_path_rejects_root_prefix_traversal_and_relative_targets(
    tmp_path: Path,
    remote_target: str,
) -> None:
    local_root = tmp_path / "airdcpp"
    local_root.mkdir()

    with pytest.raises(AirDcppPathMappingError):
        map_airdcpp_completed_path(
            remote_target=remote_target,
            remote_root="/Downloads",
            local_root=str(local_root),
            require_file=False,
        )


def test_completed_path_rejects_symlink_parent_escape(tmp_path: Path) -> None:
    local_root = tmp_path / "airdcpp"
    outside = tmp_path / "outside"
    local_root.mkdir()
    outside.mkdir()
    (outside / "Example.cbz").write_bytes(b"synthetic")
    os.symlink(outside, local_root / "escaped")

    with pytest.raises(AirDcppPathMappingError):
        map_airdcpp_completed_path(
            remote_target="/Downloads/escaped/Example.cbz",
            remote_root="/Downloads",
            local_root=str(local_root),
        )


def test_completed_path_rejects_symlink_file_escape(tmp_path: Path) -> None:
    local_root = tmp_path / "airdcpp"
    outside = tmp_path / "outside.cbz"
    local_root.mkdir()
    outside.write_bytes(b"synthetic")
    os.symlink(outside, local_root / "Example.cbz")

    with pytest.raises(AirDcppPathMappingError):
        map_airdcpp_completed_path(
            remote_target="/Downloads/Example.cbz",
            remote_root="/Downloads",
            local_root=str(local_root),
        )


def test_completed_path_rejects_directory_fifo_and_unsupported_extension(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "airdcpp"
    local_root.mkdir()
    (local_root / "folder.cbz").mkdir()
    fifo = local_root / "pipe.cbz"
    os.mkfifo(fifo)
    (local_root / "script.exe").write_bytes(b"synthetic")

    for name in ("folder.cbz", "pipe.cbz", "script.exe"):
        with pytest.raises(AirDcppPathMappingError):
            map_airdcpp_completed_path(
                remote_target=f"/Downloads/{name}",
                remote_root="/Downloads",
                local_root=str(local_root),
            )

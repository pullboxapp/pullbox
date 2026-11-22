"""Verify physical Story Arc representations beneath explicitly supplied offline mounts."""

from __future__ import annotations

import json
import os
import stat
from typing import TYPE_CHECKING

from pullbox.services.migration_asset_files import (
    AssetVerificationError,
    file_stamp,
    open_directory,
    regular_evidence,
    relative_parts,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from contextlib import ExitStack
    from pathlib import Path

    from pullbox.services.migration_assets import AssetReport


class MediaMounts:
    def __init__(
        self,
        stack: ExitStack,
        roots: dict[int, str],
        mappings: dict[int, Path],
        problems: dict[int, str],
    ) -> None:
        self.stack = stack
        self.roots = roots
        self.mappings = mappings
        self.problems = problems
        self._opened: dict[Path, int] = {}

    def open(self, path: Path) -> int:
        if path not in self._opened:
            if not path.is_absolute() or ".." in path.parts or "\0" in str(path):
                raise AssetVerificationError("invalid_media_mount")
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.stack.callback(os.close, fd)
            if os.path.samestat(os.fstat(fd), os.stat("/")):
                raise AssetVerificationError("invalid_media_mount")
            self._opened[path] = fd
        return self._opened[path]

    def locate(self, root_id: int, path: str) -> tuple[int, tuple[str, ...], Path]:
        if root_id not in self.roots or root_id not in self.mappings:
            if root_id in self.roots:
                self.problems[root_id] = "media_root_mapping_required"
            raise AssetVerificationError("media_root_mapping_required")
        parts = relative_parts(self.roots[root_id], path)
        mount = self.mappings[root_id]
        return self.open(mount), parts, mount.joinpath(*parts)

    def recheck(self) -> None:
        for path, fd in self._opened.items():
            if not os.path.samestat(os.stat(path, follow_symlinks=False), os.fstat(fd)):
                raise AssetVerificationError("mapped_root_changed")


def _arc(
    mounts: MediaMounts,
    target_id: int,
    target: str,
    source_id: int,
    source: str,
    mode: str,
    style: str | None,
    saved_source: str,
    last_result: str,
) -> str:
    src, sp, source_path = mounts.locate(source_id, source)
    dst, dp, target_path = mounts.locate(target_id, target)
    recorded = json.loads(saved_source)
    result = json.loads(last_result)
    if not isinstance(recorded, dict) or not isinstance(result, dict):
        raise AssetVerificationError("arc_evidence_invalid")
    expected = result.get("target_fingerprint")
    if not recorded or not isinstance(expected, dict) or not expected:
        raise AssetVerificationError("arc_evidence_missing")
    canonical = regular_evidence(src, sp)
    with open_directory(dst, dp[:-1]) as directory:
        before = os.stat(dp[-1], dir_fd=directory, follow_symlinks=False)
        is_link = stat.S_ISLNK(before.st_mode)
        same = (before.st_dev, before.st_ino) == (canonical["device"], canonical["inode"])
        if (
            source_path == target_path
            or (mode == "hardlink" and (not same or before.st_nlink < 2))
            or (mode == "copy" and same)
            or (mode == "symlink" and not is_link)
            or (is_link and mode not in {"symlink", "reference_only"})
        ):
            raise AssetVerificationError("arc_representation_changed")
        if is_link:
            link = os.readlink(dp[-1], dir_fd=directory)
            absolute = style == "absolute" if mode == "symlink" else os.path.isabs(link)
            allowed = (
                str(source_path) if absolute else os.path.relpath(source_path, target_path.parent)
            )
            if link != allowed:
                raise AssetVerificationError("arc_representation_changed")
            # Compare the known destination relative to the actual parent descriptor.
            # Relocating bind mounts can leave identical link text pointing elsewhere.
            try:
                reached = os.stat(allowed, dir_fd=directory, follow_symlinks=False)
            except OSError:
                raise AssetVerificationError("arc_representation_changed") from None
            if (reached.st_dev, reached.st_ino) != (canonical["device"], canonical["inode"]):
                raise AssetVerificationError("arc_representation_changed")
            actual = {
                "schema_version": 1,
                "kind": "symlink",
                "link_target": link,
                "device": before.st_dev,
                "inode": before.st_ino,
                "mtime_ns": before.st_mtime_ns,
                "content": canonical,
            }
        else:
            actual = regular_evidence(dst, dp)
            if canonical["sha256"] != actual["sha256"]:
                raise AssetVerificationError("arc_content_changed")
        with open_directory(dst, dp[:-1]) as reopened:
            if file_stamp(before) != file_stamp(
                os.stat(dp[-1], dir_fd=reopened, follow_symlinks=False)
            ):
                raise AssetVerificationError("file_changed")
    with open_directory(src, sp[:-1]) as reopened:
        now = os.stat(sp[-1], dir_fd=reopened, follow_symlinks=False)
        if (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns) != (
            canonical["device"],
            canonical["inode"],
            canonical["size"],
            canonical["mtime_ns"],
        ):
            raise AssetVerificationError("file_changed")
    if canonical != recorded or actual != expected:
        raise AssetVerificationError("arc_saved_identity_changed")
    return str(canonical["sha256"])


def verify_placements(
    db: sqlite3.Connection, mounts: MediaMounts, report: AssetReport, add: Callable[[bytes], None]
) -> None:
    rows = db.execute(
        "SELECT p.id,p.library_root_id,p.placement_path,f.library_root_id,f.file_path,p.mode,"
        "p.symlink_style,p.source_fingerprint,p.last_result,p.state,f.issue_id,m.issue_id "
        "FROM story_arc_placements p JOIN issue_story_arcs m ON m.id=p.issue_story_arc_id "
        "LEFT JOIN library_files f ON f.id=p.library_file_id ORDER BY p.id"
    )
    for (
        ident,
        target_id,
        path,
        source_id,
        source,
        mode,
        style,
        saved,
        result,
        state,
        fid,
        mid,
    ) in rows:
        try:
            if state != "current":
                raise AssetVerificationError("arc_record_needs_review")
            if source is None or fid is None or fid != mid:
                raise AssetVerificationError("arc_source_unproven")
            if mode not in {"copy", "hardlink", "symlink", "reference_only"} or (
                style not in {"absolute", "relative"} if mode == "symlink" else style is not None
            ):
                raise AssetVerificationError("arc_evidence_invalid")
            digest = _arc(mounts, target_id, path, source_id, source, mode, style, saved, result)
        except (AssetVerificationError, OSError, ValueError, TypeError, RecursionError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, AssetVerificationError)
                else "arc_file_missing"
                if isinstance(exc, FileNotFoundError)
                else "arc_unreadable"
            )
            report.media_block(reason, ident)
        else:
            report.placements_checked += 1
            add(f"arc:{ident}:{mode}:{digest}\n".encode())

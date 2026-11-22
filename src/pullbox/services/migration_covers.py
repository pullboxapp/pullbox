"""Compare local artwork by its library identity; never request a cover over HTTP."""

from __future__ import annotations

import json
import os
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from pullbox.services.migration_asset_files import (
    AssetVerificationError,
    open_directory,
    regular_evidence,
)
from pullbox.services.provider_artwork import issue_cover_stem

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

    from pullbox.services.migration_assets import AssetReport
    from pullbox.services.migration_media import MediaMounts

_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
type Directory = tuple[int, tuple[str, ...]]


def _names(directory: Directory | None) -> dict[str, list[str]]:
    if directory is None:
        return {}
    found: dict[str, list[str]] = {}
    try:
        with open_directory(*directory) as fd, os.scandir(fd) as entries:
            for item in entries:
                stem, ext = os.path.splitext(item.name)
                if ext in _EXTENSIONS and (
                    stem in {"cover", "folder", "series", "story_arc"} or stem.startswith("issue_")
                ):
                    found.setdefault(stem, []).append(item.name)
    except FileNotFoundError:
        return {}
    return found


def _local_marker(value: str | None, kind: str, ident: int) -> bool:
    if value is not None and not isinstance(value, str):
        raise AssetVerificationError("cover_reference_unknown")
    if not value or value.startswith(("https://", "http://")):
        return False
    if value.partition("?")[0] != f"/api/v1/{kind}/{ident}/cover":
        raise AssetVerificationError("cover_reference_unknown")
    return True


def _record(
    directory: Directory | None,
    names: dict[str, list[str]],
    stem: str,
    identity: str,
    report: AssetReport,
    add: Callable[[bytes], None],
) -> bool:
    if directory is None:
        return False
    fd, parts = directory
    for name in sorted(names.get(stem, ())):
        evidence = regular_evidence(fd, (*parts, name))
        if not evidence["size"]:
            raise AssetVerificationError("cover_empty")
        add(json.dumps([identity, name, evidence["sha256"]]).encode() + b"\n")
        report.covers_checked += 1
    return bool(names.get(stem))


def _series_dir(mounts: MediaMounts, root_id: int | None, path: str | None) -> Directory | None:
    if not path:
        return None
    parent = PurePosixPath(path)
    if root_id is None:
        matches = [k for k, r in mounts.roots.items() if parent.is_relative_to(r)]
        if not matches:
            raise AssetVerificationError("cover_folder_outside_roots")
        root_id = max(matches, key=lambda k: len(PurePosixPath(mounts.roots[k]).parts))
    # Appending a constant leaf also supports covers directly in a library root.
    fd, parts, _ = mounts.locate(root_id, str(parent / "cover.jpg"))
    return fd, parts[:-1]


def verify_covers(
    db: sqlite3.Connection,
    mounts: MediaMounts,
    report: AssetReport,
    add: Callable[[bytes], None],
    cache: Path | None,
    legacy: Path | None,
) -> None:
    if legacy and not cache:
        raise AssetVerificationError("effective_covers_dir_required")
    try:
        cache_fd = mounts.open(cache) if cache is not None else None
        legacy_fd = mounts.open(legacy) if legacy is not None else None
    except OSError:
        raise AssetVerificationError("covers_directory_unavailable") from None
    if (
        cache_fd is not None
        and legacy_fd is not None
        and os.path.samestat(os.fstat(cache_fd), os.fstat(legacy_fd))
    ):
        raise AssetVerificationError("duplicate_cover_mapping")
    if cache_fd is not None:
        report.cover_cache_mapped = True
        add(f"cover-cache-v1:legacy={legacy is not None}\n".encode())
    for sid, root_id, path, marker in db.execute(
        "SELECT id,library_root_id,path,cover_path FROM series ORDER BY id"
    ):
        try:
            folder = _series_dir(mounts, root_id, path)
            cache_dir = (cache_fd, (str(sid),)) if cache_fd is not None else None
            old_dir = (legacy_fd, (str(sid),)) if legacy_fd is not None else None
            dirs = (folder, cache_dir, old_dir)
            names = [_names(d) for d in dirs]
            required = _local_marker(marker, "series", sid)
            available = [
                _record(d, n, stem, f"series:{sid}:{i}", report, add)
                for i, (d, n, stem) in enumerate(
                    zip(dirs, names, ("cover", "series", "series"), strict=True)
                )
            ]
            _record(folder, names[0], "folder", f"series-folder:{sid}", report, add)
            if required and not any(available):
                report.media_block("series_cover_missing", sid)
            for iid, number, exact, saved in db.execute(
                "SELECT id,issue_number,issue_number_text,cover_path FROM issues "
                "WHERE series_id=? ORDER BY id",
                (sid,),
            ):
                try:
                    required = _local_marker(saved, "issues", iid)
                    stem = issue_cover_stem(number, exact)
                    available = [
                        _record(d, n, s, f"issue:{iid}:{i}", report, add)
                        for i, (d, n, s) in enumerate(
                            zip(dirs, names, (stem, stem, f"issue_{iid}"), strict=True)
                        )
                    ]
                    if required and not any(available):
                        report.media_block("issue_cover_missing", iid)
                except (AssetVerificationError, OSError, ValueError) as exc:
                    report.media_block(
                        str(exc)
                        if isinstance(exc, AssetVerificationError)
                        else "issue_cover_unreadable",
                        iid,
                    )
        except (AssetVerificationError, OSError, ValueError) as exc:
            report.media_block(
                str(exc) if isinstance(exc, AssetVerificationError) else "series_cover_unreadable",
                sid,
            )
    for aid, marker in db.execute("SELECT id,cover_path FROM story_arcs ORDER BY id"):
        try:
            required = _local_marker(marker, "story-arcs", aid)
            directory = (cache_fd, ("story_arcs", str(aid))) if cache_fd is not None else None
            if (
                not _record(
                    directory, _names(directory), "story_arc", f"arc-cover:{aid}", report, add
                )
                and required
            ):
                report.media_block("arc_cover_missing", aid)
        except (AssetVerificationError, OSError, ValueError) as exc:
            report.media_block(
                str(exc) if isinstance(exc, AssetVerificationError) else "arc_cover_unreadable", aid
            )

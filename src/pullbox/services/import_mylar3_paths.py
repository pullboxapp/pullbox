"""Mylar3 path mapping helpers for collection import."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

import structlog


class DebugLogger(Protocol):
    """Small logging protocol used to preserve import-service log routing."""

    def debug(self, event: str, **kwargs: object) -> object: ...


logger = structlog.get_logger(__name__)


def auto_detect_mylar3_path_map(
    db_path: Path,
    *,
    log: DebugLogger = logger,
) -> dict[str, str] | None:
    """Auto-detect path mapping for Mylar3 by examining ComicLocation values."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT ComicLocation FROM comics "
            "WHERE ComicLocation IS NOT NULL AND ComicLocation != '' "
            "ORDER BY ComicLocation "
            "LIMIT 50"
        ).fetchall()
        conn.close()
    except sqlite3.DatabaseError:
        return None

    if not rows:
        return None

    proposals: dict[str, str] = {}
    proposal_locations: dict[str, str] = {}
    for row in rows:
        comic_location: str = row[0]
        if _identity_location_is_available(comic_location):
            continue
        detected = _detect_path_map_for_location(db_path, comic_location)
        if detected is None:
            continue
        container_prefix, host_path = detected
        existing_host_path = proposals.get(container_prefix)
        if existing_host_path is not None and not _same_path(
            Path(existing_host_path), Path(host_path)
        ):
            log.debug(
                "mylar3_path_map_auto_detection_ambiguous",
                container_prefix=container_prefix,
                first_host_path=existing_host_path,
                second_host_path=host_path,
            )
            return None
        proposals[container_prefix] = host_path
        proposal_locations.setdefault(container_prefix, comic_location)

    if _has_conflicting_overlapping_mappings(proposals):
        log.debug("mylar3_path_map_auto_detection_overlapping")
        return None

    for container_prefix, host_path in sorted(proposals.items()):
        log.debug(
            "mylar3_path_map_auto_detected",
            container_prefix=container_prefix,
            host_path=host_path,
            comic_location=proposal_locations[container_prefix],
        )
    return proposals or None


def _identity_location_is_available(comic_location: str) -> bool:
    """Return whether Mylar's stored directory is already usable unchanged."""
    location_path = Path(comic_location)
    if not location_path.is_absolute() or ".." in location_path.parts:
        return False
    try:
        return location_path.resolve(strict=True).is_dir()
    except (OSError, RuntimeError, ValueError):
        return False


def _has_conflicting_overlapping_mappings(path_map: dict[str, str]) -> bool:
    """Reject nested stored prefixes whose translated roots disagree."""
    mappings = [(Path(source), Path(target)) for source, target in path_map.items()]
    for index, (left_source, left_target) in enumerate(mappings):
        for right_source, right_target in mappings[index + 1 :]:
            if _nested_mapping_conflicts(
                left_source,
                left_target,
                right_source,
                right_target,
            ) or _nested_mapping_conflicts(
                right_source,
                right_target,
                left_source,
                left_target,
            ):
                return True
    return False


def _nested_mapping_conflicts(
    parent_source: Path,
    parent_target: Path,
    child_source: Path,
    child_target: Path,
) -> bool:
    try:
        relative = child_source.relative_to(parent_source)
    except ValueError:
        return False
    if not relative.parts:
        return not _same_path(parent_target, child_target)
    return not _same_path(parent_target / relative, child_target)


def _detect_path_map_for_location(
    db_path: Path,
    comic_location: str,
) -> tuple[str, str] | None:
    """Return a validated path map for one Mylar ComicLocation."""
    location_path = Path(comic_location)
    if not location_path.is_absolute():
        return None

    for container_prefix in _container_prefixes(location_path):
        search_name = Path(container_prefix).name
        relative_location = location_path.relative_to(container_prefix)
        candidates: dict[str, Path] = {}
        for search_dir in _path_map_search_dirs(db_path):
            candidate_root = search_dir / search_name
            if not candidate_root.is_dir():
                continue
            if _same_path(candidate_root, Path(container_prefix)):
                continue
            translated_location = candidate_root / relative_location
            if not translated_location.is_dir():
                continue
            candidates[str(candidate_root.resolve(strict=False))] = candidate_root
        if len(candidates) == 1:
            candidate_root = next(iter(candidates.values()))
            return container_prefix, str(candidate_root)
        if len(candidates) > 1:
            return None
    return None


def _container_prefixes(location_path: Path) -> list[str]:
    """Return possible Mylar container prefixes, deepest useful prefix first."""
    parts = location_path.parts
    if len(parts) < 3 or parts[0] != "/":
        return []

    segments = parts[1:]
    return [str(Path("/", *segments[:index])) for index in range(len(segments) - 1, 0, -1)]


def _path_map_search_dirs(db_path: Path) -> list[Path]:
    """Return nearby directories to probe for host-side mount names."""
    search_dirs = [
        db_path.parent,
        db_path.parent.parent,
        db_path.parent.parent.parent,
    ]
    unique_dirs: list[Path] = []
    for search_dir in search_dirs:
        if search_dir not in unique_dirs:
            unique_dirs.append(search_dir)
    return unique_dirs


def _same_path(left: Path, right: Path) -> bool:
    """Return whether two paths refer to the same path without requiring existence."""
    return left.resolve(strict=False) == right.resolve(strict=False)

"""Validation helpers for frozen Mylar path-mapping snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pullbox.core.filesystem_policy import is_invalid_path_text

if TYPE_CHECKING:
    from collections.abc import Iterable

MAX_MYLAR3_PATH_MAPPINGS = 16


def normalize_mylar3_path_map(path_map: dict[str, str]) -> dict[str, str]:
    """Return a bounded canonical map or reject unsafe/ambiguous entries."""
    return normalize_mylar3_path_mapping_items(path_map.items())


def normalize_mylar3_path_mapping_items(
    mapping_items: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """Normalize ordered editor rows without silently collapsing duplicates."""
    items = list(mapping_items)
    if len(items) > MAX_MYLAR3_PATH_MAPPINGS:
        raise ValueError(f"Mylar path mapping supports at most {MAX_MYLAR3_PATH_MAPPINGS} entries.")

    normalized: dict[str, str] = {}
    for stored_prefix, visible_prefix in items:
        normalized_source = _normalize_mapping_path(stored_prefix, role="stored")
        normalized_target = _normalize_mapping_path(visible_prefix, role="Pullbox-visible")
        if normalized_source in normalized:
            raise ValueError("Mylar path mapping contains a duplicate stored prefix.")
        if normalized_source == normalized_target:
            raise ValueError(
                "Mylar path mapping must not contain an identity entry; remove that mapping."
            )
        normalized[normalized_source] = normalized_target

    if has_conflicting_overlapping_mappings(normalized):
        raise ValueError(
            "Mylar path mapping contains overlapping entries that resolve to different paths."
        )
    return normalized


def has_conflicting_overlapping_mappings(path_map: dict[str, str]) -> bool:
    """Return whether nested stored prefixes translate inconsistently."""
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


def ordered_mylar3_path_map_items(path_map: dict[str, str]) -> list[tuple[str, str]]:
    """Return mappings in deterministic longest-complete-prefix order."""
    return sorted(
        path_map.items(),
        key=lambda item: (-len(Path(item[0]).parts), item[0], item[1]),
    )


def _normalize_mapping_path(value: str, *, role: str) -> str:
    if not isinstance(value, str) or is_invalid_path_text(value):
        raise ValueError(f"Mylar path mapping {role} prefix is invalid.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise ValueError(
            f"Mylar path mapping {role} prefix must be a safe absolute directory path."
        )
    return str(path)


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
        return parent_target != child_target
    return parent_target / relative != child_target

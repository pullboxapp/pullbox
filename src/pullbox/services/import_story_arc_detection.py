"""Conservative, provider-free story-arc detection for folder imports."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from pullbox.core.story_arc_identity import normalize_story_arc_name


class FolderArcClassification(enum.StrEnum):
    """Folder-import classification without mutating or matching the library."""

    NORMAL = "normal"
    NORMAL_MIXED_FOLDER = "normal_mixed_folder"
    STORY_ARC = "story_arc"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class FolderArcFileEvidence:
    """Sanitized identity/order evidence for one file in a candidate folder."""

    relative_path: str
    series: str | None
    issue_number: str | None
    story_arc: str | None = None
    story_arc_number: str | None = None
    story_arc_number_source: str | None = None
    evidence_complete: bool = True


@dataclass(frozen=True, slots=True)
class FolderArcDetection:
    """Pure classification result used by import preview and review."""

    classification: FolderArcClassification
    reason: str
    proposed_name: str | None
    file_count: int
    series_count: int
    ordered_file_count: int
    provider_calls_required: bool = False


def detect_folder_story_arc(
    *,
    folder_label: str,
    files: tuple[FolderArcFileEvidence, ...],
    confirmed_order_pattern: bool = False,
) -> FolderArcDetection:
    """Classify bounded folder evidence without title fuzzing or provider calls."""
    series_keys = {
        item.series.strip().casefold()
        for item in files
        if item.series is not None and item.series.strip()
    }
    ordered = [
        item
        for item in files
        if item.story_arc_number is not None and item.story_arc_number.strip()
    ]
    named = [item for item in files if item.story_arc is not None and item.story_arc.strip()]

    normalized_names: dict[str, str] = {}
    for item in named:
        assert item.story_arc is not None
        display_name = _collapse_whitespace(item.story_arc)
        normalized_names.setdefault(normalize_story_arc_name(display_name), display_name)

    if any(not item.evidence_complete for item in files) and (
        len(series_keys) > 1 or bool(named) or bool(ordered)
    ):
        return _result(
            FolderArcClassification.NEEDS_REVIEW,
            "incomplete_arc_evidence",
            next(iter(normalized_names.values()), _clean_folder_label(folder_label)),
            files,
            series_keys,
            ordered,
        )

    if len(normalized_names) > 1:
        return _result(
            FolderArcClassification.NEEDS_REVIEW,
            "conflicting_exact_arc_names",
            None,
            files,
            series_keys,
            ordered,
        )

    order_keys = [_order_key(item.story_arc_number or "") for item in ordered]
    if len(order_keys) != len(set(order_keys)):
        return _result(
            FolderArcClassification.NEEDS_REVIEW,
            "duplicate_arc_order",
            next(iter(normalized_names.values()), _clean_folder_label(folder_label)),
            files,
            series_keys,
            ordered,
        )

    if normalized_names:
        return _result(
            FolderArcClassification.STORY_ARC,
            "consistent_exact_arc_name",
            next(iter(normalized_names.values())),
            files,
            series_keys,
            ordered,
        )

    ordered_mixed = len(series_keys) > 1 and len(ordered) == len(files) and bool(files)
    if ordered_mixed and confirmed_order_pattern:
        return _result(
            FolderArcClassification.STORY_ARC,
            "confirmed_ordered_mixed_folder",
            _clean_folder_label(folder_label),
            files,
            series_keys,
            ordered,
        )
    if ordered_mixed:
        return _result(
            FolderArcClassification.NEEDS_REVIEW,
            "ordered_mixed_folder_requires_confirmation",
            _clean_folder_label(folder_label),
            files,
            series_keys,
            ordered,
        )
    if len(series_keys) > 1:
        return _result(
            FolderArcClassification.NORMAL_MIXED_FOLDER,
            "mixed_series_without_strong_arc_evidence",
            None,
            files,
            series_keys,
            ordered,
        )
    return _result(
        FolderArcClassification.NORMAL,
        "no_strong_arc_evidence",
        None,
        files,
        series_keys,
        ordered,
    )


def _result(
    classification: FolderArcClassification,
    reason: str,
    proposed_name: str | None,
    files: tuple[FolderArcFileEvidence, ...],
    series_keys: set[str],
    ordered: list[FolderArcFileEvidence],
) -> FolderArcDetection:
    return FolderArcDetection(
        classification=classification,
        reason=reason,
        proposed_name=proposed_name,
        file_count=len(files),
        series_count=len(series_keys),
        ordered_file_count=len(ordered),
    )


def _clean_folder_label(value: str) -> str:
    cleaned = _collapse_whitespace(value)
    return cleaned or "Story Arc"


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _order_key(value: str) -> tuple[str, str]:
    normalized = value.strip()
    try:
        decimal = Decimal(normalized)
    except InvalidOperation:
        return ("text", normalized.casefold())
    if not decimal.is_finite():
        return ("text", normalized.casefold())
    return ("number", format(decimal.normalize(), "f"))

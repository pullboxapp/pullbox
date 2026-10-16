"""Step 4 active file progress calculations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_PDF_STAGE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("rendering", 0.55),
    ("encoding", 0.2),
    ("packing", 0.1),
)
_ARCHIVE_STAGE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("extracting", 0.6),
    ("packing", 0.2),
)
_TRANSFER_STAGE_WEIGHT = 0.15
_REWRITE_STAGE_WEIGHT = 0.1
_FINALIZE_STAGE_WEIGHT = 0.05


class ImportFileProgressLike(Protocol):
    file_path: str


@dataclass(frozen=True, slots=True)
class ActiveFileProgressSettings:
    move_to_library: bool
    convert_to_preferred_format: bool
    update_embedded_comicinfo_from_match: bool


def calculate_import_file_progress_pct(
    *,
    move_to_library: bool,
    convert_to_preferred_format: bool,
    update_embedded_comicinfo_from_match: bool,
    imp_file: ImportFileProgressLike,
    stage: str,
    current: int,
    total: int,
) -> int:
    """Public helper for truthful file-stage progress outside Step 4 job runs."""
    settings = ActiveFileProgressSettings(
        move_to_library=move_to_library,
        convert_to_preferred_format=convert_to_preferred_format,
        update_embedded_comicinfo_from_match=update_embedded_comicinfo_from_match,
    )
    return active_file_progress_pct(settings, imp_file, stage, current, total)


def active_file_progress_pct(
    settings: ActiveFileProgressSettings,
    imp_file: ImportFileProgressLike,
    stage: str,
    current: int,
    total: int,
) -> int:
    if stage == "preparing":
        return 0

    stage_plan = active_file_stage_plan(settings, imp_file)
    if not stage_plan:
        return _normalized_stage_pct(current, total)

    normalized_weights = _normalize_stage_plan(stage_plan)
    interleaved_pdf_pct = _interleaved_pdf_conversion_pct(
        settings,
        imp_file,
        normalized_weights,
        stage=stage,
        current=current,
        total=total,
    )
    if interleaved_pdf_pct is not None:
        return interleaved_pdf_pct

    completed_weight = 0.0
    stage_fraction = _normalized_stage_fraction(current, total)

    for stage_name, weight in normalized_weights:
        if stage_name == stage:
            completed_weight += weight * stage_fraction
            return max(0, min(round(completed_weight * 100), 100))
        completed_weight += weight

    return _normalized_stage_pct(current, total)


def active_file_stage_plan(
    settings: ActiveFileProgressSettings,
    imp_file: ImportFileProgressLike,
) -> list[tuple[str, float]]:
    plan: list[tuple[str, float]] = []
    suffix = Path(imp_file.file_path).suffix.lower()
    nested_repair = bool((getattr(imp_file, "diagnostics", None) or {}).get("nested_repair"))
    needs_conversion = nested_repair or (
        settings.move_to_library
        and (settings.convert_to_preferred_format or settings.update_embedded_comicinfo_from_match)
        and suffix != ".cbz"
    )

    if needs_conversion:
        if suffix == ".pdf":
            plan.extend(_PDF_STAGE_WEIGHTS)
        else:
            plan.extend(_ARCHIVE_STAGE_WEIGHTS)
    if settings.move_to_library:
        plan.append(("transferring", _TRANSFER_STAGE_WEIGHT))
    if settings.update_embedded_comicinfo_from_match and not nested_repair:
        plan.append(("rewriting", _REWRITE_STAGE_WEIGHT))
    plan.append(("finalizing", _FINALIZE_STAGE_WEIGHT))
    return plan


def _normalize_stage_plan(stage_plan: list[tuple[str, float]]) -> list[tuple[str, float]]:
    total_weight = sum(weight for _stage, weight in stage_plan)
    if total_weight <= 0:
        return []
    return [(stage, weight / total_weight) for stage, weight in stage_plan]


def _interleaved_pdf_conversion_pct(
    settings: ActiveFileProgressSettings,
    imp_file: ImportFileProgressLike,
    normalized_weights: list[tuple[str, float]],
    *,
    stage: str,
    current: int,
    total: int,
) -> int | None:
    if Path(imp_file.file_path).suffix.lower() != ".pdf":
        return None
    if stage not in {"rendering", "encoding"}:
        return None
    if total <= 0:
        return None
    if not settings.move_to_library:
        return None
    if not (settings.convert_to_preferred_format or settings.update_embedded_comicinfo_from_match):
        return None

    completed_weight = 0.0
    rendering_weight = 0.0
    encoding_weight = 0.0
    found_rendering = False

    for stage_name, weight in normalized_weights:
        if stage_name == "rendering":
            rendering_weight = weight
            found_rendering = True
            continue
        if stage_name == "encoding":
            encoding_weight = weight
            break
        if not found_rendering:
            completed_weight += weight

    conversion_weight = rendering_weight + encoding_weight
    if conversion_weight <= 0:
        return None

    current_page = max(0, min(current, total))
    completed_pages = max(current_page - 1, 0)
    rendered_page_fraction = rendering_weight / conversion_weight

    if stage == "rendering":
        conversion_fraction = (completed_pages + rendered_page_fraction) / total
    else:
        conversion_fraction = current_page / total

    progress = completed_weight + (conversion_weight * conversion_fraction)
    return max(0, min(round(progress * 100), 100))


def _normalized_stage_fraction(current: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, min(current / total, 1.0))


def _normalized_stage_pct(current: int, total: int) -> int:
    return round(_normalized_stage_fraction(current, total) * 100)

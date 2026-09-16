"""Read-only presentation of saved import outcomes."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from pullbox.core.name_matcher import NameMatcher
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.services.import_file_conflicts import review_filename_identity
from pullbox.services.import_safety_diagnostics import normalize_import_safety_diagnostics

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


LANES = {
    "decide": "Needs a decision",
    "confirm": "Review suggestions",
    "fix_source": "Fix source",
    "blocked": "Cannot import",
    "ready": "Ready",
    "info": "Info",
}

# The same labels can describe a saved outcome in review and in Follow-up.
REASONS = {
    "needs_series": ("No series matched", "Find series", "decide"),
    "series_conflict": ("Choose between series matches", "Choose series", "decide"),
    "source_layout_review": ("Files need a layout decision", "Review layout", "decide"),
    "needs_issue": ("Some files need an issue match", "Match issues", "decide"),
    "same_comic_review": ("Files disagree about the comic", "Review files", "decide"),
    "single_page_comic": ("One-page archive", "Review file", "decide"),
    "decompression_size_limit": ("Large file needs approval", "Review file", "decide"),
    "preparing_match": ("Updating the match", "Updating...", "decide"),
    "duplicate_copy_confirm": ("Choose which copy to import", "Review copies", "confirm"),
    "permission_unreadable": ("File cannot be read", "View details", "fix_source"),
    "archive_inspection_failed": ("Archive could not be inspected", "View details", "fix_source"),
    "source_changed": ("Source changed after scanning", "View details", "fix_source"),
    "outside_approved_root": ("Library access needs attention", "View details", "fix_source"),
    "zero_byte": ("File is empty", "View details", "fix_source"),
    "archive_no_pages": ("Archive has no comic pages", "View details", "fix_source"),
    "failed": ("File needs another inspection", "View details", "fix_source"),
    "dangerous_path_or_payload": ("Unsafe archive content", "View details", "blocked"),
    "unsupported_file_type": ("Unsupported file type", "View details", "blocked"),
    "unknown": ("Safety could not be established", "View details", "blocked"),
    "source_missing": ("Recorded file not found", "View details", "info"),
    "already_handled": ("Already handled", "View details", "info"),
    "ready": ("Ready to import", "View files", "ready"),
}


@dataclass(frozen=True, slots=True)
class ReviewFacts:
    status: str
    known_target: bool = False
    kind: str = ""
    pending: bool = False
    ready_files: int = 0
    unmatched_files: int = 0
    conflict_files: int = 0
    approved_files: int = 0
    failed_files: int = 0
    identity_conflict: bool = False
    safety_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReviewRow:
    lane: str
    reasons: tuple[str, ...]
    ready_files: int
    attention_files: int
    updating: bool = False

    @property
    def label(self) -> str:
        return REASONS[self.reasons[0]][0]

    @property
    def action(self) -> str:
        return "Updating..." if self.updating else REASONS[self.reasons[0]][1]


def classify_review_row(facts: ReviewFacts) -> ReviewRow:
    """Derive a display lane without changing persisted outcomes."""
    reasons: list[str] = []
    if facts.status in {"skipped", "imported"}:
        return ReviewRow("info", ("already_handled",), 0, 0)
    if facts.kind in {"series_conflict", "source_layout_review"}:
        reasons.append(facts.kind)
    elif (
        (facts.status == "no_match" and not facts.safety_counts and not facts.approved_files)
        or facts.unmatched_files
        or (facts.approved_files and not facts.pending)
    ):
        reasons.append("needs_issue" if facts.known_target else "needs_series")
    if facts.conflict_files:
        reasons.append("same_comic_review" if facts.identity_conflict else "duplicate_copy_confirm")
    reasons.extend(
        category if category in REASONS else "unknown" for category in facts.safety_counts
    )
    if facts.kind == "mylar3_path_incompatible" and not facts.safety_counts:
        reasons.append("outside_approved_root")
    if facts.failed_files or facts.status == "failed":
        reasons.append("failed")
    if facts.pending:
        reasons.append("preparing_match")
    if not reasons:
        reasons.append("ready" if facts.ready_files else "already_handled")
    order = list(LANES)
    reasons = sorted(dict.fromkeys(reasons), key=lambda reason: order.index(REASONS[reason][2]))
    # Missing references can accompany useful files without making the files wait.
    lane = REASONS[reasons[0]][2]
    if lane == "info" and facts.ready_files:
        lane = "ready"
        reasons.insert(0, "ready")
    return ReviewRow(
        lane,
        tuple(reasons),
        facts.ready_files,
        facts.unmatched_files
        + facts.approved_files
        + facts.conflict_files
        + facts.failed_files
        + sum(facts.safety_counts.values()),
        facts.pending,
    )


async def load_review_rows(session: AsyncSession, job_id: int) -> dict[int, ReviewRow]:
    """Project compact saved facts; never load archives or change matching state."""
    status_counts: dict[int, Counter[str]] = defaultdict(Counter)
    safety_counts: dict[int, Counter[str]] = defaultdict(Counter)
    identity_conflicts = await _identity_conflict_series_ids(session, job_id)
    block = ImportedFile.diagnostics["safety_block"]
    category = block["category"].as_string()
    code = block["code"].as_string()
    reason = block["reason"].as_string()
    conflict_class = ImportedFile.diagnostics["conflict_class"].as_string()
    groups = await session.execute(
        select(
            ImportedFile.import_series_id,
            ImportedFile.status,
            category,
            code,
            reason,
            conflict_class,
            func.count(),
        )
        .where(ImportedFile.import_job_id == job_id)
        .group_by(
            ImportedFile.import_series_id,
            ImportedFile.status,
            category,
            code,
            reason,
            conflict_class,
        )
    )
    for series_id, status, raw_category, raw_code, raw_reason, group_class, count in groups:
        status_counts[series_id][status.value] += count
        if status.value == "safety_blocked":
            normalized = normalize_import_safety_diagnostics(
                {"category": raw_category, "code": raw_code, "reason": raw_reason or raw_code or ""}
            )
            safety_counts[series_id][str(normalized["category"])] += count
        if status.value == "conflict" and group_class in {"series_mismatch", "year_disagreement"}:
            identity_conflicts.add(series_id)
    series_rows = await session.execute(
        select(
            ImportedSeries.id,
            ImportedSeries.status,
            ImportedSeries.cv_id,
            ImportedSeries.user_selected_cv_id,
            ImportedSeries.series_id,
            ImportedSeries.diagnostics["kind"].as_string(),
            ImportedSeries.diagnostics["rematch_pending"].as_boolean(),
        ).where(ImportedSeries.import_job_id == job_id)
    )
    rows: dict[int, ReviewRow] = {}
    for series_id, status, cv_id, chosen_id, library_id, kind, pending in series_rows:
        counts = status_counts[series_id]
        rows[series_id] = classify_review_row(
            ReviewFacts(
                status=status.value,
                known_target=bool(cv_id or chosen_id or library_id),
                kind=kind or "",
                pending=bool(pending),
                ready_files=counts["matched"] + counts["confirmed"],
                unmatched_files=counts["no_match"],
                conflict_files=counts["conflict"],
                approved_files=counts["safety_approved"],
                failed_files=counts["failed"],
                identity_conflict=series_id in identity_conflicts,
                safety_counts=dict(safety_counts[series_id]),
            )
        )
    return rows


async def _identity_conflict_series_ids(session: AsyncSession, job_id: int) -> set[int]:
    """Older jobs have no conflict class; interpret saved fields without rewriting them."""
    groups: dict[int, tuple[set[int], set[str], set[str], set[int], set[str | None]]] = {}
    records = await session.execute(
        select(
            ImportedFile.conflict_group_id,
            ImportedFile.import_series_id,
            ImportedFile.parsed_series,
            ImportedFile.parsed_year,
            ImportedFile.content_hash,
            ImportedFile.file_name,
        )
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.status == ImportedFileStatus.CONFLICT,
            ImportedFile.conflict_group_id.is_not(None),
        )
        .distinct()
    )
    for group_id, series_id, title, year, content_hash, file_name in records:
        series, titles, saved_titles, years, hashes = groups.setdefault(
            group_id, (set(), set(), set(), set(), set())
        )
        series.add(series_id)
        if title:
            saved_titles.add(NameMatcher.normalize(title))
        file_title, file_year = review_filename_identity(file_name, title, year)
        if file_title:
            titles.add(file_title)
        if file_year:
            years.add(file_year)
        hashes.add(content_hash)
    result: set[int] = set()
    for series, titles, saved_titles, years, hashes in groups.values():
        identical = len(hashes) == 1 and None not in hashes and "" not in hashes
        if not identical and (len(titles) > 1 or len(saved_titles) > 1 or len(years) > 1):
            result.update(series)
    return result

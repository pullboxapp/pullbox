"""Bounded, read-only source layout analysis for collection imports."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from pullbox.core.collection_scanner import COMIC_EXTENSIONS, IGNORE_DIRS
from pullbox.core.issue_numbers import format_issue_number
from pullbox.core.library_layout import (
    CompiledSourceLayout,
    ImportLayoutMode,
    LayoutClassification,
    LayoutValueError,
    SourceLayoutMatch,
    SourceLayoutSpec,
    compile_source_layout,
    resolve_source_layout_spec,
)
from pullbox.core.source_metadata import SourceMetadataExtractor

_GENERIC_OR_TYPE_CONTAINERS = frozenset(
    {
        "annual",
        "annuals",
        "books",
        "collection",
        "collections",
        "comics",
        "downloads",
        "imports",
        "incoming",
        "library",
        "media",
        "special",
        "specials",
        "staging",
        "volume",
        "volumes",
    }
)


@dataclass(frozen=True, slots=True)
class LayoutAnalysisBudget:
    """Server-owned hard limits for one preflight analysis."""

    max_directories: int = 2_000
    max_files: int = 5_000
    max_examples_per_cluster: int = 3
    deadline_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.max_directories < 1:
            raise ValueError("max_directories must be positive")
        if self.max_files < 1:
            raise ValueError("max_files must be positive")
        if self.max_examples_per_cluster < 1:
            raise ValueError("max_examples_per_cluster must be positive")
        if self.deadline_seconds < 0:
            raise ValueError("deadline_seconds cannot be negative")


@dataclass(frozen=True, slots=True)
class LayoutExample:
    """One sanitized root-relative example from a detected cluster."""

    relative_path: str
    publisher: str | None
    series: str | None
    year: int | None
    issue_number: str | None
    issue_title: str | None
    evidence: list[str]
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class LayoutClusterSummary:
    """Bounded summary of paths sharing one interpreted layout."""

    cluster_id: str
    classification: LayoutClassification
    file_count: int
    directory_count: int
    confidence: str
    proposed_series_path_template: str | None
    proposed_issue_filename_template: str | None
    examples: list[LayoutExample]


@dataclass(frozen=True, slots=True)
class LayoutAnalysisResult:
    """Read-only preflight result; not durable job state."""

    effective_spec: SourceLayoutSpec
    classification: LayoutClassification
    clusters: list[LayoutClusterSummary]
    directories_considered: int
    files_considered: int
    files_fitting: int
    files_ambiguous: int
    files_outside_root: int
    archive_probes: int
    can_keep_in_place: bool
    can_apply_future_policy: bool
    partial: bool
    warnings: list[str]


@dataclass(slots=True)
class _ClusterAccumulator:
    classification: LayoutClassification
    confidence: str
    proposed_series_path_template: str | None
    proposed_issue_filename_template: str | None
    file_count: int = 0
    directories: set[str] = field(default_factory=set)
    examples: list[LayoutExample] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _AnalyzedPath:
    cluster_key: str
    classification: LayoutClassification
    confidence: str
    match: SourceLayoutMatch | None
    proposed_series_path_template: str | None
    proposed_issue_filename_template: str | None
    evidence: list[str]
    warnings: list[str]


class ImportLayoutAnalyzer:
    """Analyze comic paths without provider calls, archive probes, or writes."""

    def __init__(self, *, extensions: frozenset[str] | None = None) -> None:
        self._extensions = extensions or COMIC_EXTENSIONS
        self._metadata_extractor = SourceMetadataExtractor()
        self._series_matcher = compile_source_layout(
            SourceLayoutSpec(mode=ImportLayoutMode.PRESET, preset="series_folders")
        )
        self._publisher_matcher = compile_source_layout(
            SourceLayoutSpec(mode=ImportLayoutMode.PRESET, preset="publisher_series")
        )

    async def analyze(
        self,
        root_path: str | Path,
        *,
        spec: SourceLayoutSpec | None = None,
        budget: LayoutAnalysisBudget | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> LayoutAnalysisResult:
        """Return a deterministic, bounded source-layout analysis."""
        root = Path(root_path).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Layout analysis root must be a directory")

        effective_spec = resolve_source_layout_spec(spec or SourceLayoutSpec())
        selected_matcher = (
            compile_source_layout(effective_spec)
            if effective_spec.mode != ImportLayoutMode.AUTO
            else None
        )
        limits = budget or LayoutAnalysisBudget()
        deadline = time.monotonic() + limits.deadline_seconds
        warnings: list[str] = []
        clusters: dict[str, _ClusterAccumulator] = {}
        directories_considered = 0
        files_considered = 0
        files_fitting = 0
        files_ambiguous = 0
        files_outside_root = 0
        partial = False

        self._raise_if_cancelled(cancel_event)
        if self._deadline_reached(deadline):
            return self._empty_partial_result(effective_spec, "deadline_reached")

        walk_errors: list[str] = []

        def on_walk_error(_error: OSError) -> None:
            walk_errors.append("unreadable_path_skipped")

        for current_root, dir_names, file_names in os.walk(
            root,
            topdown=True,
            onerror=on_walk_error,
            followlinks=False,
        ):
            self._raise_if_cancelled(cancel_event)
            if self._deadline_reached(deadline):
                partial = True
                _append_once(warnings, "deadline_reached")
                break
            if directories_considered >= limits.max_directories:
                partial = True
                _append_once(warnings, "directory_limit_reached")
                break

            current = Path(current_root)
            if not _is_within_root(current, root):
                dir_names.clear()
                continue
            safe_dir_names: list[str] = []
            for name in sorted(dir_names):
                if name in IGNORE_DIRS or name.startswith("."):
                    continue
                candidate = current / name
                if candidate.is_symlink():
                    partial = True
                    _append_once(warnings, "symlink_directory_skipped")
                    continue
                safe_dir_names.append(name)
            dir_names[:] = safe_dir_names
            file_names.sort()
            directories_considered += 1

            for file_name in file_names:
                self._raise_if_cancelled(cancel_event)
                if self._deadline_reached(deadline):
                    partial = True
                    _append_once(warnings, "deadline_reached")
                    break
                path = current / file_name
                if path.suffix.lower() not in self._extensions or file_name.startswith("._"):
                    continue
                if files_considered >= limits.max_files:
                    partial = True
                    _append_once(warnings, "file_limit_reached")
                    break
                if not _is_within_root(path, root):
                    files_outside_root += 1
                    files_ambiguous += 1
                    _append_once(warnings, "outside_root_skipped")
                    continue

                relative_path = path.relative_to(root).as_posix()
                files_considered += 1
                analyzed = self._analyze_path(
                    relative_path,
                    effective_spec=effective_spec,
                    selected_matcher=selected_matcher,
                )
                if analyzed.classification == LayoutClassification.NEEDS_REVIEW:
                    files_ambiguous += 1
                else:
                    files_fitting += 1
                self._accumulate(
                    clusters,
                    analyzed,
                    relative_path=relative_path,
                    max_examples=limits.max_examples_per_cluster,
                )
                if files_considered % 64 == 0:
                    await asyncio.sleep(0)

            if partial:
                break

        for warning in walk_errors:
            _append_once(warnings, warning)
        if walk_errors:
            partial = True

        summaries = self._summaries(clusters)
        classification = _overall_classification(summaries)
        can_keep_in_place = files_outside_root == 0 and not walk_errors
        can_apply_future_policy = bool(
            not partial
            and files_ambiguous == 0
            and len(summaries) == 1
            and classification == LayoutClassification.NORMAL_LIBRARY
            and summaries[0].proposed_series_path_template is not None
            and (
                effective_spec.mode == ImportLayoutMode.AUTO
                or summaries[0].proposed_series_path_template == effective_spec.series_path_template
            )
        )
        return LayoutAnalysisResult(
            effective_spec=effective_spec,
            classification=classification,
            clusters=summaries,
            directories_considered=directories_considered,
            files_considered=files_considered,
            files_fitting=files_fitting,
            files_ambiguous=files_ambiguous,
            files_outside_root=files_outside_root,
            archive_probes=0,
            can_keep_in_place=can_keep_in_place,
            can_apply_future_policy=can_apply_future_policy,
            partial=partial,
            warnings=warnings,
        )

    def _analyze_path(
        self,
        relative_path: str,
        *,
        effective_spec: SourceLayoutSpec,
        selected_matcher: CompiledSourceLayout | None,
    ) -> _AnalyzedPath:
        if selected_matcher is not None:
            try:
                matched = selected_matcher.match(relative_path)
            except LayoutValueError:
                matched = None
            if matched is not None:
                return _AnalyzedPath(
                    cluster_key=f"selected:{effective_spec.series_path_template}",
                    classification=LayoutClassification.NORMAL_LIBRARY,
                    confidence="high",
                    match=matched,
                    proposed_series_path_template=effective_spec.series_path_template,
                    proposed_issue_filename_template=effective_spec.issue_filename_template,
                    evidence=["selected_layout_match"],
                    warnings=[],
                )
            if not effective_spec.fallback_to_auto:
                return self._needs_review_path(relative_path, "selected_layout_no_match")

        return self._auto_analyze_path(relative_path)

    def _auto_analyze_path(self, relative_path: str) -> _AnalyzedPath:
        parts = PurePosixPath(relative_path).parts
        depth = len(parts) - 1
        if depth == 1:
            matcher = self._series_matcher
            key = "auto:series_folders"
            confidence = "high"
            template = "{Series}"
        elif depth == 2:
            possible_publisher = parts[0].strip().casefold()
            possible_series = parts[1].strip().casefold()
            if (
                possible_publisher in _GENERIC_OR_TYPE_CONTAINERS
                or possible_series in _GENERIC_OR_TYPE_CONTAINERS
                or possible_publisher.isdigit()
            ):
                return self._needs_review_path(
                    relative_path,
                    "generic_or_type_container_requires_review",
                )
            matcher = self._publisher_matcher
            key = "auto:publisher_series"
            confidence = "high"
            template = "{Publisher}/{Series}"
        elif depth == 0:
            metadata = self._metadata_extractor.from_release_title(
                PurePosixPath(relative_path).name
            )
            if metadata.series_name and metadata.issue_number is not None:
                match = SourceLayoutMatch(
                    relative_path=relative_path,
                    series=metadata.series_name,
                    year=metadata.year,
                    issue_number=format_issue_number(metadata.issue_number),
                )
                return _AnalyzedPath(
                    cluster_key="auto:loose_files",
                    classification=LayoutClassification.NORMAL_LIBRARY,
                    confidence="low",
                    match=match,
                    proposed_series_path_template=None,
                    proposed_issue_filename_template="{Series} {Issue}",
                    evidence=["loose_filename_identity"],
                    warnings=["loose_files_require_review"],
                )
            return self._needs_review_path(relative_path, "loose_file_without_strong_identity")
        else:
            return self._needs_review_path(relative_path, "unrecognized_directory_depth")

        try:
            matched = matcher.match(relative_path)
        except LayoutValueError:
            matched = None
        if matched is None:
            return self._needs_review_path(relative_path, "registered_layout_no_match")
        return _AnalyzedPath(
            cluster_key=key,
            classification=LayoutClassification.NORMAL_LIBRARY,
            confidence=confidence,
            match=matched,
            proposed_series_path_template=template,
            proposed_issue_filename_template=None,
            evidence=[key.replace(":", "_")],
            warnings=[],
        )

    def _needs_review_path(self, relative_path: str, reason: str) -> _AnalyzedPath:
        metadata = self._metadata_extractor.from_release_title(PurePosixPath(relative_path).name)
        match = SourceLayoutMatch(
            relative_path=relative_path,
            series=metadata.series_name,
            year=metadata.year,
            issue_number=(
                format_issue_number(metadata.issue_number)
                if metadata.issue_number is not None
                else None
            ),
        )
        return _AnalyzedPath(
            cluster_key=f"needs_review:{reason}",
            classification=LayoutClassification.NEEDS_REVIEW,
            confidence="low",
            match=match,
            proposed_series_path_template=None,
            proposed_issue_filename_template=None,
            evidence=[],
            warnings=[reason],
        )

    @staticmethod
    def _accumulate(
        clusters: dict[str, _ClusterAccumulator],
        analyzed: _AnalyzedPath,
        *,
        relative_path: str,
        max_examples: int,
    ) -> None:
        accumulator = clusters.setdefault(
            analyzed.cluster_key,
            _ClusterAccumulator(
                classification=analyzed.classification,
                confidence=analyzed.confidence,
                proposed_series_path_template=analyzed.proposed_series_path_template,
                proposed_issue_filename_template=analyzed.proposed_issue_filename_template,
            ),
        )
        accumulator.file_count += 1
        accumulator.directories.add(PurePosixPath(relative_path).parent.as_posix())
        if len(accumulator.examples) >= max_examples:
            return
        matched = analyzed.match
        accumulator.examples.append(
            LayoutExample(
                relative_path=relative_path,
                publisher=matched.publisher if matched is not None else None,
                series=matched.series if matched is not None else None,
                year=matched.year if matched is not None else None,
                issue_number=matched.issue_number if matched is not None else None,
                issue_title=matched.issue_title if matched is not None else None,
                evidence=list(analyzed.evidence),
                warnings=list(analyzed.warnings),
            )
        )

    @staticmethod
    def _summaries(clusters: dict[str, _ClusterAccumulator]) -> list[LayoutClusterSummary]:
        summaries: list[LayoutClusterSummary] = []
        for key in sorted(clusters):
            cluster = clusters[key]
            cluster_id = hashlib.sha256(key.encode()).hexdigest()[:12]
            summaries.append(
                LayoutClusterSummary(
                    cluster_id=cluster_id,
                    classification=cluster.classification,
                    file_count=cluster.file_count,
                    directory_count=len(cluster.directories),
                    confidence=cluster.confidence,
                    proposed_series_path_template=cluster.proposed_series_path_template,
                    proposed_issue_filename_template=cluster.proposed_issue_filename_template,
                    examples=list(cluster.examples),
                )
            )
        return summaries

    @staticmethod
    def _raise_if_cancelled(cancel_event: asyncio.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError

    @staticmethod
    def _deadline_reached(deadline: float) -> bool:
        return time.monotonic() >= deadline

    @staticmethod
    def _empty_partial_result(
        spec: SourceLayoutSpec,
        warning: str,
    ) -> LayoutAnalysisResult:
        return LayoutAnalysisResult(
            effective_spec=spec,
            classification=LayoutClassification.NEEDS_REVIEW,
            clusters=[],
            directories_considered=0,
            files_considered=0,
            files_fitting=0,
            files_ambiguous=0,
            files_outside_root=0,
            archive_probes=0,
            can_keep_in_place=True,
            can_apply_future_policy=False,
            partial=True,
            warnings=[warning],
        )


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return resolved == root or resolved.is_relative_to(root)


def _overall_classification(
    clusters: list[LayoutClusterSummary],
) -> LayoutClassification:
    if not clusters:
        return LayoutClassification.NEEDS_REVIEW
    if len(clusters) > 1:
        return LayoutClassification.MIXED
    return clusters[0].classification


def _append_once(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)

"""Bounded, read-only Story Arc evidence analysis for Import Step 1."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pullbox.core.collection_scanner import COMIC_EXTENSIONS, IGNORE_DIRS
from pullbox.core.filesystem_policy import is_sensitive_path, resolve_preview_source
from pullbox.core.mylar3_reader import Mylar3Reader
from pullbox.core.mylar_story_arc_policy import build_mylar_story_arc_policy_draft
from pullbox.core.naming import parse_filename
from pullbox.core.story_arc_naming import (
    DEFAULT_STORY_ARC_FILE_TEMPLATE,
    DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
)
from pullbox.core.story_arc_ordering import extract_story_arc_order_prefix
from pullbox.models.import_job import ImportSourceType
from pullbox.schemas.import_story_arc_preflight import (
    StoryArcEvidenceExample,
    StoryArcPolicyPreview,
    StoryArcPreflightResponse,
    StoryArcResolutionPreview,
    StoryArcSettingPreview,
)


@dataclass(frozen=True, slots=True)
class StoryArcPreflightBudget:
    """Server-owned path limits for a Step 1 folder sample."""

    max_directories: int = 2_000
    max_files: int = 5_000
    max_examples: int = 5
    deadline_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class _FolderCandidate:
    folder: str
    examples: tuple[StoryArcEvidenceExample, ...]
    entry_count: int
    duplicate_count: int


class StoryArcPreflightAnalyzer:
    """Expose source evidence without creating jobs, calling providers, or writing files."""

    async def analyze(
        self,
        source_path: str | Path,
        *,
        source_type: ImportSourceType,
        budget: StoryArcPreflightBudget | None = None,
    ) -> StoryArcPreflightResponse:
        """Return a typed Mylar or folder preflight response."""
        path = resolve_preview_source(source_path)
        if source_type is ImportSourceType.MYLAR3:
            database = path / "mylar.db" if path.is_dir() else path
            # Validate the target without relocating the selected folder's config.ini.
            resolve_preview_source(database)
            return await self._analyze_mylar(database)
        if not path.is_dir():
            raise ValueError("Filesystem Story Arc analysis requires a directory")
        return await self._analyze_folder(path, budget or StoryArcPreflightBudget())

    async def _analyze_mylar(self, database: Path) -> StoryArcPreflightResponse:
        reader = Mylar3Reader(database)
        snapshot = await reader.read_story_arc_preflight()
        draft = build_mylar_story_arc_policy_draft(snapshot.arc_settings)
        placement_value = draft.get("placement_policy")
        placement = (
            cast("dict[str, object]", placement_value) if isinstance(placement_value, dict) else {}
        )
        configured_settings = tuple(
            StoryArcSettingPreview(
                key=setting.key,
                value=self._safe_setting_value(setting.key, setting.value),
                used_default=setting.used_default,
            )
            for setting in snapshot.arc_settings.values
            if not setting.used_default
        )
        examples = [StoryArcEvidenceExample.model_validate(item) for item in snapshot.examples]
        missing = min(snapshot.missing_count, snapshot.entries_count)
        evidence_detected = snapshot.arcs_count > 0
        warnings = list(snapshot.warnings)
        review_warnings = draft.get("review_warnings")
        if isinstance(review_warnings, list):
            warnings.extend(str(item) for item in review_warnings if item)
        return StoryArcPreflightResponse(
            source_type=ImportSourceType.MYLAR3,
            evidence_detected=evidence_detected,
            arcs_detected=snapshot.arcs_count,
            entries_detected=snapshot.entries_count,
            resolution=StoryArcResolutionPreview(
                pending=max(snapshot.entries_count - missing, 0),
                missing=missing,
                duplicates=snapshot.duplicate_count,
            ),
            existing_arc_files_detected=snapshot.existing_location_count > 0,
            existing_arc_folders_detected=snapshot.existing_location_count > 0,
            pattern_summary=(
                "Mylar Story Arc rows and saved ordering" if evidence_detected else None
            ),
            settings=list(configured_settings),
            examples=examples,
            provider_calls_required=False,
            provider_call_summary="No provider calls are needed for trusted Mylar data.",
            proposed_policy=StoryArcPolicyPreview(
                mode=str(placement.get("mode") or "logical"),
                destination_root_configured=bool(placement.get("destination_root")),
                folder_template=str(
                    placement.get("folder_template") or DEFAULT_STORY_ARC_FOLDER_TEMPLATE
                ),
                file_template=str(
                    placement.get("file_template") or DEFAULT_STORY_ARC_FILE_TEMPLATE
                ),
                reading_order_prefix="{ReadingOrder" in str(placement.get("file_template") or ""),
                synchronize=bool(placement.get("synchronize")),
            ),
            readlist_present=snapshot.readlist_present,
            readlist_count=snapshot.readlist_count,
            readlist_import_state=("deferred_v1.5.0" if snapshot.readlist_present else None),
            warnings=self._unique(warnings),
        )

    async def _analyze_folder(
        self,
        root: Path,
        budget: StoryArcPreflightBudget,
    ) -> StoryArcPreflightResponse:
        if (
            budget.max_directories < 1
            or budget.max_files < 1
            or budget.max_examples < 1
            or budget.deadline_seconds < 0
        ):
            raise ValueError("Story Arc preflight budget values are invalid")
        candidates, partial, warnings = await asyncio.to_thread(
            self._scan_folder_candidates,
            root,
            budget,
        )
        entries = sum(candidate.entry_count for candidate in candidates)
        duplicates = sum(candidate.duplicate_count for candidate in candidates)
        examples = [example for candidate in candidates for example in candidate.examples][
            : budget.max_examples
        ]
        detected = bool(candidates)
        if detected:
            warnings.append("full_scan_may_find_additional_comicinfo_evidence")
        return StoryArcPreflightResponse(
            source_type=ImportSourceType.FILESYSTEM,
            evidence_detected=detected,
            arcs_detected=len(candidates),
            entries_detected=entries,
            resolution=StoryArcResolutionPreview(
                pending=entries,
                duplicates=duplicates,
            ),
            existing_arc_files_detected=detected,
            existing_arc_folders_detected=detected,
            pattern_summary=("Reading-order prefixes across multiple series" if detected else None),
            examples=examples,
            provider_calls_required=True,
            provider_call_summary=(
                "The normal matching workflow may use providers after local evidence is exhausted."
            ),
            proposed_policy=StoryArcPolicyPreview(
                mode="reference_only" if detected else "logical",
                destination_root_configured=detected,
                folder_template=DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
                file_template=DEFAULT_STORY_ARC_FILE_TEMPLATE,
                reading_order_prefix=True,
                synchronize=False,
            ),
            archive_probes=0,
            partial=partial or detected,
            warnings=self._unique(warnings),
        )

    def _scan_folder_candidates(
        self,
        root: Path,
        budget: StoryArcPreflightBudget,
    ) -> tuple[list[_FolderCandidate], bool, list[str]]:
        deadline = time.monotonic() + budget.deadline_seconds
        directories = 0
        files = 0
        partial = False
        warnings: list[str] = []
        groups: dict[str, list[StoryArcEvidenceExample]] = {}
        orders: dict[str, list[int]] = {}
        series: dict[str, set[str]] = {}
        comic_counts: dict[str, int] = {}

        def on_error(_error: OSError) -> None:
            nonlocal partial
            partial = True
            warnings.append("unreadable_path_skipped")

        for current_root, dir_names, file_names in os.walk(
            root,
            topdown=True,
            onerror=on_error,
            followlinks=False,
        ):
            if time.monotonic() >= deadline:
                partial = True
                warnings.append("deadline_reached")
                break
            if directories >= budget.max_directories:
                partial = True
                warnings.append("directory_limit_reached")
                break
            current = Path(current_root)
            if is_sensitive_path(current):
                dir_names.clear()
                partial = True
                warnings.append("sensitive_directory_skipped")
                continue
            if any(is_sensitive_path(current / name) for name in dir_names):
                partial = True
                warnings.append("sensitive_directory_skipped")
            dir_names[:] = [
                name
                for name in sorted(dir_names)
                if name not in IGNORE_DIRS
                and not name.startswith(".")
                and not (current / name).is_symlink()
                and not is_sensitive_path(current / name)
            ]
            directories += 1
            relative_folder = current.relative_to(root).as_posix()
            for file_name in sorted(file_names):
                path = current / file_name
                if path.is_symlink() or path.suffix.lower() not in COMIC_EXTENSIONS:
                    continue
                if files >= budget.max_files:
                    partial = True
                    warnings.append("file_limit_reached")
                    break
                files += 1
                comic_counts[relative_folder] = comic_counts.get(relative_folder, 0) + 1
                prefix = extract_story_arc_order_prefix(file_name)
                if prefix is None:
                    continue
                parsed = parse_filename(prefix.residual_file_name)
                parsed_series = parsed.series if parsed is not None else None
                relative_path = path.relative_to(root).as_posix()
                groups.setdefault(relative_folder, []).append(
                    StoryArcEvidenceExample(
                        story_arc=current.name,
                        series=parsed_series,
                        issue_number=(
                            str(int(parsed.issue_number))
                            if parsed is not None and parsed.issue_number.is_integer()
                            else str(parsed.issue_number)
                            if parsed is not None
                            else None
                        ),
                        issue_title=None,
                        reading_order=prefix.reading_order_raw,
                        status=None,
                        relative_path=relative_path,
                    )
                )
                orders.setdefault(relative_folder, []).append(prefix.reading_order)
                if parsed_series:
                    series.setdefault(relative_folder, set()).add(parsed_series.casefold())
            if partial and "file_limit_reached" in warnings:
                break

        candidates: list[_FolderCandidate] = []
        for folder in sorted(groups):
            folder_examples = groups[folder]
            if (
                len(folder_examples) < 2
                or len(folder_examples) != comic_counts.get(folder, 0)
                or len(series.get(folder, set())) < 2
            ):
                continue
            folder_orders = orders.get(folder, [])
            candidates.append(
                _FolderCandidate(
                    folder=folder,
                    examples=tuple(folder_examples[: budget.max_examples]),
                    entry_count=len(folder_examples),
                    duplicate_count=len(folder_orders) - len(set(folder_orders)),
                )
            )
        return candidates, partial, self._unique(warnings)

    def _safe_setting_value(self, key: str, value: bool | str | None) -> bool | str | None:
        if key == "STORYARC_LOCATION":
            return "Configured" if isinstance(value, str) and value.strip() else "Not configured"
        if isinstance(value, str):
            return " ".join(value.split())[:200]
        return value

    def _unique(self, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

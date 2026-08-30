"""Persist review-only story-arc evidence without providers or source I/O."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, cast

from sqlalchemy import select

from pullbox.core.mylar_story_arc_policy import build_mylar_story_arc_policy_draft
from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services.import_folder_story_arc_evidence import (
    _evidence_from_imported_file,
    detect_imported_folder_story_arc,
)
from pullbox.services.import_story_arc_detection import FolderArcClassification

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.mylar3_reader import (
        Mylar3ArcSettingsSnapshot,
        Mylar3CollectionSnapshot,
        Mylar3StoryArcEntrySnapshot,
        Mylar3StoryArcSnapshot,
    )
    from pullbox.services.import_story_arc_detection import FolderArcFileEvidence


_SAFE_CODE = re.compile(r"[^A-Za-z0-9_.:-]+")
_MISSING_MYLAR_STATUSES = frozenset({"missing", "wanted"})
_SKIPPED_MYLAR_STATUSES = frozenset({"skipped", "skip"})
_REVIEW_LOCKED_ARC_STATUSES = frozenset(
    {
        ImportedStoryArcStatus.CONFIRMED,
        ImportedStoryArcStatus.SKIPPED,
        ImportedStoryArcStatus.IMPORTED,
    }
)


@dataclass(frozen=True, slots=True)
class StoryArcStagingResult:
    """Bounded staging summary suitable for progress/review integration."""

    arcs_staged: int = 0
    entries_staged: int = 0
    needs_review: int = 0
    cohorts_examined: int = 0
    cohorts_skipped: int = 0
    readlist_present: bool = False
    readlist_count: int = 0


CancellationCheck = Callable[[], Awaitable[None]]


async def stage_mylar_story_arcs(
    session: AsyncSession,
    *,
    import_job_id: int,
    snapshot: Mylar3CollectionSnapshot,
    batch_size: int = 100,
    cancellation_check: CancellationCheck | None = None,
) -> StoryArcStagingResult:
    """Stage a pre-read Mylar collection snapshot without source or provider I/O.

    The caller owns the transaction. This function flushes at bounded checkpoints
    but never commits, so a cancellation can still roll back the complete stage.
    """
    _require_positive_batch_size(batch_size)
    await _checkpoint(cancellation_check)

    settings_snapshot = _mylar_settings_snapshot(snapshot)
    arcs_staged = 0
    entries_staged = 0
    needs_review = 0

    arcs = tuple(snapshot.story_arcs)
    seen_source_keys: set[str] = set()
    for start in range(0, len(arcs), batch_size):
        await _checkpoint(cancellation_check)
        source_arcs = tuple(
            (
                source_ordinal,
                source_arc,
                _mylar_source_key(source_arc),
            )
            for source_ordinal, source_arc in enumerate(
                arcs[start : start + batch_size],
                start=start + 1,
            )
        )
        page_source_keys = [source_key for _, _, source_key in source_arcs]
        duplicate_source_keys = seen_source_keys.intersection(page_source_keys)
        if len(set(page_source_keys)) != len(page_source_keys) or duplicate_source_keys:
            msg = "Mylar story-arc staging requires unique source identities."
            raise ValueError(msg)
        seen_source_keys.update(page_source_keys)

        existing_arcs = list(
            (
                await session.execute(
                    select(ImportedStoryArc).where(
                        ImportedStoryArc.import_job_id == import_job_id,
                        ImportedStoryArc.source_key.in_(page_source_keys),
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_arc_by_source_key = {arc.source_key: arc for arc in existing_arcs}
        existing_entries_by_arc_id: dict[int, dict[int, ImportedStoryArcEntry]] = {}
        existing_arc_ids = [arc.id for arc in existing_arcs]
        if existing_arc_ids:
            existing_entries = list(
                (
                    await session.execute(
                        select(ImportedStoryArcEntry).where(
                            ImportedStoryArcEntry.imported_story_arc_id.in_(existing_arc_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for entry in existing_entries:
                existing_entries_by_arc_id.setdefault(entry.imported_story_arc_id, {})[
                    entry.source_ordinal
                ] = entry

        for source_ordinal, source_arc, source_key in source_arcs:
            entry_payloads = tuple(_mylar_entry_payload(entry) for entry in source_arc.entries)
            status = _mylar_arc_status(source_arc, entry_payloads, snapshot.arc_settings)
            existing_arc = existing_arc_by_source_key.get(source_key)
            staged_entry_count = await _upsert_staged_arc(
                session,
                import_job_id=import_job_id,
                source_kind=StoryArcSourceKind.MYLAR3,
                source_key=source_key,
                source_arc_id=_bounded_identifier(source_arc.story_arc_id, 255),
                source_ordinal=source_ordinal,
                name=_bounded_text(source_arc.name, 500),
                description=None,
                status=status,
                proposed_policy_snapshot=build_mylar_story_arc_policy_draft(snapshot.arc_settings),
                source_settings_snapshot=settings_snapshot,
                diagnostics=_mylar_arc_diagnostics(
                    snapshot=snapshot,
                    source_arc=source_arc,
                    entries=entry_payloads,
                ),
                entry_payloads=entry_payloads,
                existing_staged_arc=existing_arc,
                lookup_existing=False,
                existing_entries=(
                    existing_entries_by_arc_id.get(existing_arc.id, {})
                    if existing_arc is not None
                    else {}
                ),
            )
            arcs_staged += 1
            entries_staged += staged_entry_count
            needs_review += status == ImportedStoryArcStatus.NEEDS_REVIEW
        await session.flush()

    return StoryArcStagingResult(
        arcs_staged=arcs_staged,
        entries_staged=entries_staged,
        needs_review=needs_review,
        readlist_present=bool(snapshot.readlist_present),
        readlist_count=max(int(snapshot.readlist_count), 0),
    )


async def stage_folder_story_arcs(
    session: AsyncSession,
    *,
    import_job_id: int,
    cohort_batch_size: int = 100,
    confirmed_cohort_keys: Collection[str] = (),
    cancellation_check: CancellationCheck | None = None,
) -> StoryArcStagingResult:
    """Stage complete folder cohorts from cached ImportedFile evidence only.

    Cohort keys are keyset-paginated. Every selected key is then loaded as one
    complete cohort, including rows split across several ImportedSeries records.
    """
    _require_positive_batch_size(cohort_batch_size)
    await _checkpoint(cancellation_check)

    confirmed = frozenset(confirmed_cohort_keys)
    last_key: str | None = None
    arcs_staged = 0
    entries_staged = 0
    needs_review = 0
    cohorts_examined = 0
    cohorts_skipped = 0

    while True:
        key_query = (
            select(ImportedFile.source_folder_cohort_key)
            .where(
                ImportedFile.import_job_id == import_job_id,
                ImportedFile.source_folder_cohort_key.is_not(None),
            )
            .distinct()
            .order_by(ImportedFile.source_folder_cohort_key)
            .limit(cohort_batch_size)
        )
        if last_key is not None:
            key_query = key_query.where(ImportedFile.source_folder_cohort_key > last_key)
        keys = [
            key
            for key in (await session.execute(key_query)).scalars().all()
            if isinstance(key, str) and key
        ]
        if not keys:
            break

        await _checkpoint(cancellation_check)
        files_by_cohort: dict[str, list[ImportedFile]] = {key: [] for key in keys}
        cohort_files = list(
            (
                await session.execute(
                    select(ImportedFile)
                    .where(
                        ImportedFile.import_job_id == import_job_id,
                        ImportedFile.source_folder_cohort_key.in_(keys),
                    )
                    .order_by(
                        ImportedFile.source_folder_cohort_key,
                        ImportedFile.source_ordinal.is_(None),
                        ImportedFile.source_ordinal,
                        ImportedFile.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for item in cohort_files:
            cohort_key = item.source_folder_cohort_key
            if cohort_key is not None:
                files_by_cohort.setdefault(cohort_key, []).append(item)

        source_key_by_cohort = {key: _folder_source_key(key) for key in keys}
        existing_arcs = list(
            (
                await session.execute(
                    select(ImportedStoryArc).where(
                        ImportedStoryArc.import_job_id == import_job_id,
                        ImportedStoryArc.source_key.in_(source_key_by_cohort.values()),
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_arc_by_source_key = {arc.source_key: arc for arc in existing_arcs}
        existing_entries_by_arc_id: dict[int, dict[int, ImportedStoryArcEntry]] = {}
        existing_arc_ids = [arc.id for arc in existing_arcs]
        if existing_arc_ids:
            existing_entries = list(
                (
                    await session.execute(
                        select(ImportedStoryArcEntry).where(
                            ImportedStoryArcEntry.imported_story_arc_id.in_(existing_arc_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for entry in existing_entries:
                existing_entries_by_arc_id.setdefault(entry.imported_story_arc_id, {})[
                    entry.source_ordinal
                ] = entry

        for cohort_key in keys:
            files = files_by_cohort.get(cohort_key, [])
            cohorts_examined += 1
            if not files:
                cohorts_skipped += 1
                continue

            detection = detect_imported_folder_story_arc(
                folder_label=_folder_label(cohort_key),
                files=files,
                confirmed_order_pattern=cohort_key in confirmed,
            )
            source_key = source_key_by_cohort[cohort_key]
            existing_arc = existing_arc_by_source_key.get(source_key)
            if detection.classification not in {
                FolderArcClassification.STORY_ARC,
                FolderArcClassification.NEEDS_REVIEW,
            }:
                await _remove_obsolete_unconfirmed_arc(
                    session,
                    import_job_id=import_job_id,
                    source_key=source_key,
                    existing_staged_arc=existing_arc,
                    lookup_existing=False,
                )
                cohorts_skipped += 1
                continue

            evidence = tuple(_evidence_from_imported_file(item) for item in files)
            entry_payloads = tuple(
                _folder_entry_payload(item, item_evidence, source_ordinal=source_ordinal)
                for source_ordinal, (item, item_evidence) in enumerate(
                    zip(files, evidence, strict=True),
                    start=1,
                )
            )
            status = (
                ImportedStoryArcStatus.NEEDS_REVIEW
                if detection.classification == FolderArcClassification.NEEDS_REVIEW
                else ImportedStoryArcStatus.DETECTED
            )
            staged_entry_count = await _upsert_staged_arc(
                session,
                import_job_id=import_job_id,
                source_kind=StoryArcSourceKind.FOLDER,
                source_key=source_key,
                source_arc_id=None,
                source_ordinal=cohorts_examined,
                name=_bounded_text(detection.proposed_name, 500),
                description=None,
                status=status,
                proposed_policy_snapshot={
                    "schema_version": 1,
                    "source": "folder",
                    "activation": "requires_confirmation",
                },
                source_settings_snapshot={},
                diagnostics={
                    "schema_version": 1,
                    "classification": detection.classification.value,
                    "reason": _safe_code(detection.reason),
                    "file_count": len(files),
                    "series_count": int(detection.series_count),
                    "ordered_file_count": int(detection.ordered_file_count),
                    "cohort_key_digest": _digest_text(cohort_key),
                    "safety_incomplete": any(not item.evidence_complete for item in evidence),
                },
                entry_payloads=entry_payloads,
                existing_staged_arc=existing_arc,
                lookup_existing=False,
                existing_entries=(
                    existing_entries_by_arc_id.get(existing_arc.id, {})
                    if existing_arc is not None
                    else {}
                ),
            )
            arcs_staged += 1
            entries_staged += staged_entry_count
            needs_review += status == ImportedStoryArcStatus.NEEDS_REVIEW

        await session.flush()
        last_key = keys[-1]

    return StoryArcStagingResult(
        arcs_staged=arcs_staged,
        entries_staged=entries_staged,
        needs_review=needs_review,
        cohorts_examined=cohorts_examined,
        cohorts_skipped=cohorts_skipped,
    )


async def _upsert_staged_arc(
    session: AsyncSession,
    *,
    import_job_id: int,
    source_kind: StoryArcSourceKind,
    source_key: str,
    source_arc_id: str | None,
    source_ordinal: int,
    name: str | None,
    description: str | None,
    status: ImportedStoryArcStatus,
    proposed_policy_snapshot: dict[str, object],
    source_settings_snapshot: dict[str, object],
    diagnostics: dict[str, object],
    entry_payloads: Sequence[dict[str, object]],
    existing_staged_arc: ImportedStoryArc | None = None,
    lookup_existing: bool = True,
    existing_entries: Mapping[int, ImportedStoryArcEntry] | None = None,
) -> int:
    staged_arc = existing_staged_arc
    if lookup_existing:
        staged_arc = (
            await session.execute(
                select(ImportedStoryArc).where(
                    ImportedStoryArc.import_job_id == import_job_id,
                    ImportedStoryArc.source_key == source_key,
                )
            )
        ).scalar_one_or_none()
    if staged_arc is None:
        review_locked = False
        staged_arc = ImportedStoryArc(
            import_job_id=import_job_id,
            source_kind=source_kind,
            source_key=source_key,
            source_arc_id=source_arc_id,
            source_ordinal=source_ordinal,
            name=name,
            description=description,
            status=status,
            selected_for_import=False,
            proposed_policy_snapshot=proposed_policy_snapshot,
            source_settings_snapshot=source_settings_snapshot,
            diagnostics=diagnostics,
        )
        session.add(staged_arc)
    else:
        review_locked = staged_arc.status in _REVIEW_LOCKED_ARC_STATUSES
        staged_arc.source_kind = source_kind
        staged_arc.source_arc_id = source_arc_id
        staged_arc.source_ordinal = source_ordinal
        staged_arc.name = name
        staged_arc.description = description
        if not review_locked:
            staged_arc.status = status
        staged_arc.proposed_policy_snapshot = proposed_policy_snapshot
        staged_arc.source_settings_snapshot = source_settings_snapshot
        staged_arc.diagnostics = diagnostics

    if existing_entries is None:
        existing_entries = (
            {
                entry.source_ordinal: entry
                for entry in (
                    (
                        await session.execute(
                            select(ImportedStoryArcEntry).where(
                                ImportedStoryArcEntry.imported_story_arc_id == staged_arc.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            }
            if staged_arc.id is not None
            else {}
        )
    seen_ordinals: set[int] = set()
    for payload in entry_payloads:
        source_entry_ordinal = cast("int", payload["source_ordinal"])
        if source_entry_ordinal in seen_ordinals:
            msg = "Story-arc staging requires unique source ordinals."
            raise ValueError(msg)
        seen_ordinals.add(source_entry_ordinal)
        entry = existing_entries.get(source_entry_ordinal)
        if entry is None:
            entry = ImportedStoryArcEntry(
                imported_story_arc=staged_arc,
                **payload,
            )
            session.add(entry)
            continue
        for attribute, value in payload.items():
            if attribute in {"selected_for_import", "materialized_membership_id"}:
                continue
            if review_locked and attribute in {
                "matched_issue_id",
                "resolution_state",
                "resolution_confidence",
                "resolution_method",
            }:
                continue
            setattr(entry, attribute, value)

    for source_entry_ordinal, entry in existing_entries.items():
        if source_entry_ordinal not in seen_ordinals and not review_locked:
            await session.delete(entry)

    return len(entry_payloads)


async def _remove_obsolete_unconfirmed_arc(
    session: AsyncSession,
    *,
    import_job_id: int,
    source_key: str,
    existing_staged_arc: ImportedStoryArc | None = None,
    lookup_existing: bool = True,
) -> None:
    staged_arc = existing_staged_arc
    if lookup_existing:
        staged_arc = (
            await session.execute(
                select(ImportedStoryArc).where(
                    ImportedStoryArc.import_job_id == import_job_id,
                    ImportedStoryArc.source_key == source_key,
                )
            )
        ).scalar_one_or_none()
    if staged_arc is None:
        return
    if staged_arc.materialized_story_arc_id is not None:
        return
    if staged_arc.status in {
        ImportedStoryArcStatus.CONFIRMED,
        ImportedStoryArcStatus.SKIPPED,
        ImportedStoryArcStatus.IMPORTED,
    }:
        return
    await session.delete(staged_arc)


def _mylar_entry_payload(entry: Mylar3StoryArcEntrySnapshot) -> dict[str, object]:
    exact_issue_number = _bounded_exact_text(entry.issue_number, 320)
    exact_reading_order = _bounded_exact_text(entry.reading_order_raw, 50)
    source_location = _bounded_source_location(entry.location)
    resolution_state = _mylar_resolution_state(entry)
    diagnostics: dict[str, object] = {
        "schema_version": 1,
        "review_reason": _mylar_review_reason(resolution_state),
        "source_location_present": entry.location is not None,
        "source_location_omitted": entry.location is not None and source_location is None,
        "exact_issue_number_omitted": entry.issue_number is not None and exact_issue_number is None,
        "exact_reading_order_omitted": entry.reading_order_raw is not None
        and exact_reading_order is None,
    }
    source_publisher = entry.issue_publisher or entry.publisher
    return {
        "import_file_id": None,
        "matched_issue_id": None,
        "materialized_membership_id": None,
        "source_ordinal": int(entry.ordinal),
        "reading_order": entry.reading_order,
        "reading_order_raw": exact_reading_order,
        "resolution_state": resolution_state,
        "source_kind": StoryArcSourceKind.MYLAR3,
        "source_entry_id": _bounded_identifier(entry.issue_arc_id, 255),
        "source_arc_id": _bounded_identifier(entry.story_arc_id, 255),
        "source_issue_id": _bounded_identifier(entry.issue_id, 255),
        "source_series_id": _bounded_identifier(entry.comic_id, 255),
        "source_issue_number_text": exact_issue_number,
        "source_series_name": _bounded_text(entry.comic_name, 500),
        "source_issue_title": _bounded_text(entry.issue_name, 500),
        "source_publisher": _bounded_text(source_publisher, 255),
        "source_release_date_text": _bounded_text(entry.release_date, 50),
        "source_issue_date_text": _bounded_text(entry.issue_date, 50),
        "resolution_confidence": None,
        "resolution_method": "trusted_mylar_snapshot",
        "evidence": {
            "schema_version": 1,
            "series_year": _bounded_text(entry.series_year, 50),
            "issue_year": _bounded_text(entry.issue_year, 50),
            "status": _bounded_text(entry.status, 100),
            "manual": _bounded_text(entry.manual, 100),
            "date_added": _bounded_text(entry.date_added, 50),
            "digital_date": _bounded_text(entry.digital_date, 50),
            "issue_type": _bounded_text(entry.issue_type, 100),
            "aliases": _bounded_text(entry.aliases, 1000),
            "total_issues": _bounded_text(entry.total_issues, 50),
            "in_cache_dir": _bounded_text(entry.in_cache_dir, 50),
            "int_issue_number": _bounded_text(entry.int_issue_number, 320),
            "dynamic_comic_name": _bounded_text(entry.dynamic_comic_name, 500),
            "volume": _bounded_text(entry.volume, 100),
            "cv_arc_id": _bounded_identifier(entry.cv_arc_id, 255),
            "has_arc_image": entry.arc_image is not None,
            "has_source_location": entry.location is not None,
        },
        "source_location": source_location,
        "selected_for_import": False,
        "diagnostics": diagnostics,
    }


def _folder_entry_payload(
    item: ImportedFile,
    evidence: FolderArcFileEvidence,
    *,
    source_ordinal: int,
) -> dict[str, object]:
    comicinfo = _cached_comicinfo(item)
    safety_code = _folder_safety_code(item)
    resolution_state = _folder_resolution_state(item, evidence)
    issue_identity = item.comicvine_issue_id or item.matched_issue_cv_id
    series_identity = _mapping(item.diagnostics).get("comicvine_series_id")
    source_location = _bounded_source_location(item.file_path)
    return {
        "import_file_id": int(item.id),
        "matched_issue_id": item.matched_issue_id,
        "materialized_membership_id": None,
        "source_ordinal": source_ordinal,
        "reading_order": _parse_integral_order(evidence.story_arc_number),
        "reading_order_raw": _bounded_exact_text(evidence.story_arc_number, 50),
        "resolution_state": resolution_state,
        "source_kind": StoryArcSourceKind.FOLDER,
        "source_entry_id": f"import-file:{int(item.id)}",
        "source_arc_id": None,
        "source_issue_id": _bounded_identifier(issue_identity, 255),
        "source_series_id": _bounded_identifier(series_identity, 255),
        "source_issue_number_text": _bounded_exact_text(evidence.issue_number, 320),
        "source_series_name": _bounded_text(evidence.series, 500),
        "source_issue_title": _bounded_text(comicinfo.get("title"), 500),
        "source_publisher": _bounded_text(comicinfo.get("publisher"), 255),
        "source_release_date_text": None,
        "source_issue_date_text": None,
        "resolution_confidence": None,
        "resolution_method": _bounded_text(item.match_method, 50),
        "evidence": {
            "schema_version": 1,
            "story_arc_number_source": _safe_code(evidence.story_arc_number_source),
            "original_source_ordinal": item.source_ordinal,
            "has_comicinfo": bool(item.has_comicinfo),
            "matched_issue_from_import_job": item.matched_issue_id is not None,
            "has_source_location": bool(item.file_path),
        },
        "source_location": source_location,
        "selected_for_import": False,
        "diagnostics": {
            "schema_version": 1,
            "review_reason": (
                "safety_incomplete"
                if not evidence.evidence_complete
                else _folder_review_reason(resolution_state)
            ),
            "safety_code": safety_code,
            "source_location_omitted": bool(item.file_path) and source_location is None,
        },
    }


def _mylar_resolution_state(entry: Mylar3StoryArcEntrySnapshot) -> StoryArcResolutionState:
    status = (entry.status or "").strip().casefold()
    if status in _SKIPPED_MYLAR_STATUSES:
        return StoryArcResolutionState.SKIPPED
    if status in _MISSING_MYLAR_STATUSES or entry.location is None:
        return StoryArcResolutionState.MISSING
    return StoryArcResolutionState.PENDING


def _folder_resolution_state(
    item: ImportedFile,
    evidence: FolderArcFileEvidence,
) -> StoryArcResolutionState:
    if item.status == ImportedFileStatus.CONFLICT:
        return StoryArcResolutionState.CONFLICT
    if not evidence.evidence_complete:
        return StoryArcResolutionState.AMBIGUOUS
    if item.matched_issue_id is not None:
        return StoryArcResolutionState.RESOLVED
    if evidence.series is None or evidence.issue_number is None:
        return StoryArcResolutionState.AMBIGUOUS
    return StoryArcResolutionState.PENDING


def _mylar_arc_status(
    source_arc: Mylar3StoryArcSnapshot,
    entries: Sequence[dict[str, object]],
    settings: Mylar3ArcSettingsSnapshot,
) -> ImportedStoryArcStatus:
    review_states = {
        StoryArcResolutionState.MISSING,
        StoryArcResolutionState.AMBIGUOUS,
        StoryArcResolutionState.CONFLICT,
    }
    if source_arc.name is None or settings.parse_warnings:
        return ImportedStoryArcStatus.NEEDS_REVIEW
    if any(payload["resolution_state"] in review_states for payload in entries):
        return ImportedStoryArcStatus.NEEDS_REVIEW
    return ImportedStoryArcStatus.DETECTED


def _mylar_settings_snapshot(snapshot: Mylar3CollectionSnapshot) -> dict[str, object]:
    settings = snapshot.arc_settings
    values: dict[str, object] = {}
    for setting in settings.values:
        value: bool | str | None = setting.value
        if isinstance(value, str):
            value = _bounded_text(value, 1000)
        values[setting.key] = {
            "section": setting.section,
            "value": value,
            "raw_value": _bounded_text(setting.raw_value, 1000),
            "used_default": bool(setting.used_default),
        }
    return {
        "schema_version": 1,
        "present": bool(settings.present),
        "parse_warnings": [_safe_code(warning) for warning in settings.parse_warnings],
        "values": values,
        "readlist": {
            "present": bool(snapshot.readlist_present),
            "count": max(int(snapshot.readlist_count), 0),
            "import_state": "deferred_v1.5.0",
        },
    }


def _mylar_arc_diagnostics(
    *,
    snapshot: Mylar3CollectionSnapshot,
    source_arc: Mylar3StoryArcSnapshot,
    entries: Sequence[dict[str, object]],
) -> dict[str, object]:
    reading_orders = [
        (
            "numeric",
            payload["reading_order"],
        )
        if payload["reading_order"] is not None
        else (
            "raw",
            payload["reading_order_raw"],
        )
        for payload in entries
        if payload["reading_order"] is not None or payload["reading_order_raw"] is not None
    ]
    missing_count = sum(
        payload["resolution_state"] == StoryArcResolutionState.MISSING for payload in entries
    )
    return {
        "schema_version": 1,
        "storyarcs_present": bool(snapshot.storyarcs_present),
        "entry_count": len(entries),
        "missing_entry_count": missing_count,
        "duplicate_reading_order": len(reading_orders) != len(set(reading_orders)),
        "settings_warning_codes": [
            _safe_code(warning) for warning in snapshot.arc_settings.parse_warnings
        ],
        "external_identities": _mylar_external_identities(source_arc),
        "source_name_present": source_arc.name is not None,
        "readlist_present": bool(snapshot.readlist_present),
        "readlist_count": max(int(snapshot.readlist_count), 0),
        "readlist_import_state": "deferred_v1.5.0",
    }


def _mylar_source_key(source_arc: Mylar3StoryArcSnapshot) -> str:
    if source_arc.story_arc_id is not None:
        identity: object = {
            "kind": "story_arc_id",
            "value": source_arc.story_arc_id,
        }
    elif source_arc.cv_arc_id is not None:
        identity = {
            "kind": "cv_arc_id",
            "value": source_arc.cv_arc_id,
        }
    elif source_arc.name is not None:
        identity = {
            "kind": "story_arc_name",
            "value": source_arc.name,
        }
    else:
        identity = {
            "kind": "anonymous_entries",
            "entries": [
                {
                    "ordinal": entry.ordinal,
                    "issue_arc_id": entry.issue_arc_id,
                    "issue_id": entry.issue_id,
                    "comic_id": entry.comic_id,
                    "issue_number": entry.issue_number,
                }
                for entry in source_arc.entries
            ],
        }
    return f"mylar3:{_digest_json(identity)}"


def _mylar_external_identities(
    source_arc: Mylar3StoryArcSnapshot,
) -> list[dict[str, str]]:
    external_id = _bounded_identifier(source_arc.cv_arc_id, 255)
    if external_id is None:
        return []
    return [
        {
            "source": "comicvine",
            "namespace": "story_arc",
            "external_id": external_id,
        }
    ]


def _folder_source_key(cohort_key: str) -> str:
    return f"folder:{_digest_text(cohort_key)}"


def _folder_label(cohort_key: str) -> str:
    normalized = cohort_key.replace("\\", "/").rstrip("/")
    label = normalized.rsplit("/", 1)[-1]
    return label or "Story Arc"


def _cached_comicinfo(item: ImportedFile) -> Mapping[str, object]:
    diagnostics = _mapping(item.diagnostics)
    source_metadata = _mapping(diagnostics.get("source_metadata")) or diagnostics
    archive_evidence = _mapping(source_metadata.get("archive_member_evidence")) or _mapping(
        diagnostics.get("archive_member_evidence")
    )
    return _mapping(archive_evidence.get("comicinfo")) or _mapping(source_metadata.get("comicinfo"))


def _folder_safety_code(item: ImportedFile) -> str | None:
    safety_block = _mapping(_mapping(item.diagnostics).get("safety_block"))
    return _safe_code(safety_block.get("code"))


def _mylar_review_reason(state: StoryArcResolutionState) -> str | None:
    if state == StoryArcResolutionState.MISSING:
        return "source_issue_missing"
    if state == StoryArcResolutionState.SKIPPED:
        return "source_issue_skipped"
    return None


def _folder_review_reason(state: StoryArcResolutionState) -> str | None:
    if state == StoryArcResolutionState.AMBIGUOUS:
        return "incomplete_identity"
    if state == StoryArcResolutionState.CONFLICT:
        return "identity_conflict"
    return None


def _parse_integral_order(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        return None
    result = int(parsed)
    return result if -(2**31) <= result <= 2**31 - 1 else None


def _bounded_source_location(value: object) -> str | None:
    text = _text(value)
    if text is None or len(text) > 1000:
        return None
    return text


def _bounded_exact_text(value: object, limit: int) -> str | None:
    text = _text(value)
    if text is None or len(text) > limit:
        return None
    return text


def _bounded_identifier(value: object, limit: int) -> str | None:
    text = _text(value)
    if text is None:
        return None
    if len(text) <= limit:
        return text
    suffix = f":sha256:{_digest_text(text)}"
    return f"{text[: limit - len(suffix)]}{suffix}"


def _bounded_text(value: object, limit: int) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit]


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_code(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    sanitized = _SAFE_CODE.sub("_", text)[:100]
    return sanitized or None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _digest_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return _digest_text(payload)


def _require_positive_batch_size(value: int) -> None:
    if value <= 0:
        msg = "Story-arc staging batch size must be positive."
        raise ValueError(msg)


async def _checkpoint(callback: CancellationCheck | None) -> None:
    if callback is not None:
        await callback()

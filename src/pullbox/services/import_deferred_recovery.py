"""Reconcile completed import decisions using physical files and exact identities."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import batched
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pullbox.core.name_matcher import NameMatcher
from pullbox.core.release_parser import normalize_issue_number, parse_release_title
from pullbox.core.source_metadata import _extract_issue_id_from_notes, _extract_issue_id_from_web
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobLog,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.series import IssueCatalogState, Series
from pullbox.services.import_source_metadata import source_metadata_for_import_file
from pullbox.services.import_terminal_recovery import allows_terminal_import_recovery

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select


def positive_id(value: object) -> int | None:
    try:
        result = int(str(value))
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def provider_ids(file: ImportedFile) -> set[int]:
    """Retain disagreements instead of silently preferring one saved identity."""
    summary = file.diagnostics.get("target_issue_summary")
    summary = summary if isinstance(summary, dict) else {}
    source = file.diagnostics.get("source_metadata")
    source = source if isinstance(source, dict) else {}
    comicinfo = source.get("comicinfo")
    comicinfo = comicinfo if isinstance(comicinfo, dict) else {}
    embedded_ids = {
        value
        for value in (
            _extract_issue_id_from_web(comicinfo.get("web")),
            _extract_issue_id_from_notes(comicinfo.get("notes")),
        )
        if value is not None
    }
    if len(embedded_ids) > 1:
        return embedded_ids
    signals = file.diagnostics.get("metadata_signals")
    signals = signals if isinstance(signals, dict) else {}
    source_id = file.comicvine_issue_id
    if len(embedded_ids) == 1 and signals.get("comicvine_issue_id") == "mylar3":
        source_id = next(iter(embedded_ids))
    values = (source_id, file.matched_issue_cv_id, summary.get("provider_id"), *embedded_ids)
    return {positive_id(value) or -1 for value in values if value is not None}


def _unresolved_identity_conflicts(file: ImportedFile) -> bool:
    source = file.diagnostics.get("source_metadata")
    source = source if isinstance(source, dict) else {}
    conflicts = [
        *(source.get("identity_conflicts") or []),
        *(file.diagnostics.get("identity_conflicts") or []),
    ]
    signals = file.diagnostics.get("metadata_signals")
    signals = signals if isinstance(signals, dict) else {}
    ids = provider_ids(file)
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            return True
        if conflict.get("field") == "comicvine_series_id":
            continue
        if (
            conflict.get("field") == "comicvine_issue_id"
            and signals.get("comicvine_issue_id") == "mylar3"
            and len(ids) == 1
            and positive_id(conflict.get("first")) == file.comicvine_issue_id
            and positive_id(conflict.get("conflicting")) in ids
        ):
            continue
        return True
    return False


def protected_file(file: ImportedFile, item: ImportedSeries) -> bool:
    diagnostics = dict(file.diagnostics or {})
    return bool(
        file.status is not ImportedFileStatus.NO_MATCH
        or file.library_file_id is not None
        or file.include_in_import
        or item.user_selected_cv_id is not None
        or (file.match_method or "").startswith(("manual", "orphan_recovery"))
        or any(
            diagnostics.get(key)
            for key in (
                "safety_block",
                "safety_exception",
                "source_revalidation",
                "safety_review",
            )
        )
        or (diagnostics.get("kind") == "metadata_conflict" and bool(provider_ids(file)))
        or _unresolved_identity_conflicts(file)
        or (
            file.conflict_group_id is not None
            and not (
                file.is_preferred
                and file.match_confidence == "high"
                and file.matched_issue_cv_id is not None
            )
        )
        or len(provider_ids(file)) > 1
        or -1 in provider_ids(file)
    )


def same_source(left: ImportedFile, right: ImportedFile | LibraryFile) -> bool:
    if left.file_path != right.file_path or left.file_size != right.file_size:
        return False
    first, second = dict(left.source_signature or {}), dict(right.source_signature or {})
    # Persisted path alone is not proof that a file survived unchanged.
    # Device IDs change when container mounts are recreated. Size and mtime are portable.
    keys = ("size", "size_bytes", "mtime_ns", "content_digest", "content_digest_algorithm")
    common = [key for key in keys if key in first and key in second]
    return bool("mtime_ns" in common and all(first[key] == second[key] for key in common))


def _titles(series: Series) -> set[str]:
    return {NameMatcher.normalize(name) for name in (series.title, *(series.alternate_names or []))}


def _source_agrees(
    file: ImportedFile,
    item: ImportedSeries,
    series: Series,
    issue: Issue,
    *,
    require_issue_number: bool = True,
) -> bool:
    metadata = source_metadata_for_import_file(item, file)
    if metadata.series_name and NameMatcher.normalize(metadata.series_name) not in _titles(series):
        return False
    issue_numbers: list[float] = []
    if metadata.issue_number is not None:
        issue_numbers.append(metadata.issue_number)
    hint = metadata.diagnostics.get("archive_entry_issue_hint")
    if isinstance(hint, dict) and hint.get("confidence") == "strong":
        hint_number = normalize_issue_number(hint.get("issue_number"))
        if hint_number is None:
            return False
        issue_numbers.append(hint_number)
    if (require_issue_number and not issue_numbers) or any(
        number != issue.issue_number for number in issue_numbers
    ):
        return False
    # Type evidence from a release or ComicInfo must not turn an Annual into #1.
    raw_type = file.diagnostics.get("source_issue_type")
    return not raw_type or raw_type == issue.issue_type.value


def strict_filename_target(
    file: ImportedFile, item: ImportedSeries, series: Series, issues: list[Issue]
) -> Issue | None:
    """Reparse only after identifying the series, requiring independent agreement."""
    if series.issue_catalog_state is not IssueCatalogState.COMPLETE:
        return None
    source = file.diagnostics.get("source_metadata")
    if isinstance(source, dict) and source.get("identity_conflicts"):
        return None
    name = re.sub(r"\(converted\)", "", file.file_name, flags=re.IGNORECASE)
    name = re.sub(r"(?<=\D)\.(\d+)\.(?=\s|\()", r" \1 ", name)
    parsed = parse_release_title(
        name, expected_series=(series.title, *(series.alternate_names or []))
    )
    if (
        parsed is None
        or parsed.is_pack
        or parsed.volume is not None
        or parsed.issue_number is None
        or parsed.year is None
        or NameMatcher.normalize(parsed.series_name or "") not in _titles(series)
        or (file.parsed_series and NameMatcher.normalize(file.parsed_series) not in _titles(series))
        or (
            file.parsed_issue_number is not None and file.parsed_issue_number != parsed.issue_number
        )
    ):
        return None
    candidates = [issue for issue in issues if issue.issue_number == parsed.issue_number]
    if len(candidates) != 1:
        return None
    issue = candidates[0]
    if (
        issue.release_date is None
        or abs(issue.release_date.year - parsed.year) > 1
        or parsed.issue_type != issue.issue_type
        or (file.parsed_year is not None and abs(file.parsed_year - issue.release_date.year) > 1)
        or not _source_agrees(file, item, series, issue, require_issue_number=False)
    ):
        return None
    return issue


@dataclass(frozen=True)
class DeferredRecoveryPlan:
    file_id: int
    action: str
    canonical_file_id: int | None = None
    issue_id: int | None = None
    reason: str = ""


async def load_deferred_rows(
    session: AsyncSession, job_id: int
) -> list[tuple[ImportedFile, ImportedSeries]]:
    rows: list[tuple[ImportedFile, ImportedSeries]] = []
    cursor = 0
    while True:
        batch = (
            await session.execute(
                select(ImportedFile, ImportedSeries)
                .join(ImportedSeries, ImportedSeries.id == ImportedFile.import_series_id)
                .where(
                    ImportedFile.import_job_id == job_id,
                    ImportedFile.status == ImportedFileStatus.NO_MATCH,
                    ImportedFile.id > cursor,
                )
                .order_by(ImportedFile.id)
                .limit(500)
            )
        ).all()
        if not batch:
            return rows
        rows.extend((file, item) for file, item in batch)
        cursor = batch[-1][0].id


async def plan_deferred_recovery(
    session: AsyncSession, job_id: int, *, running: bool = False
) -> tuple[DeferredRecoveryPlan, ...]:
    """Return a read-only plan for deterministic local recovery."""
    job = await session.get(ImportJob, job_id)
    if job is None or (not running and not allows_terminal_import_recovery(job)):
        return ()
    rows = await load_deferred_rows(session, job_id)
    groups: dict[str, list[tuple[ImportedFile, ImportedSeries]]] = defaultdict(list)
    for file, item in rows:
        groups[file.file_path].append((file, item))
    cv_ids = set().union(*(provider_ids(file) for file, _ in rows)) if rows else set()
    local_series_ids = {item.series_id for _, item in rows if item.series_id is not None}
    targets: dict[int, tuple[Issue, Series]] = {}
    issues_by_series: dict[int, list[Issue]] = defaultdict(list)
    series_by_id: dict[int, Series] = {}
    for ids in batched(sorted(local_series_ids), 300):
        for issue, series in (
            await session.execute(
                select(Issue, Series)
                .join(Series, Series.id == Issue.series_id)
                .where(Series.id.in_(ids))
            )
        ).all():
            issues_by_series[series.id].append(issue)
            series_by_id[series.id] = series
            if issue.comicvine_id is not None:
                targets[issue.comicvine_id] = issue, series
    for ids in batched(sorted(cv_ids - targets.keys()), 300):
        for issue, series in (
            await session.execute(
                select(Issue, Series)
                .join(Series, Series.id == Issue.series_id)
                .where(Issue.comicvine_id.in_(ids))
            )
        ).all():
            if issue.comicvine_id is not None:
                targets[issue.comicvine_id] = issue, series

    registered: dict[str, LibraryFile] = {}
    owned: dict[int, LibraryFile] = {}
    for ids in batched(
        sorted(
            {issue.id for issue, _ in targets.values()}
            | {issue.id for issues in issues_by_series.values() for issue in issues}
        ),
        300,
    ):
        for library in await session.scalars(
            select(LibraryFile).where(LibraryFile.issue_id.in_(ids))
        ):
            if library.issue_id is not None:
                owned[library.issue_id] = library
            registered[library.file_path] = library
    for paths in batched(sorted(groups), 300):
        for library in await session.scalars(
            select(LibraryFile).where(LibraryFile.file_path.in_(paths))
        ):
            registered[library.file_path] = library

    plans: list[DeferredRecoveryPlan] = []
    for path, cohort in groups.items():
        if any(protected_file(file, item) for file, item in cohort):
            continue
        identities = set().union(*(provider_ids(file) for file, _ in cohort))
        if len(identities) > 1:
            continue
        # Prefer the row whose parent already owns the target catalog.
        cohort.sort(key=lambda pair: (pair[1].series_id is None, pair[0].id))
        file, item = cohort[0]
        if any(not same_source(file, other) for other, _ in cohort[1:]):
            continue
        provider_id = next(iter(identities), None)
        target = targets.get(provider_id) if provider_id is not None else None
        if target is not None:
            issue, series = target
            if any(not _source_agrees(other, parent, series, issue) for other, parent in cohort):
                continue
            if any(other.matched_issue_id not in (None, issue.id) for other, _ in cohort):
                continue
        elif provider_id is None and item.series_id in series_by_id:
            series = series_by_id[item.series_id]
            issue = strict_filename_target(file, item, series, issues_by_series[series.id])
            target = (issue, series) if issue is not None else None

        registered_file = registered.get(path)
        if registered_file is not None:
            if (
                target is None
                or registered_file.issue_id != target[0].id
                or not same_source(file, registered_file)
            ):
                continue
            plans.append(DeferredRecoveryPlan(file.id, "already_registered", issue_id=target[0].id))
        elif target is not None:
            issue = target[0]
            plans.append(
                DeferredRecoveryPlan(
                    file.id,
                    "owned_variant" if issue.id in owned else "exact_target",
                    issue_id=issue.id,
                    reason="provider_id" if provider_id else "strict_filename",
                )
            )
        for other, _ in cohort[1:]:
            plans.append(
                DeferredRecoveryPlan(other.id, "duplicate_reference", canonical_file_id=file.id)
            )

    # Two distinct physical files targeting one unowned issue still need a choice.
    pending: dict[int, list[int]] = defaultdict(list)
    for index, plan in enumerate(plans):
        if plan.action == "exact_target" and plan.issue_id is not None:
            pending[plan.issue_id].append(index)
    ambiguous = {index for indices in pending.values() if len(indices) > 1 for index in indices}
    return tuple(
        sorted(
            (plan for index, plan in enumerate(plans) if index not in ambiguous),
            key=lambda plan: plan.file_id,
        )
    )


def candidate_series_ids(file: ImportedFile, item: ImportedSeries) -> set[int]:
    """Use trusted saved provider identities as candidates, never as proof of ownership."""
    diagnostics = dict(file.diagnostics or {})
    signals = diagnostics.get("metadata_signals")
    signals = signals if isinstance(signals, dict) else {}
    ids: set[int] = set()
    if signals.get("comicvine_series_id") in {"mylar3", "comicinfo", "sidecar", "folder_sidecar"}:
        cv_id = positive_id(diagnostics.get("comicvine_series_id"))
        if cv_id is not None:
            ids.add(cv_id)
        source = diagnostics.get("source_metadata")
        if isinstance(source, dict):
            for conflict in source.get("identity_conflicts") or []:
                if isinstance(conflict, dict) and conflict.get("field") == "comicvine_series_id":
                    ids.update(
                        value
                        for raw in (conflict.get("first"), conflict.get("conflicting"))
                        if (value := positive_id(raw)) is not None
                    )
    candidate = dict(item.diagnostics or {}).get("selected_candidate")
    if isinstance(candidate, dict) and candidate.get("match_method") in {
        "mylar3_cv_id",
        "comicinfo_cv_id",
        "folder_cv_id",
    }:
        cv_id = positive_id(candidate.get("cv_id"))
        if cv_id is not None:
            ids.add(cv_id)
    retained = diagnostics.get("deferred_recovery_candidates")
    if isinstance(retained, list):
        ids.update(value for raw in retained if (value := positive_id(raw)) is not None)
    return ids


def issue_summary(issue: Issue) -> dict[str, Any]:
    return {
        "provider_id": str(issue.comicvine_id) if issue.comicvine_id else None,
        "issue_number": issue.issue_number,
        "issue_number_text": issue.issue_number_text or str(issue.issue_number),
        "title": issue.title,
        "release_date": issue.release_date.isoformat() if issue.release_date else None,
        "cover_url": issue.cover_url,
        "issue_type": issue.issue_type.value,
    }


def apply_proven_identity(
    file: ImportedFile,
    *,
    issue_cv_id: int | None,
    series_cv_id: int | None,
    summary: dict[str, Any],
) -> None:
    """Retain superseded evidence while replacing only an already-proven identity."""
    diagnostics = dict(file.diagnostics or {})
    previous = dict(diagnostics)
    source = dict(diagnostics.get("source_metadata") or {})
    source.pop("identity_conflicts", None)
    diagnostics.pop("identity_conflicts", None)
    diagnostics.update(
        source_metadata=source,
        comicvine_series_id=series_cv_id,
        kind="deferred_exact_identity",
        target_issue_summary=summary,
        deferred_recovery_previous_diagnostics=previous,
    )
    file.diagnostics = diagnostics
    file.comicvine_issue_id = issue_cv_id


async def apply_deferred_recovery(
    session: AsyncSession, job: ImportJob, *, running: bool = False
) -> dict[str, int]:
    """Apply locally proven decisions; materialization remains ordinary Step 4 work."""
    from pullbox.services.import_story_arc_resolution import (
        refresh_story_arc_entries_for_import_files,
    )

    if not running and not allows_terminal_import_recovery(job):
        return {}
    plans = await plan_deferred_recovery(session, job.id, running=running)
    counts: dict[str, int] = defaultdict(int)
    affected: set[int] = set()
    targets: dict[int, ImportedSeries] = {}
    for batch in batched(plans, 300):
        files = {
            file.id: file
            for file in await session.scalars(
                select(ImportedFile).where(ImportedFile.id.in_([plan.file_id for plan in batch]))
            )
        }
        for plan in batch:
            file = files[plan.file_id]
            parent = await session.get(ImportedSeries, file.import_series_id)
            assert parent is not None
            file.diagnostics = {
                **file.diagnostics,
                "deferred_recovery_candidates": sorted(candidate_series_ids(file, parent)),
            }
            affected.add(parent.id)
            evidence = {
                "action": plan.action,
                "reason": plan.reason,
                "source_import_series_id": parent.id,
                "previous_error": file.error_message,
                "source_preserved": True,
                "resolved_at": datetime.now(UTC).isoformat(),
            }
            if plan.action == "duplicate_reference":
                canonical = await session.get(ImportedFile, plan.canonical_file_id)
                assert canonical is not None
                canonical_parent = await session.get(ImportedSeries, canonical.import_series_id)
                assert canonical_parent is not None
                candidates = candidate_series_ids(file, parent) | candidate_series_ids(
                    canonical, canonical_parent
                )
                canonical.diagnostics = {
                    **canonical.diagnostics,
                    "deferred_recovery_candidates": sorted(candidates),
                }
                file.duplicate_of_file_id = canonical.id
                file.status = ImportedFileStatus.SKIPPED
                evidence["canonical_file_id"] = canonical.id
            else:
                issue = await session.get(Issue, plan.issue_id)
                assert issue is not None
                series = await session.get(Series, issue.series_id)
                assert series is not None
                file.matched_issue_id = issue.id
                file.matched_issue_cv_id = issue.comicvine_id
                if plan.action == "already_registered":
                    file.status = ImportedFileStatus.ALREADY_OWNED
                elif plan.action == "owned_variant":
                    file.status = ImportedFileStatus.CONFLICT
                    file.error_message = "This issue is already owned. Review this alternate file."
                else:
                    target = targets.get(series.id)
                    if target is None:
                        # Isolate selected files from unrelated ready rows in the old parent.
                        target = ImportedSeries(
                            import_job_id=job.id,
                            raw_series_name=series.title,
                            raw_year=series.year_start,
                            cv_id=series.comicvine_id,
                            cv_title=series.title,
                            cv_year=series.year_start,
                            cv_match_method="deferred_exact_identity",
                            cv_match_score=1.0,
                            series_id=series.id,
                            has_files=True,
                            status=ImportSeriesStatus.CONFIRMED,
                            selected_for_import=True,
                            diagnostics={"kind": "deferred_recovery", "source_preserved": True},
                        )
                        session.add(target)
                        await session.flush()
                        targets[series.id] = target
                    file.import_series_id = target.id
                    affected.add(target.id)
                    file.status = ImportedFileStatus.CONFIRMED
                    file.include_in_import = True
                    file.match_method = "completed_import_exact_target"
                    file.match_confidence = "high"
                    file.parsed_issue_number = issue.issue_number
                    file.issue_number_raw = issue.issue_number_text
                    file.conflict_group_id = None
                    apply_proven_identity(
                        file,
                        issue_cv_id=issue.comicvine_id,
                        series_cv_id=series.comicvine_id,
                        summary=issue_summary(issue),
                    )
                evidence["target_issue_id"] = issue.id
            if plan.action != "exact_target":
                file.include_in_import = False
            if plan.action != "owned_variant":
                file.error_message = None
            file.diagnostics = {**file.diagnostics, "deferred_recovery": evidence}
            counts[plan.action] += 1
        await refresh_story_arc_entries_for_import_files(
            session, import_job_id=job.id, import_file_ids=list(files)
        )
        await session.flush()

    stale_series_ids: tuple[int, ...] | None = None
    if running:
        state = dict(dict(job.progress_snapshot or {}).get("deferred_recovery") or {})
        raw_ids = state.get("stale_series_ids")
        stale_series_ids = (
            tuple(int(value) for value in raw_ids)
            if isinstance(raw_ids, list) and all(isinstance(value, int) for value in raw_ids)
            else ()
        )
    counts["stale_series"] = await archive_empty_stale_series(
        session,
        job.id,
        series_ids=stale_series_ids,
    )
    await refresh_recovered_groups(session, job, affected)
    snapshot = dict(job.progress_snapshot or {})
    recovery = dict(snapshot.get("deferred_recovery") or {})
    recovery["series_ids"] = sorted(
        set(recovery.get("series_ids", [])) | {item.id for item in targets.values()}
    )
    job.progress_snapshot = {**snapshot, "deferred_recovery": recovery}
    session.add(
        ImportJobLog(
            import_job_id=job.id,
            level="INFO",
            event="import_deferred_recovery_local",
            message="Reconciled deferred file records.",
            data={"counts": dict(counts), "source_preserved": True},
        )
    )
    await session.flush()
    return dict(counts)


async def refresh_recovered_groups(
    session: AsyncSession, job: ImportJob, affected: set[int]
) -> None:
    """Remove finished matching groups while keeping their audit records."""
    from pullbox.services.import_counters import recompute_file_counters, recompute_series_counters

    if affected:
        await recompute_file_counters(session, job, series_ids=sorted(affected))
        # Groups containing only completed decisions no longer need a matching task.
        actionable = {
            ImportedFileStatus.NO_MATCH,
            ImportedFileStatus.CONFLICT,
            ImportedFileStatus.FAILED,
            ImportedFileStatus.SAFETY_BLOCKED,
            ImportedFileStatus.SAFETY_APPROVED,
            ImportedFileStatus.MATCHED,
            ImportedFileStatus.CONFIRMED,
            ImportedFileStatus.PENDING,
        }
        for ids in batched(sorted(affected), 300):
            open_ids = set(
                await session.scalars(
                    select(ImportedFile.import_series_id)
                    .where(
                        ImportedFile.import_series_id.in_(ids), ImportedFile.status.in_(actionable)
                    )
                    .distinct()
                )
            )
            for item in await session.scalars(
                select(ImportedSeries).where(ImportedSeries.id.in_(ids))
            ):
                if item.id not in open_ids and item.status in {
                    ImportSeriesStatus.NO_MATCH,
                    ImportSeriesStatus.RECOVERY_PENDING,
                }:
                    item.status = ImportSeriesStatus.SKIPPED
                    item.selected_for_import = False
                    item.diagnostics = {
                        **item.diagnostics,
                        "follow_up_resolved": "all_files_handled",
                    }
    await recompute_series_counters(session, job)


def empty_stale_series_query(job_id: int) -> Select[tuple[ImportedSeries]]:
    """Return empty missing-location rows eligible for follow-up archival."""
    return select(ImportedSeries).where(
        ImportedSeries.import_job_id == job_id,
        ImportedSeries.status.in_(
            (ImportSeriesStatus.NO_MATCH, ImportSeriesStatus.RECOVERY_PENDING)
        ),
        ImportedSeries.user_selected_cv_id.is_(None),
        ImportedSeries.diagnostics["reason"].as_string().in_(("path_missing", "source_missing")),
        ~select(ImportedFile.id).where(ImportedFile.import_series_id == ImportedSeries.id).exists(),
    )


async def load_empty_stale_series(
    session: AsyncSession,
    job_id: int,
) -> list[ImportedSeries]:
    """Load empty stale series deterministically for signed cleanup previews."""
    return list(await session.scalars(empty_stale_series_query(job_id).order_by(ImportedSeries.id)))


async def archive_empty_stale_series(
    session: AsyncSession,
    job_id: int,
    *,
    series_ids: tuple[int, ...] | None = None,
) -> int:
    """Retain empty missing Mylar locations in history rather than active matching."""
    query = empty_stale_series_query(job_id)
    if series_ids is not None:
        query = query.where(ImportedSeries.id.in_(series_ids))
    items = list(await session.scalars(query.order_by(ImportedSeries.id)))
    for item in items:
        item.status = ImportSeriesStatus.SKIPPED
        item.selected_for_import = False
        item.diagnostics = {
            **item.diagnostics,
            "follow_up_resolved": "stale_empty_reference",
            "archived_at": datetime.now(UTC).isoformat(),
        }
    return len(items)

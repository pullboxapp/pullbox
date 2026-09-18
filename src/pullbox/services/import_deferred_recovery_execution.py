"""Durable background preparation for completed-import file recovery."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pullbox.core.exceptions import JobPausedError, NotFoundError, ProviderError, ValidationError
from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.name_matcher import NameMatcher
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.catalog.contract import CatalogError
from pullbox.services.import_counters import recompute_file_counters, recompute_series_counters
from pullbox.services.import_deferred_recovery import (
    apply_deferred_recovery,
    apply_proven_identity,
    candidate_series_ids,
    load_deferred_rows,
    positive_id,
    protected_file,
    provider_ids,
    refresh_recovered_groups,
)
from pullbox.services.import_reference_recovery import (
    reference_candidates,
    repair_catalog_references,
)
from pullbox.services.import_source_metadata import source_metadata_for_import_file
from pullbox.services.import_workflow_state import (
    emit_live_progress,
    emit_progress,
    raise_if_job_cancelled,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.providers.base import IssueSummary
    from pullbox.services.metadata_service import MetadataService


def recovery_state(job: ImportJob) -> dict[str, Any]:
    return dict(dict(job.progress_snapshot or {}).get("deferred_recovery") or {})


def save_recovery_state(job: ImportJob, state: dict[str, Any]) -> None:
    job.progress_snapshot = {**dict(job.progress_snapshot or {}), "deferred_recovery": state}


def _catalog_summary_payload(summary: IssueSummary) -> dict[str, Any]:
    """Return a durable JSON-safe catalog checkpoint payload."""
    payload = asdict(summary)
    source_cutoff_at = payload.get("source_cutoff_at")
    if isinstance(source_cutoff_at, datetime):
        payload["source_cutoff_at"] = source_cutoff_at.isoformat()
    return payload


def _filename_catalog_identity(
    file: ImportedFile,
    item: ImportedSeries,
) -> dict[str, str | int] | None:
    """Return bounded filename identity eligible for local-catalog recovery."""
    if protected_file(file, item) or provider_ids(file):
        return None
    diagnostics = dict(file.diagnostics or {})
    source = source_metadata_for_import_file(item, file).diagnostics
    if source.get("identity_conflicts"):
        return None
    parsed = source.get("filename_parse")
    parsed = parsed if isinstance(parsed, dict) else {}
    source_title = str(parsed.get("series_name") or "").strip()
    raw_number = parsed.get("issue_number_text") or parsed.get("issue_number")
    if not source_title or raw_number is None:
        return None
    try:
        issue_number = normalize_issue_number_text(str(raw_number))
    except ValueError:
        return None
    issue_type = str(parsed.get("issue_type") or diagnostics.get("source_issue_type") or "issue")
    if issue_type in {"annual", "special"}:
        label = issue_type.title()
        if not NameMatcher.normalize(source_title).endswith(f" {NameMatcher.normalize(label)}"):
            source_title = f"{source_title} {label}"
    normalized_title = NameMatcher.normalize(source_title)
    parent_title = NameMatcher.normalize(item.cv_title or item.raw_series_name)
    if not normalized_title or normalized_title == parent_title:
        return None
    title_match = NameMatcher().match(source_title, item.cv_title or item.raw_series_name)
    corroborated = diagnostics.get("conflict_type") == "corroborated_file_series_mismatch"
    if title_match.is_match and not corroborated and issue_type not in {"annual", "special"}:
        return None
    year = parsed.get("year") or file.parsed_year
    return {
        "key": normalized_title,
        "query": source_title,
        "issue_number": issue_number,
        "year": int(year) if isinstance(year, int | float) else 0,
        "issue_type": issue_type,
    }


async def _catalog_title_candidates(
    session: AsyncSession,
    job: ImportJob,
    references: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Group deterministic filename searches so each catalog title is queried once."""
    candidates: dict[str, dict[str, Any]] = {}
    identities = [
        identity
        for file, item in await load_deferred_rows(session, job.id)
        if (identity := _filename_catalog_identity(file, item)) is not None
    ]
    identities.extend((references or {}).values())
    for identity in identities:
        key = str(identity["key"])
        candidate = candidates.setdefault(
            key,
            {
                "query": str(identity["query"]),
                "issue_numbers": set(),
                "years": set(),
                "years_by_issue": defaultdict(set),
            },
        )
        issue_number = str(identity["issue_number"])
        candidate["issue_numbers"].add(issue_number)
        if int(identity["year"]):
            year = int(identity["year"])
            candidate["years"].add(year)
            candidate["years_by_issue"][issue_number].add(year)
    return {
        key: {
            "query": value["query"],
            "issue_numbers": sorted(value["issue_numbers"]),
            "years": sorted(value["years"]),
            "years_by_issue": {
                number: sorted(years) for number, years in sorted(value["years_by_issue"].items())
            },
        }
        for key, value in candidates.items()
    }


def _summary_year(summary: IssueSummary) -> int | None:
    release_date = summary.release_date
    if release_date is None:
        return None
    if isinstance(release_date, str):
        try:
            return int(release_date[:4])
        except ValueError:
            return None
    return release_date.year


async def _search_exact_title_catalog(
    metadata_service: MetadataService,
    candidate: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Return exact local-catalog issue targets for one normalized title."""
    query = str(candidate["query"])
    normalized_query = NameMatcher.normalize(query)
    results = await metadata_service.search_catalog_series(query, limit=1_000)
    exact_results = [
        result for result in results if NameMatcher.normalize(result.title) == normalized_query
    ]
    if not exact_results or len(exact_results) > 24:
        return {}
    needed_numbers = {str(value) for value in candidate.get("issue_numbers", [])}
    fallback_years = {int(value) for value in candidate.get("years", [])}
    raw_years_by_issue = candidate.get("years_by_issue")
    years_by_issue = raw_years_by_issue if isinstance(raw_years_by_issue, dict) else {}
    matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in exact_results:
        summaries = await metadata_service.get_catalog_issue_summaries_for_series(
            int(result.provider_id)
        )
        for summary in summaries:
            exact_number = normalize_issue_number_text(
                summary.issue_number_text or summary.issue_number
            )
            if exact_number not in needed_numbers:
                continue
            summary_year = _summary_year(summary)
            expected_years = {
                int(value) for value in years_by_issue.get(exact_number, fallback_years)
            }
            if (
                summary_year is not None
                and expected_years
                and all(abs(summary_year - year) > 1 for year in expected_years)
            ):
                continue
            matches[exact_number].append(
                {
                    "cv_id": int(result.provider_id),
                    "title": result.title,
                    "year": result.year_start,
                    "issue_count": result.issue_count,
                    "publisher": result.publisher,
                    "cover_url": result.cover_url,
                    "comicvine_url": result.comicvine_url,
                    "summary": _catalog_summary_payload(summary),
                }
            )
    return dict(matches)


async def cancel_deferred_preparation(session: AsyncSession, job: ImportJob) -> bool:
    """Stop this recovery pass, retaining the original and any completed imports."""
    state = recovery_state(job)
    if state.get("state") not in {"queued", "catalogs", "prepared"}:
        return False
    ids = state.get("series_ids", [])
    for item in await session.scalars(
        select(ImportedSeries).where(
            ImportedSeries.import_job_id == job.id, ImportedSeries.id.in_(ids)
        )
    ):
        pending = list(
            await session.scalars(
                select(ImportedFile).where(
                    ImportedFile.import_series_id == item.id,
                    ImportedFile.status.in_(
                        (ImportedFileStatus.CONFIRMED, ImportedFileStatus.MATCHED)
                    ),
                )
            )
        )
        for file in pending:
            file.status = ImportedFileStatus.NO_MATCH
            file.include_in_import = False
        if pending:
            item.status = ImportSeriesStatus.RECOVERY_PENDING
            item.selected_for_import = False
    await recompute_file_counters(session, job, series_ids=ids)
    await recompute_series_counters(session, job)
    state["state"] = "cancelled"
    save_recovery_state(job, state)
    job.status = ImportJobStatus.COMPLETED
    job.control_request = ImportControlRequest.NONE
    job.error_message = None
    job.progress_snapshot = {
        **job.progress_snapshot,
        "status": "completed",
        "phase": "done",
        "progress": 100,
        "message": "Recovery stopped. Completed imports and source files were preserved.",
    }
    session.add(
        ImportJobLog(
            import_job_id=job.id,
            level="INFO",
            event="import_deferred_recovery_cancelled",
            message="Recovery stopped without rolling back the original import.",
            data={"source_preserved": True},
        )
    )
    await session.commit()
    return True


async def _catalog_candidates(session: AsyncSession, job: ImportJob) -> dict[str, list[int]]:
    candidates: dict[str, set[int]] = defaultdict(set)
    rows = await load_deferred_rows(session, job.id)
    for file, item in rows:
        if protected_file(file, item) or len(provider_ids(file)) != 1:
            continue
        issue_cv_id = next(iter(provider_ids(file)))
        for series_cv_id in candidate_series_ids(file, item):
            candidates[str(series_cv_id)].add(issue_cv_id)
    # Never fetch catalogs for an issue whose canonical local ownership is already known.
    from itertools import batched

    known: set[int] = set()
    for ids in batched(sorted(set().union(*candidates.values()) if candidates else set()), 300):
        known.update(
            value
            for value in await session.scalars(
                select(Issue.comicvine_id).where(Issue.comicvine_id.in_(ids))
            )
            if value is not None
        )
    return {key: sorted(values - known) for key, values in candidates.items() if values - known}


def _catalog_target_agrees(
    file: ImportedFile, item: ImportedSeries, target: dict[str, Any]
) -> bool:
    metadata = source_metadata_for_import_file(item, file)
    summary = target["summary"]
    source_title = metadata.series_name or ""
    hint = metadata.diagnostics.get("archive_entry_issue_hint")
    if (
        isinstance(hint, dict)
        and hint.get("confidence") == "strong"
        and hint.get("issue_number") != summary["issue_number"]
    ):
        return False
    return bool(
        source_title
        and NameMatcher.normalize(source_title) == NameMatcher.normalize(target["title"])
        and metadata.issue_number is not None
        and metadata.issue_number == summary["issue_number"]
        and (
            not file.diagnostics.get("source_issue_type")
            or file.diagnostics["source_issue_type"] == summary["issue_type"]
        )
        and file.matched_issue_id is None
    )


async def _prepare_catalog_targets(session: AsyncSession, job: ImportJob) -> int:
    """Only stage unique issue membership with agreeing file evidence."""
    state = recovery_state(job)
    matches = state.get("matches", {})
    rows = await load_deferred_rows(session, job.id)
    eligible: list[tuple[ImportedFile, ImportedSeries, dict[str, Any]]] = []
    for file, item in rows:
        ids = provider_ids(file)
        if protected_file(file, item) or len(ids) != 1:
            continue
        options = matches.get(str(next(iter(ids))), [])
        # Membership in conflicting candidate catalogs is ambiguous even when titles agree.
        if len(options) != 1 or not _catalog_target_agrees(file, item, options[0]):
            continue
        eligible.append((file, item, options[0]))
    counts = Counter(str(target["summary"]["provider_id"]) for _, _, target in eligible)
    targets: dict[int, ImportedSeries] = {}
    affected: set[int] = set()
    ready = 0
    for file, original, target in eligible:
        issue_cv_id = positive_id(target["summary"]["provider_id"])
        if issue_cv_id is None or counts[str(issue_cv_id)] != 1:
            continue
        if await session.scalar(select(Issue.id).where(Issue.comicvine_id == issue_cv_id)):
            continue
        target_cv_id = int(target["cv_id"])
        target_item = targets.get(target_cv_id)
        if target_item is None:
            target_item = ImportedSeries(
                import_job_id=job.id,
                raw_series_name=target["title"],
                raw_year=target["year"],
                cv_id=target_cv_id,
                cv_title=target["title"],
                cv_year=target["year"],
                cv_match_score=1.0,
                cv_match_method="deferred_catalog_identity",
                status=ImportSeriesStatus.CONFIRMED,
                selected_for_import=True,
                has_files=True,
                diagnostics={"kind": "deferred_recovery", "source_preserved": True},
            )
            session.add(target_item)
            await session.flush()
            targets[target_cv_id] = target_item
        affected.update((original.id, target_item.id))
        file.import_series_id = target_item.id
        file.status = ImportedFileStatus.CONFIRMED
        file.matched_issue_cv_id = issue_cv_id
        file.include_in_import = True
        file.match_confidence = "high"
        file.match_method = "completed_import_exact_target"
        file.error_message = None
        file.conflict_group_id = None
        apply_proven_identity(
            file, issue_cv_id=issue_cv_id, series_cv_id=target_cv_id, summary=target["summary"]
        )
        file.diagnostics = {
            **file.diagnostics,
            "target_issue_summary": target["summary"],
            "deferred_recovery": {
                "action": "catalog_identity",
                "source_import_series_id": original.id,
                "target_series_cv_id": target_cv_id,
                "source_preserved": True,
                "resolved_at": datetime.now(UTC).isoformat(),
            },
        }
        ready += 1
    if affected:
        from pullbox.services.import_story_arc_resolution import (
            refresh_story_arc_entries_for_import_files,
        )

        await refresh_recovered_groups(session, job, affected)
        await refresh_story_arc_entries_for_import_files(
            session,
            import_job_id=job.id,
            import_file_ids=[
                file.id for file, _, _ in eligible if file.status is ImportedFileStatus.CONFIRMED
            ],
        )
    state["series_ids"] = sorted(
        set(state.get("series_ids", [])) | {item.id for item in targets.values()}
    )
    save_recovery_state(job, state)
    await session.flush()
    return ready


async def _prepare_title_catalog_targets(session: AsyncSession, job: ImportJob) -> int:
    """Stage only unique exact title-and-issue catalog identities."""
    state = recovery_state(job)
    title_matches = state.get("title_matches", {})
    rows = await load_deferred_rows(session, job.id)
    eligible: list[tuple[ImportedFile, ImportedSeries, dict[str, Any]]] = []
    for file, item in rows:
        identity = _filename_catalog_identity(file, item)
        if identity is None:
            continue
        options_by_number = title_matches.get(str(identity["key"]), {})
        options = options_by_number.get(str(identity["issue_number"]), [])
        if len(options) != 1:
            continue
        eligible.append((file, item, options[0]))

    counts = Counter(str(target["summary"]["provider_id"]) for _, _, target in eligible)
    issue_cv_ids = {
        issue_cv_id
        for _file, _item, target in eligible
        if (issue_cv_id := positive_id(target["summary"].get("provider_id"))) is not None
    }
    existing_issue_ids: set[int] = set()
    from itertools import batched

    for ids in batched(sorted(issue_cv_ids), 300):
        existing_issue_ids.update(
            int(value)
            for value in await session.scalars(
                select(Issue.comicvine_id).where(Issue.comicvine_id.in_(ids))
            )
            if value is not None
        )

    targets: dict[int, ImportedSeries] = {}
    affected: set[int] = set()
    ready = 0
    for file, original, target in eligible:
        issue_cv_id = positive_id(target["summary"].get("provider_id"))
        target_cv_id = positive_id(target.get("cv_id"))
        if (
            issue_cv_id is None
            or target_cv_id is None
            or counts[str(issue_cv_id)] != 1
            or issue_cv_id in existing_issue_ids
        ):
            continue
        target_item = targets.get(target_cv_id)
        if target_item is None:
            target_item = ImportedSeries(
                import_job_id=job.id,
                raw_series_name=str(target["title"]),
                raw_year=target.get("year"),
                cv_id=target_cv_id,
                cv_title=str(target["title"]),
                cv_year=target.get("year"),
                cv_match_score=1.0,
                cv_match_method="deferred_catalog_title_identity",
                status=ImportSeriesStatus.CONFIRMED,
                selected_for_import=True,
                has_files=True,
                diagnostics={
                    "kind": "deferred_recovery",
                    "reason": "unique_catalog_title_and_issue",
                    "source_preserved": True,
                },
            )
            session.add(target_item)
            await session.flush()
            targets[target_cv_id] = target_item
        affected.update((original.id, target_item.id))
        file.import_series_id = target_item.id
        file.status = ImportedFileStatus.CONFIRMED
        file.matched_issue_cv_id = issue_cv_id
        file.include_in_import = True
        file.match_confidence = "high"
        file.match_method = "completed_import_catalog_title_target"
        file.error_message = None
        file.conflict_group_id = None
        apply_proven_identity(
            file,
            issue_cv_id=issue_cv_id,
            series_cv_id=target_cv_id,
            summary=target["summary"],
        )
        file.diagnostics = {
            **file.diagnostics,
            "target_issue_summary": target["summary"],
            "deferred_recovery": {
                "action": "catalog_title_identity",
                "source_import_series_id": original.id,
                "target_series_cv_id": target_cv_id,
                "source_preserved": True,
                "resolved_at": datetime.now(UTC).isoformat(),
            },
        }
        ready += 1
    if affected:
        from pullbox.services.import_story_arc_resolution import (
            refresh_story_arc_entries_for_import_files,
        )

        await refresh_recovered_groups(session, job, affected)
        await refresh_story_arc_entries_for_import_files(
            session,
            import_job_id=job.id,
            import_file_ids=[
                file.id for file, _, _ in eligible if file.status is ImportedFileStatus.CONFIRMED
            ],
        )
    state["series_ids"] = sorted(
        set(state.get("series_ids", [])) | {item.id for item in targets.values()}
    )
    save_recovery_state(job, state)
    await session.flush()
    return ready


async def prepare_deferred_recovery(
    session: AsyncSession,
    job_id: int,
    *,
    metadata_service: MetadataService,
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
) -> bool:
    """Resume a saved recovery request, preparing exact files for normal execution."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    state = recovery_state(job)
    if not state or state.get("state") in {"prepared", "completed", "cancelled"}:
        return False
    if job.status is not ImportJobStatus.IMPORTING:
        raise ValidationError("Deferred recovery must run inside the import worker.")

    revision_state = {"value": int(job.progress_revision or 0)}

    def progress_event(
        current: int,
        total: int,
        message: str,
        unit: str,
    ) -> ImportProgressEvent:
        return ImportProgressEvent(
            job_id=job_id,
            status=ImportJobStatus.IMPORTING,
            mode="import",
            phase="deferred_recovery",
            progress=round(15 * current / max(total, 1)),
            message=message,
            current_file_stage="deferred_recovery",
            current_file_progress_current=current,
            current_file_progress_total=total,
            current_file_progress_pct=round(100 * current / max(total, 1)),
            current_file_progress_unit=unit,
        )

    async def report(
        current: int,
        total: int,
        message: str,
        *,
        durable: bool = True,
        check_control: bool = True,
        unit: str = "catalogs",
    ) -> None:
        if check_control:
            await raise_if_job_cancelled(session, job_id)
        event = progress_event(current, total, message, unit)
        if durable:
            event.progress_revision = revision_state["value"] + 1
            await emit_progress(session, job, event, progress_callback)
            revision_state["value"] = event.progress_revision
            return
        # A read-only control check still opens a transaction. Close it before
        # provider I/O, then publish live progress without rewriting the large
        # durable recovery checkpoint a second time.
        await session.commit()
        await emit_live_progress(
            job,
            event,
            progress_callback=progress_callback,
            revision_state=revision_state,
            started_at=job.import_started_at,
        )

    if state.get("state") == "queued":
        await report(0, 1, "Reconciling deferred files with the existing library...")
        local_counts = await apply_deferred_recovery(session, job, running=True)
        state = recovery_state(job)
        references = state.get("reference_candidates")
        if references is None:
            references = await reference_candidates(session, job.id)
        state.update(
            state="catalogs",
            local_counts=local_counts,
            candidates=await _catalog_candidates(session, job),
            completed=[],
            matches={},
            reference_candidates=references,
            reference_files_repaired=0,
            title_candidates=await _catalog_title_candidates(session, job, references),
            title_completed=[],
            title_matches={},
        )
        save_recovery_state(job, state)
        await session.commit()

    title_state_needs_initialization = "title_candidates" not in state
    pending_title_candidates = (
        await _catalog_title_candidates(session, job) if title_state_needs_initialization else {}
    )

    candidates = state["candidates"]
    completed = set(state.get("completed", []))
    for cv_id_text, needed_ids in sorted(candidates.items()):
        if cv_id_text in completed:
            continue
        await report(
            len(completed),
            len(candidates),
            f"Checking series catalog {len(completed) + 1} of {len(candidates)}...",
            durable=False,
        )
        # The live progress report closes its read transaction before provider I/O.
        cv_id = int(cv_id_text)
        try:
            series = await metadata_service.get_series_metadata(cv_id)
            summaries = await metadata_service.get_issue_summaries_for_series(cv_id)
        except NotFoundError:
            series = None
            summaries = []
        except (ProviderError, CatalogError) as exc:
            job.error_message = (
                "Metadata is temporarily unavailable. Resume recovery when it is available."
            )
            await report(len(completed), len(candidates), job.error_message)
            raise JobPausedError(job.error_message) from exc
        await raise_if_job_cancelled(session, job_id)
        if series is not None and positive_id(series.provider_id) == cv_id:
            needed = set(needed_ids)
            for summary in summaries:
                if positive_id(summary.provider_id) not in needed:
                    continue
                entries = state["matches"].setdefault(str(summary.provider_id), [])
                entries.append(
                    {
                        "cv_id": cv_id,
                        "title": series.title,
                        "year": series.year_start,
                        "summary": _catalog_summary_payload(summary),
                    }
                )
        completed.add(cv_id_text)
        state["completed"] = sorted(completed)
        save_recovery_state(job, state)
        await report(
            len(completed),
            len(candidates),
            f"Checked series catalog {len(completed)} of {len(candidates)}.",
            check_control=False,
        )

    if title_state_needs_initialization:
        state = recovery_state(job)
        state["title_candidates"] = pending_title_candidates
        state["title_completed"] = []
        state["title_matches"] = {}
        save_recovery_state(job, state)
        await session.commit()

    title_candidates = state.get("title_candidates", {})
    title_completed = set(state.get("title_completed", []))
    for key, candidate in sorted(title_candidates.items()):
        if key in title_completed:
            continue
        await report(
            len(title_completed),
            len(title_candidates),
            f"Checking exact title catalog {len(title_completed) + 1} "
            f"of {len(title_candidates)}...",
            durable=False,
        )
        try:
            state["title_matches"][key] = await _search_exact_title_catalog(
                metadata_service,
                candidate,
            )
        except (ProviderError, CatalogError) as exc:
            job.error_message = (
                "The local metadata catalog is temporarily unavailable. "
                "Resume recovery after repairing or updating it."
            )
            await report(len(title_completed), len(title_candidates), job.error_message)
            raise JobPausedError(job.error_message) from exc
        await raise_if_job_cancelled(session, job_id)
        title_completed.add(key)
        state["title_completed"] = sorted(title_completed)
        save_recovery_state(job, state)
        await report(
            len(title_completed),
            len(title_candidates),
            f"Checked exact title catalog {len(title_completed)} of {len(title_candidates)}.",
            check_control=False,
        )

    await report(len(completed), max(len(candidates), 1), "Preparing verified files for import...")
    catalog_count = await _prepare_catalog_targets(session, job)
    title_catalog_count = await _prepare_title_catalog_targets(session, job)
    await session.commit()

    async def reference_progress(current: int, total: int) -> None:
        await report(
            current,
            total,
            f"Checking misplaced references {current} of {total}...",
            durable=False,
            unit="files",
        )

    reference_count = await repair_catalog_references(
        session,
        job,
        metadata_service,
        state.get("reference_candidates", {}),
        state.get("title_matches", {}),
        progress=reference_progress,
    )
    state = recovery_state(job)
    state.update(
        state="prepared",
        catalog_files_prepared=catalog_count,
        title_catalog_files_prepared=title_catalog_count,
        reference_files_repaired=reference_count,
    )
    job.error_message = None
    if not state.get("series_ids"):
        state["state"] = "completed"
        job.status = ImportJobStatus.COMPLETED
        job.progress_snapshot = {
            **job.progress_snapshot,
            "status": "completed",
            "phase": "done",
            "progress": 100,
            "message": "Recovery completed. Remaining files still need review.",
        }
    save_recovery_state(job, state)
    session.add(
        ImportJobLog(
            import_job_id=job_id,
            level="INFO",
            event="import_deferred_recovery_prepared",
            message="Deferred recovery preparation completed.",
            data={
                "local_counts": state.get("local_counts", {}),
                "catalogs_checked": len(completed),
                "catalog_files_prepared": catalog_count,
                "title_catalogs_checked": len(title_completed),
                "title_catalog_files_prepared": title_catalog_count,
                "reference_files_repaired": reference_count,
                "source_preserved": True,
            },
        )
    )
    await session.commit()
    return True

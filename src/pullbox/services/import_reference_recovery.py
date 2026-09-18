"""Conservative catalog repair of already-imported, user-owned references."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, or_, select

from pullbox.core.exceptions import ConfigurationError
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.core.name_matcher import NameMatcher
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, LibraryFileStorageMode, LibraryRoot
from pullbox.models.series import IssueCatalogState, Series
from pullbox.providers.base import IssueSummary
from pullbox.services.catalog.reader import CatalogIssueSummary, CatalogSeriesMetadata
from pullbox.services.import_deferred_recovery import (
    apply_proven_identity,
    provider_ids,
    refresh_recovered_groups,
    same_source,
)
from pullbox.services.import_recovery_identity import (
    catalog_file_identity,
    catalog_target_agrees,
    record_catalog_review,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select

    from pullbox.models.import_job import ImportJob
    from pullbox.services.metadata_service import MetadataService


def reference_candidate_ids(job_id: int) -> Select[tuple[int]]:
    """Bound the existing recheck preview to potentially misplaced references."""
    return (
        select(ImportedFile.id)
        .join(ImportedSeries, ImportedSeries.id == ImportedFile.import_series_id)
        .join(LibraryFile, LibraryFile.id == ImportedFile.library_file_id)
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.status == ImportedFileStatus.IMPORTED,
            LibraryFile.storage_mode == LibraryFileStorageMode.REFERENCED,
            LibraryFile.file_path == ImportedFile.file_path,
            LibraryFile.issue_id == ImportedFile.matched_issue_id,
            ImportedSeries.user_selected_cv_id.is_(None),
            or_(
                ImportedFile.match_method.is_(None),
                and_(
                    ~ImportedFile.match_method.startswith("manual"),
                    ~ImportedFile.match_method.startswith("orphan_recovery"),
                    ImportedFile.match_method != "completed_import_metadata_reassignment",
                ),
            ),
            or_(
                ImportedFile.parsed_series != ImportedSeries.raw_series_name,
                ImportedFile.diagnostics["source_metadata"]["comicinfo"]["series"].as_string()
                != ImportedSeries.raw_series_name,
                ImportedFile.match_method == "import_reconcile_provisional_issue",
                and_(
                    ImportedSeries.files_no_match > 0,
                    ImportedFile.diagnostics["metadata_signals"]["issue_number"].as_string()
                    == "release_title",
                ),
            ),
        )
    )


def _stamp(file: ImportedFile, item: ImportedSeries, library: LibraryFile) -> str:
    payload = [
        file.updated_at.isoformat(),
        library.updated_at.isoformat(),
        item.user_selected_cv_id,
        item.cv_id,
        item.cv_title,
        item.raw_series_name,
        item.series_id,
        file.file_path,
        file.file_name,
        file.file_size,
        file.status.value,
        file.match_method,
        file.import_series_id,
        file.library_file_id,
        file.matched_issue_id,
        file.matched_issue_cv_id,
        file.comicvine_issue_id,
        file.conflict_group_id,
        file.duplicate_group_id,
        file.diagnostics,
        file.source_signature,
        library.source_signature,
        library.issue_id,
        library.storage_mode.value,
        library.library_root_id,
    ]
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def reference_candidates(session: AsyncSession, job_id: int) -> dict[str, dict[str, Any]]:
    """Freeze exact identity evidence without inspecting or modifying source files."""
    result: dict[str, dict[str, Any]] = {}
    rows = await session.stream(
        select(ImportedFile, ImportedSeries, LibraryFile, Issue)
        .join(ImportedSeries, ImportedSeries.id == ImportedFile.import_series_id)
        .join(LibraryFile, LibraryFile.id == ImportedFile.library_file_id)
        .join(Issue, Issue.id == LibraryFile.issue_id)
        .where(ImportedFile.id.in_(reference_candidate_ids(job_id)))
        .execution_options(yield_per=500)
    )
    try:
        async for file, item, library, old_issue in rows:
            diagnostics = dict(file.diagnostics or {})
            raw_source = diagnostics.get("source_metadata")
            source = raw_source if isinstance(raw_source, dict) else {}
            if (
                file.conflict_group_id is not None
                or file.duplicate_group_id is not None
                or not same_source(file, library)
                or any(
                    diagnostics.get(key)
                    for key in (
                        "safety_block",
                        "safety_exception",
                        "safety_review",
                        "source_revalidation",
                        "identity_conflicts",
                    )
                )
                or source.get("identity_conflicts")
            ):
                continue
            identity = catalog_file_identity(file)
            if identity is None:
                continue
            issue_cv_id = identity["issue_cv_id"]
            evidence = identity["evidence"]
            # A derived old match is not source evidence. Any other saved ID must
            # agree with the trusted embedded identity, or remain for review.
            allowed_ids = {value for value in (old_issue.comicvine_id, issue_cv_id) if value}
            if (
                not provider_ids(file).issubset(allowed_ids)
                or (file.comicvine_issue_id is not None and issue_cv_id is None)
                or (source.get("comicinfo") and evidence == "filename_parse" and provider_ids(file))
            ):
                continue
            key = identity["key"]
            if not key or key == NameMatcher.normalize(item.cv_title or item.raw_series_name):
                continue
            result[str(file.id)] = {
                **identity,
                "stamp": _stamp(file, item, library),
            }
    finally:
        await rows.close()
    return result


def _target_agrees(identity: dict[str, Any], target: dict[str, Any]) -> bool:
    return catalog_target_agrees(identity, target)


def _unchanged_source(path: str, signature: dict[str, Any], root_path: str) -> bool:
    try:
        current = build_file_identity_signature(Path(path))
        if not Path(str(current["resolved_path"])).is_relative_to(
            Path(root_path).resolve(strict=True)
        ):
            return False
    except (OSError, RuntimeError, ValueError, ConfigurationError):
        return False
    # Device/inode can change after a container upgrade; size, mtime and path
    # remain portable. Recovery never writes to the artifact being inspected.
    size = signature.get("size", signature.get("size_bytes"))
    return bool(
        size == current["size"]
        and signature.get("mtime_ns") == current["mtime_ns"]
        and signature.get("resolved_path", path) == current["resolved_path"]
    )


async def repair_catalog_references(
    session: AsyncSession,
    job: ImportJob,
    metadata: MetadataService,
    saved: dict[str, dict[str, Any]],
    matches: dict[str, Any],
    progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> int:
    """Create missing metadata targets and reuse ownership repair, never file import."""
    from pullbox.services.import_completed_cleanup import (
        _apply_mixed_folder_resolutions,
        _MixedFolderResolution,
    )
    from pullbox.services.import_workflow_state import raise_if_job_cancelled

    current = await reference_candidates(session, job.id)
    plans: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for file_id_text, identity in saved.items():
        if current.get(file_id_text) != identity:
            continue
        options = matches.get(identity["key"], {}).get(identity["issue_number"], [])
        options = [option for option in options if _target_agrees(identity, option)]
        if len(options) == 1:
            plans.append((int(file_id_text), identity, options[0]))
        else:
            file = await session.get(ImportedFile, int(file_id_text))
            if file is not None:
                record_catalog_review(
                    file,
                    identity,
                    "Multiple catalog issues agree; choose the correct series and issue."
                    if options
                    else "No catalog issue agrees with this file's title, number, type and year.",
                )
    counts = Counter(str(target["summary"]["provider_id"]) for _, _, target in plans)
    state = dict(job.progress_snapshot.get("deferred_recovery") or {})
    repaired = int(state.get("reference_files_repaired", 0))
    for position, (file_id, identity, target) in enumerate(plans):
        if progress is not None:
            await progress(position, len(plans))
        if counts[str(target["summary"]["provider_id"])] != 1:
            file = await session.get(ImportedFile, file_id)
            if file is not None:
                record_catalog_review(
                    file, identity, "Multiple files claim this issue; choose which copy to keep."
                )
            continue
        await raise_if_job_cancelled(session, job.id)
        file = await session.get(ImportedFile, file_id)
        if file is None or file.library_file_id is None:
            continue
        library = await session.get(LibraryFile, file.library_file_id)
        item = await session.get(ImportedSeries, file.import_series_id)
        if library is None or item is None:
            continue
        root = await session.get(LibraryRoot, library.library_root_id)
        if root is None or not root.enabled or not root.allow_referenced_registrations:
            continue
        if _stamp(file, item, library) != identity["stamp"]:
            continue
        inspected_root_path = root.path
        await session.commit()
        if not await asyncio.to_thread(
            _unchanged_source, file.file_path, file.source_signature, root.path
        ):
            continue
        # Reload ownership after filesystem I/O, before any catalog row is written.
        await raise_if_job_cancelled(session, job.id)
        await session.refresh(file)
        await session.refresh(library)
        await session.refresh(item)
        await session.refresh(root)
        if (
            _stamp(file, item, library) != identity["stamp"]
            or not root.enabled
            or not root.allow_referenced_registrations
            or root.path != inspected_root_path
        ):
            continue
        cv_id = int(target["cv_id"])
        issue_cv_id = int(target["summary"]["provider_id"])
        series = await session.scalar(select(Series).where(Series.comicvine_id == cv_id))
        created_series = series is None
        issue = await session.scalar(select(Issue).where(Issue.comicvine_id == issue_cv_id))
        reparent_issue = issue is not None and (series is None or issue.series_id != series.id)
        if reparent_issue and (
            issue is None
            or issue.id != library.issue_id
            or issue.series_id != item.series_id
            or identity.get("issue_cv_id") != issue.comicvine_id
            or identity["evidence"] != "comicinfo"
        ):
            continue
        if issue is not None and (
            issue.effective_issue_number_text != identity["issue_number"]
            or issue.issue_type.value != str(target["summary"].get("issue_type") or "issue")
        ):
            continue
        if issue is not None and await session.scalar(
            select(LibraryFile.id).where(
                LibraryFile.issue_id == issue.id, LibraryFile.id != library.id
            )
        ):
            continue
        if (
            reparent_issue
            and issue is not None
            and await session.scalar(
                select(ImportedFile.id).where(
                    ImportedFile.matched_issue_id == issue.id,
                    ImportedFile.id != file.id,
                    ImportedFile.status == ImportedFileStatus.IMPORTED,
                )
            )
        ):
            continue
        if await session.scalar(
            select(ImportedFile.id).where(
                ImportedFile.matched_issue_cv_id == issue_cv_id,
                ImportedFile.include_in_import.is_(True),
                ImportedFile.status.in_((ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED)),
            )
        ):
            continue
        if series is not None and (issue is None or reparent_issue):
            existing = list(
                await session.scalars(select(Issue).where(Issue.series_id == series.id))
            )
            if any(row.effective_issue_number_text == identity["issue_number"] for row in existing):
                continue
        if series is None:
            series = await metadata.upsert_series_metadata(
                session,
                cv_id,
                CatalogSeriesMetadata(
                    provider_id=str(cv_id),
                    title=str(target["title"]),
                    sort_title=str(target["title"]),
                    year_start=target.get("year"),
                    year_end=None,
                    status=None,
                    publisher=target.get("publisher"),
                    description=None,
                    cover_url=target.get("cover_url"),
                    issue_count=target.get("issue_count"),
                    comicvine_url=target.get("comicvine_url"),
                ),
            )
            series.monitored = False
            series.issue_catalog_state = IssueCatalogState.PARTIAL
        previous_series_id = issue.series_id if reparent_issue and issue is not None else None
        if reparent_issue and issue is not None:
            # The exact embedded identity and catalog agree, and this is the
            # only registration. Retain the issue ID (including reader state).
            issue.series_id = series.id
        if issue is None:
            payload = target["summary"]
            cutoff = payload.get("source_cutoff_at")
            summary = CatalogIssueSummary(
                source_cutoff_at=datetime.fromisoformat(cutoff) if cutoff else None,
                **{
                    key: payload[key] for key in IssueSummary.__dataclass_fields__ if key in payload
                },
            )
            await metadata.upsert_issue_summaries(session, series, [summary])
            issue = await session.scalar(select(Issue).where(Issue.comicvine_id == issue_cv_id))
        if issue is None or issue.series_id != series.id:
            continue
        resolution = _MixedFolderResolution(
            file_id=file.id,
            source_import_series_id=item.id,
            source_import_series_name=item.raw_series_name,
            target_series_id=series.id,
            target_series_title=series.title,
            target_issue_id=issue.id,
            target_issue_cv_id=issue.comicvine_id,
            target_issue_number=issue.issue_number,
            target_issue_number_text=issue.effective_issue_number_text,
            target_library_file_id=None,
            source_library_file_id=library.id,
            source_issue_id=library.issue_id,
            source_library_updated_at=library.updated_at.isoformat(),
            evidence_source=identity["evidence"],
            source_series_name=identity["query"],
            source_updated_at=file.updated_at.isoformat(),
        )
        affected, _ = await _apply_mixed_folder_resolutions(session, job, resolutions=(resolution,))
        if previous_series_id is not None:
            file.diagnostics = {
                **file.diagnostics,
                "completed_import_cleanup": {
                    **file.diagnostics["completed_import_cleanup"],
                    "source_catalog_series_id": previous_series_id,
                    "issue_identity_preserved": True,
                },
            }
        previous_issue = await session.get(Issue, resolution.source_issue_id)
        if (
            previous_issue is not None
            and previous_issue.comicvine_id is None
            and previous_issue.metadata_source in {"provisional_import", "import_placeholder"}
            and not await session.scalar(
                select(LibraryFile.id).where(LibraryFile.issue_id == previous_issue.id)
            )
        ):
            # Keep the audit/reader row, but do not search for an issue invented
            # from a misplaced filename under the wrong series.
            previous_issue.status = IssueStatus.SKIPPED
        if created_series:
            target_group = await session.get(ImportedSeries, file.import_series_id)
            assert target_group is not None
            target_group.status = ImportSeriesStatus.IMPORTED
        apply_proven_identity(
            file, issue_cv_id=issue_cv_id, series_cv_id=cv_id, summary=target["summary"]
        )
        await refresh_recovered_groups(session, job, affected)
        repaired += 1
        state = dict(job.progress_snapshot.get("deferred_recovery") or {})
        state["reference_files_repaired"] = repaired
        job.progress_snapshot = {**job.progress_snapshot, "deferred_recovery": state}
        await session.commit()
    if progress is not None and plans:
        await progress(len(plans), len(plans))
    return repaired

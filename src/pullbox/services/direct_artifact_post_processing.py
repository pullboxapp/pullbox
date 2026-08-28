"""Adapter from direct quarantine into Pullbox's existing ingest pipeline."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from pullbox.models.download import DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile
from pullbox.models.series import Series
from pullbox.services.direct_artifact_pack import extract_same_series_issue_files
from pullbox.services.direct_artifact_quarantine import validate_direct_artifact
from pullbox.services.issue_import_service import (
    ManualIssueImportResult,
    execute_manual_issue_import,
    prepare_manual_issue_import,
)
from pullbox.tasks.download_task import _run_post_processing

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from sqlalchemy.ext.asyncio import AsyncSession


class _PostProcessor(Protocol):
    def __call__(
        self,
        session: Any,
        download: Any,
        *,
        resolve_local_path: Any,
        cleanup_source: bool,
        allow_resource_safety_exception: bool,
    ) -> Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class _DirectClient:
    value: str = "direct"
    is_torrent: bool = False
    is_usenet: bool = False


@dataclass(slots=True)
class _DirectPostProcessingRecord:
    """Ephemeral download-shaped adapter; it is never added to the database."""

    id: int
    issue_id: int
    title: str
    downloaded_path: str
    replace_existing_file: bool
    download_client: _DirectClient
    download_url: str
    state: DownloadState
    final_path: str | None = None
    imported_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DirectPostProcessingResult:
    """Library linkage returned by the unchanged post-processing pipeline."""

    library_file_id: int
    final_path: Path
    imported_issue_ids: tuple[int, ...] = ()


async def run_direct_artifact_post_processing(
    session: AsyncSession,
    *,
    acquisition_id: int,
    download_history_id: int,
    issue_id: int,
    source_path: Path,
    replace_existing_file: bool,
    allow_resource_safety_exception: bool = False,
    post_processor: _PostProcessor | None = None,
) -> DirectPostProcessingResult:
    """Process one quarantined comic without client path mapping or cleanup."""
    if acquisition_id < 1 or download_history_id < 1 or issue_id < 1:
        raise ValueError("Direct acquisition, download history, and issue IDs must be positive.")
    record = _DirectPostProcessingRecord(
        id=download_history_id,
        issue_id=issue_id,
        title=source_path.name,
        downloaded_path=str(source_path),
        replace_existing_file=replace_existing_file,
        download_client=_DirectClient(),
        download_url=f"direct://attempt/{acquisition_id}",
        state=DownloadState.COMPLETED,
    )
    processor = post_processor or cast("_PostProcessor", _run_post_processing)
    await processor(
        session,
        record,
        resolve_local_path=_resolve_direct_source,
        cleanup_source=False,
        allow_resource_safety_exception=allow_resource_safety_exception,
    )
    await session.flush()
    result = await session.execute(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
    library_file = result.scalar_one_or_none()
    if library_file is None:
        raise RuntimeError("Direct artifact post-processing did not register a library file.")
    final_path = Path(library_file.file_path)
    await asyncio.to_thread(
        _materialize_library_symlink,
        final_path,
        source_path,
    )
    record.imported_at = datetime.now(UTC)
    return DirectPostProcessingResult(
        library_file_id=library_file.id,
        final_path=final_path,
        imported_issue_ids=(issue_id,),
    )


async def run_direct_artifact_pack_post_processing(
    session: AsyncSession,
    *,
    acquisition_id: int,
    download_history_id: int,
    issue_id: int,
    source_path: Path,
    expected_issue_numbers: frozenset[str],
    replace_existing_file: bool,
    allow_resource_safety_exception: bool = False,
) -> DirectPostProcessingResult:
    """Import separable same-series pack members through normal issue ingestion."""
    if acquisition_id < 1 or download_history_id < 1 or issue_id < 1:
        raise ValueError("Direct acquisition, download history, and issue IDs must be positive.")
    initiating_issue_result = await session.execute(
        select(Issue)
        .options(joinedload(Issue.series).joinedload(Series.publisher))
        .where(Issue.id == issue_id)
    )
    initiating_issue = initiating_issue_result.unique().scalar_one_or_none()
    if initiating_issue is None:
        raise RuntimeError("The target issue for this direct-download pack no longer exists.")

    extracted_paths = await asyncio.to_thread(
        extract_same_series_issue_files,
        source_path,
        destination=source_path.parent / "pack-members",
        expected_issue_numbers=expected_issue_numbers,
        expected_series_titles=frozenset(
            (initiating_issue.series.title, *(initiating_issue.series.alternate_names or []))
        ),
    )
    issues_result = await session.execute(
        select(Issue)
        .options(
            joinedload(Issue.series).joinedload(Series.publisher),
            joinedload(Issue.library_file),
        )
        .where(Issue.series_id == initiating_issue.series_id)
    )
    issue_by_number = {issue.issue_number: issue for issue in issues_result.unique().scalars()}
    prepared_imports = []
    for issue_number, file_path in sorted(extracted_paths.items()):
        candidate = issue_by_number.get(issue_number)
        if candidate is None:
            continue
        has_existing_file = getattr(candidate, "library_file", None) is not None
        if has_existing_file and (candidate.id != issue_id or not replace_existing_file):
            continue
        if candidate.id != issue_id and candidate.status not in {
            IssueStatus.WANTED,
            IssueStatus.DOWNLOADING,
        }:
            continue
        await validate_direct_artifact(session, file_path)
        prepared_imports.append(
            await prepare_manual_issue_import(
                session,
                issue_id=candidate.id,
                file_path=str(file_path),
                move_to_library=True,
            )
        )
    if not prepared_imports:
        raise RuntimeError("No wanted issues in this direct-download pack can be imported.")

    imported_results: list[ManualIssueImportResult] = []
    try:
        for prepared in prepared_imports:
            imported = await execute_manual_issue_import(
                session,
                prepared,
                allow_resource_safety_exception=allow_resource_safety_exception,
            )
            imported_results.append(imported)
            await asyncio.to_thread(
                _materialize_library_symlink,
                Path(imported.library_file.file_path),
                prepared.source_path,
            )
    except Exception:
        # The executor rolls database state back. Remove any files copied before a later
        # member failed so a rejected pack never leaves partial library artifacts behind.
        for result in imported_results:
            await asyncio.to_thread(Path(result.library_file.file_path).unlink, missing_ok=True)
        raise
    primary_import_result = next(
        (result for result in imported_results if result.issue_id == issue_id),
        imported_results[0],
    )
    return DirectPostProcessingResult(
        library_file_id=primary_import_result.library_file.id,
        final_path=Path(primary_import_result.library_file.file_path),
        imported_issue_ids=tuple(result.issue_id for result in imported_results),
    )


async def _resolve_direct_source(
    _session: AsyncSession,
    download: _DirectPostProcessingRecord,
) -> str:
    return download.downloaded_path


def _materialize_library_symlink(final_path: Path, source_path: Path) -> None:
    """Replace a direct-import symlink before its quarantine target is removed."""
    if not final_path.is_symlink():
        return
    try:
        target = final_path.resolve(strict=True)
        source = source_path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Direct artifact symlink target is unavailable.") from exc
    if target != source:
        return

    descriptor, temporary_name = tempfile.mkstemp(
        dir=final_path.parent,
        prefix=f".{final_path.name}.pullbox-direct-",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, final_path)
    finally:
        temporary.unlink(missing_ok=True)

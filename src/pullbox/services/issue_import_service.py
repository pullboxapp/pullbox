"""Shared validation and execution helpers for manual issue file imports."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import joinedload

from pullbox.config import get_settings
from pullbox.core.exceptions import NotFoundError
from pullbox.core.file_ops import register_library_file
from pullbox.core.file_safety import get_allowed_extensions
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.core.library_policy import (
    LibraryIngestPolicy,
    load_effective_library_ingest_policy,
    load_library_ingest_policy,
)
from pullbox.core.library_root_resolution import preferred_managed_root_id
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile, MatchConfidence
from pullbox.models.series import Series
from pullbox.services.archive_metadata_binding import ArchiveMetadataBindingError
from pullbox.services.issue_file_service import resolve_configured_utility_trash_dir
from pullbox.services.manual_paired_metadata import (
    materialize_manual_metadata,
    read_manual_metadata_plan,
)
from pullbox.utilities.comicinfo import materialize_cbz_with_comicinfo
from pullbox.utilities.executors.archive_metadata_staging import ArchiveMetadataStagingError
from pullbox.utilities.executors.archive_subprocess import ControlCheck, convert_file_interruptible
from pullbox.utilities.executors.file_converter import convert_file

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.services.direct_pack_paired_metadata import StagedPackMember


class ManualIssueImportError(Exception):
    """Structured validation error for manual issue imports."""

    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(slots=True)
class PreparedManualIssueImport:
    """Validated inputs for one manual issue import run."""

    issue: Issue
    issue_id: int
    source_path: Path
    ingest_policy: LibraryIngestPolicy


@dataclass(slots=True)
class ManualIssueImportResult:
    """Result payload for a completed manual issue import."""

    issue_id: int
    library_file: LibraryFile
    ingest_policy: LibraryIngestPolicy


async def prepare_manual_issue_import(
    session: AsyncSession,
    *,
    issue_id: int,
    file_path: str,
    move_to_library: bool | None,
) -> PreparedManualIssueImport:
    """Load and validate a manual issue-import request."""
    result = await session.execute(
        select(Issue)
        .options(
            joinedload(Issue.series).joinedload(Series.publisher),
            joinedload(Issue.library_file),
        )
        .where(Issue.id == issue_id)
        .execution_options(populate_existing=True)
    )
    issue = result.unique().scalar_one_or_none()
    if issue is None:
        raise NotFoundError("Issue", issue_id)

    if move_to_library is False:
        raise ManualIssueImportError(
            status_code=422,
            detail=(
                "Manual issue import now always creates a library artifact. "
                "The deprecated move_to_library=false override is no longer supported."
            ),
        )

    if "\x00" in file_path:
        raise ManualIssueImportError(
            status_code=400,
            detail="Invalid file path: contains null bytes",
        )

    # Manual imports intentionally accept operator-selected absolute files; the path is
    # resolved strictly and extension-validated before any library mutation happens.
    # codeql[py/path-injection]
    source_path = Path(file_path).expanduser()
    if not source_path.is_absolute():
        raise ManualIssueImportError(
            status_code=400,
            detail="File path must be absolute",
        )
    try:
        source_path = source_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ManualIssueImportError(
            status_code=400,
            detail="File not found on disk",
        ) from None
    if not source_path.is_file():
        raise ManualIssueImportError(
            status_code=400,
            detail="Path is a directory, not a file",
        )

    allowed_extensions = await get_allowed_extensions(session)
    ext = source_path.suffix.lower()
    if ext not in allowed_extensions:
        supported = ", ".join(sorted(allowed_extensions))
        raise ManualIssueImportError(
            status_code=400,
            detail=f"Unsupported format '{ext}'. Supported: {supported}",
        )

    root_id = getattr(getattr(issue, "series", None), "library_root_id", None)
    ingest_policy = (
        await load_effective_library_ingest_policy(session, root_id)
        if root_id is not None
        else await load_library_ingest_policy(session)
    )
    return PreparedManualIssueImport(
        issue=issue,
        issue_id=issue.id,
        source_path=source_path,
        ingest_policy=ingest_policy,
    )


async def execute_manual_issue_import(
    session: AsyncSession,
    prepared: PreparedManualIssueImport,
    *,
    use_paired_metadata: bool = True,
    staged_pack_member: StagedPackMember | None = None,
    allow_resource_safety_exception: bool = False,
    preparation_progress_callback: Callable[[str, int, int, str], Any] | None = None,
    transfer_progress_callback: Callable[[int, int], Any] | None = None,
    comicinfo_progress_callback: Callable[[str, int, int, str], Any] | None = None,
    cancellation_check: ControlCheck | None = None,
) -> ManualIssueImportResult:
    """Import one validated file into the library for the selected issue."""
    paired = staged_pack_member is not None or (
        use_paired_metadata
        and get_settings().metadata_paired_import_writer_enabled
        and prepared.ingest_policy.update_embedded_comicinfo_from_match
    )
    factory = (
        async_sessionmaker(session.bind, expire_on_commit=False)
        if paired and staged_pack_member is None
        else None
    )
    plan = None
    source_signature = None
    if staged_pack_member is not None:
        prepared = await prepare_manual_issue_import(
            session,
            issue_id=prepared.issue_id,
            file_path=str(prepared.source_path),
            move_to_library=True,
        )
        if prepared.ingest_policy != staged_pack_member.plan.policy:
            raise ManualIssueImportError(
                status_code=409, detail="Import settings changed; retry with the current settings."
            )
    if factory is not None:
        if session.new or session.dirty or session.deleted:
            raise ManualIssueImportError(
                status_code=409, detail="Finish pending changes before importing paired metadata."
            )
        prepared = await prepare_manual_issue_import(
            session,
            issue_id=prepared.issue_id,
            file_path=str(prepared.source_path),
            move_to_library=True,
        )
        if not prepared.ingest_policy.update_embedded_comicinfo_from_match:
            raise ManualIssueImportError(
                status_code=409, detail="Import settings changed; retry with the current settings."
            )
        try:
            async with factory() as reader:
                plan = await read_manual_metadata_plan(reader, prepared.issue_id)
            if plan.policy != prepared.ingest_policy:
                raise ValueError("Import settings changed; retry with the current settings.")
            if plan.policy.post_processing_method not in {"copy", "move"}:
                raise ValueError("Paired metadata requires Copy or Move, not linked files.")
        except ArchiveMetadataBindingError as exc:
            from pullbox.services.issue_file_metadata import file_metadata_error

            raise ManualIssueImportError(status_code=409, detail=file_metadata_error(exc)) from None
        except ValueError as exc:
            raise ManualIssueImportError(
                status_code=409,
                detail="Import metadata needs review before retrying. " + str(exc),
            ) from None
        source_signature = await asyncio.to_thread(
            build_file_identity_signature, prepared.source_path
        )

    async def converter_with_progress(
        source: Path,
        target_format: str,
        destination: Path | None = None,
        *,
        allow_resource_safety_exception: bool = False,
    ) -> Path:
        if staged_pack_member is not None:
            return staged_pack_member.staged.path
        if paired:

            async def conversion_progress(stage: str, current: int, total: int, unit: str) -> None:
                if preparation_progress_callback is not None:
                    preparation_progress_callback(stage, current, total, unit)

            return await convert_file_interruptible(
                source,
                target_format,
                destination,
                cancellation_check=cancellation_check,
                progress_callback=conversion_progress,
                allow_resource_safety_exception=allow_resource_safety_exception,
            )
        return await convert_file(
            source,
            target_format,
            destination,
            progress_callback=preparation_progress_callback,
            allow_resource_safety_exception=allow_resource_safety_exception,
        )

    converter = (
        converter_with_progress if paired or preparation_progress_callback is not None else None
    )

    async def materialize_cbz_with_progress(
        source: Path,
        target: Path,
        comicinfo_payload: dict[str, Any],
        *,
        transfer_method: str,
        progress_callback: Callable[[str, int, int, str], Any] | None = None,
    ) -> bool:
        if staged_pack_member is not None:
            return await staged_pack_member.materialize(source, target)
        if factory is not None and plan is not None and source_signature is not None:

            async def paired_progress(stage: str, current: int, total: int, unit: str) -> None:
                if progress_callback is not None:
                    progress_callback(stage, current, total, unit)

            try:
                return await materialize_manual_metadata(
                    factory,
                    plan,
                    prepared.source_path,
                    source_signature,
                    source,
                    target,
                    allow_resource_safety_exception=allow_resource_safety_exception,
                    cancellation_check=cancellation_check,
                    progress_callback=paired_progress,
                )
            except (ArchiveMetadataStagingError, ValueError) as exc:
                if isinstance(exc, ArchiveMetadataStagingError) and str(exc) != "metadata_conflict":
                    raise ManualIssueImportError(
                        status_code=400,
                        detail=(
                            "The source archive could not be prepared safely. "
                            "The source file has not been changed. Check or replace it, then retry."
                        ),
                    ) from None
                raise ManualIssueImportError(
                    status_code=409,
                    detail=(
                        "Import metadata needs review. The source file has not been changed. "
                        "Reconcile its embedded ComicInfo.xml / MetronInfo.xml with the selected "
                        "issue, or choose a corrected source file, then retry."
                    ),
                ) from None
        return bool(
            await asyncio.to_thread(
                materialize_cbz_with_comicinfo,
                source,
                target,
                comicinfo_payload,
                transfer_method=transfer_method,
                progress_callback=progress_callback,
            )
        )

    existing_library_file = getattr(prepared.issue, "__dict__", {}).get("library_file")
    library_file = await register_library_file(
        session,
        source_path=prepared.source_path,
        issue=prepared.issue,
        confidence=MatchConfidence.MANUAL,
        move_to_library=True,
        library_root_id=preferred_managed_root_id(prepared.issue.series),
        loaded_issue=prepared.issue,
        ingest_policy=prepared.ingest_policy,
        transfer_method="copy" if paired else None,
        comicinfo_payload={} if paired else None,
        allow_resource_safety_exception=allow_resource_safety_exception,
        transfer_progress_callback=transfer_progress_callback,
        converter=converter,
        comicinfo_progress_callback=comicinfo_progress_callback,
        comicinfo_materializer=materialize_cbz_with_progress,
        replace_existing_library_file=existing_library_file is not None,
        replacement_trash_dir=await resolve_configured_utility_trash_dir(session)
        if existing_library_file is not None
        else None,
    )
    if paired:
        library_file.has_comicinfo = True

    return ManualIssueImportResult(
        issue_id=prepared.issue_id,
        library_file=library_file,
        ingest_policy=replace(prepared.ingest_policy, post_processing_method="copy")
        if paired
        else prepared.ingest_policy,
    )

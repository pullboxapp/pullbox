"""Run import-owned paired archive writing using the existing publication lifecycle."""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4
from zipfile import ZipFile

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pullbox.core.archive_metadata import MAX_METADATA_BYTES, read_archive_metadata
from pullbox.core.file_safety import (
    get_archive_size_limit_bytes,
    is_dangerous_file_blocking_enabled,
    is_resource_safety_exception_allowed,
)
from pullbox.core.metadata_identity import MetadataSource
from pullbox.models import Issue, LibraryFile, Series
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportJob,
    ImportJobAction,
    ImportJobStatus,
)
from pullbox.models.library import FileFormat
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.series import IssueCatalogState
from pullbox.providers.base import IssueMetadata
from pullbox.providers.metadata.comicvine_normalization import issue as normalize_comicvine_issue
from pullbox.services.archive_metadata_binding import (
    ArchiveMetadataBindingError,
    archive_primary_identity,
    assemble_bound_archive_metadata,
    inspect_archive_metadata_target,
    read_archive_metadata_binding,
)
from pullbox.services.archive_metadata_finalization import finalize_archive_publication
from pullbox.services.archive_metadata_publication import (
    ArchivePublicationError,
    inspect_archive_publication,
    prepare_archive_publication,
    publish_archive_publication,
    record_archive_publication,
)
from pullbox.services.archive_metadata_reconciliation import reconcile_archive_metadata
from pullbox.services.catalog.reader import CatalogIssueMetadata
from pullbox.services.import_archive_publication import bind_import_archive_publication
from pullbox.utilities.executors.archive_metadata_staging import stage_cbz_metadata_interruptible

if TYPE_CHECKING:
    from pullbox.schemas.metadata_sources import ProviderIssueRead


async def write_imported_archive_metadata(
    factory: async_sessionmaker[AsyncSession],
    imported_file_id: int,
    prefetched: Mapping[int, IssueMetadata],
) -> bool:
    """Handle a queued CBZ, or leave other formats to their existing writer.

    This task owns its short sessions. A rejected CBZ never falls back to the
    single-document writer. Reference-only and unverified targets fail closed.
    """
    async with factory() as session:
        file = await session.get(ImportedFile, imported_file_id)
        if file is None or file.library_file_id is None:
            raise ArchivePublicationError("import_owner_missing")
        library = await session.get(LibraryFile, file.library_file_id)
        if library is not None and library.file_format is not FileFormat.CBZ:
            return False
        try:
            binding = await read_archive_metadata_binding(
                session, file.library_file_id, expected_issue_id=file.matched_issue_id
            )
        except ArchiveMetadataBindingError as exc:
            if exc.code == "identity_requires_review" and library is not None:
                provisional = await session.scalar(
                    select(Issue.id)
                    .join(Series, Series.id == Issue.series_id)
                    .where(
                        Issue.id == library.issue_id,
                        Issue.id == file.matched_issue_id,
                        Issue.comicvine_id.is_(None),
                        Series.comicvine_id.is_not(None),
                        Series.issue_catalog_state == IssueCatalogState.HYDRATING,
                        select(SeriesExternalIdentity.id)
                        .where(
                            SeriesExternalIdentity.series_id == Series.id,
                            SeriesExternalIdentity.identity_namespace == "comicvine",
                            SeriesExternalIdentity.external_id == cast(Series.comicvine_id, String),
                            SeriesExternalIdentity.verification_state == "verified",
                        )
                        .exists(),
                        ~select(SeriesExternalIdentity.id)
                        .where(
                            SeriesExternalIdentity.series_id == Series.id,
                            SeriesExternalIdentity.verification_state != "verified",
                        )
                        .exists(),
                        ~select(IssueExternalIdentity.id)
                        .where(IssueExternalIdentity.issue_id == Issue.id)
                        .exists(),
                    )
                )
                if provisional is not None:
                    # Catalog completion will requeue this pending file. Never
                    # fall back to the legacy writer for an unverified target.
                    return True
            raise
        job_id = file.import_job_id
        details = file.diagnostics.get("comicinfo_enrichment", {})
        action_id = details.get("action_id") if isinstance(details, dict) else None
        actions = (
            [action_id]
            if type(action_id) is int and action_id > 0
            else list(
                await session.scalars(
                    select(ImportJobAction.id)
                    .where(
                        ImportJobAction.import_job_id == job_id,
                        ImportJobAction.action_type == "library_file_registered",
                        ImportJobAction.payload["imported_file_id"].as_integer() == file.id,
                    )
                    .limit(2)
                )
            )
        )
        if len(actions) != 1:
            raise ArchivePublicationError("import_owner_missing_or_ambiguous")
        limit = await get_archive_size_limit_bytes(session)
        block_dangerous = await is_dangerous_file_blocking_enabled(session)
        allowed_resource_exception = is_resource_safety_exception_allowed(file.diagnostics)

    async def check_control() -> None:
        async with factory() as session:
            job = await session.get(ImportJob, job_id)
            if (
                job is None
                or job.status is not ImportJobStatus.COMPLETED
                or job.control_request is not ImportControlRequest.NONE
            ):
                raise asyncio.CancelledError

    await check_control()
    target = await inspect_archive_metadata_target(binding)
    files = await asyncio.to_thread(
        read_archive_metadata, target.path, "cbz", max_solid_scan_bytes=MAX_METADATA_BYTES
    )
    archive = reconcile_archive_metadata(files)
    cv_id = binding.metadata.issues[0].comicvine_id
    cached = prefetched.get(cv_id) if cv_id is not None else None
    candidates: tuple[ProviderIssueRead, ...] = ()
    if cached is not None:
        source = (
            MetadataSource.COMICVINE_LOCAL
            if isinstance(cached, CatalogIssueMetadata)
            else MetadataSource.COMICVINE_API
        )
        if any(policy.source is source and policy.enabled for policy in binding.metadata.policies):
            candidates = (normalize_comicvine_issue(source, cached),)
    series, issue = assemble_bound_archive_metadata(
        binding, archive, now=datetime.now(UTC), issue_candidates=candidates
    )
    if allowed_resource_exception:

        def approved_budget() -> int:
            with ZipFile(target.path) as source:
                # The exception applies only to this captured file, not global policy.
                return (
                    sum(member.file_size for member in source.infolist()) + 2 * MAX_METADATA_BYTES
                )

        limit = max(limit, await asyncio.to_thread(approved_budget))
        await asyncio.to_thread(target.check_unchanged)

    async with stage_cbz_metadata_interruptible(
        target.path,
        target.path.parent,
        series,
        issue,
        max_uncompressed_bytes=limit,
        block_dangerous=block_dangerous,
        primary_identity=archive_primary_identity(binding, archive),
        previous_series=binding.metadata.series.baseline,
        previous_issue=binding.metadata.issues[0].baseline,
        cancellation_check=check_control,
    ) as staged:
        plan = await prepare_archive_publication(target, staged, series, issue)
        async with factory() as session:
            plan = await bind_import_archive_publication(
                session, plan, imported_file_id=imported_file_id, action_id=actions[0]
            )
        operation = uuid4()
        async with factory.begin() as session:
            await record_archive_publication(session, plan, operation)
        await check_control()
        async with factory.begin() as session:
            receipt = await publish_archive_publication(session, operation)
        inspection = await inspect_archive_publication(receipt)
        async with factory.begin() as session:
            await finalize_archive_publication(session, receipt, inspection)
    return True

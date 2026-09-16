"""File-operation compatibility helpers for ``ImportService``."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog

from pullbox.services.import_comicinfo_metadata import (
    enrich_issue_for_comicinfo as enrich_import_issue_for_comicinfo,
)
from pullbox.services.import_comicinfo_metadata import (
    issue_needs_comicinfo_enrichment as import_issue_needs_comicinfo_enrichment,
)
from pullbox.services.import_file_interruptible_ops import (
    convert_import_file_interruptible as run_convert_import_file_interruptible,
)
from pullbox.services.import_file_interruptible_ops import (
    embed_import_comicinfo_interruptible as run_embed_import_comicinfo_interruptible,
)
from pullbox.services.import_file_interruptible_ops import (
    materialize_import_cbz_with_comicinfo_interruptible as run_materialize_cbz_with_comicinfo,
)
from pullbox.services.import_file_interruptible_ops import (
    transfer_import_artifact_interruptible as run_transfer_import_artifact_interruptible,
)
from pullbox.services.import_file_preparation import (
    PreparedImportFile,
    apply_comicinfo_to_imported_artifact,
    build_comicinfo_payload_for_issue,
    cleanup_prepared_file,
    format_comicinfo_issue_number,
    inspect_archive_page_count,
    prepare_import_file,
    repaired_cbz_output_path,
    rewrite_import_file_comicinfo,
)
from pullbox.services.import_file_timing_logs import log_import_file_timing_events
from pullbox.services.import_runtime_settings import load_cached_utility_trash_dir
from pullbox.utilities.comicinfo import embed_comicinfo_in_cbz
from pullbox.utilities.executors.file_converter import convert_file

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from typing import Protocol

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedFile, ImportJob
    from pullbox.models.issue import Issue
    from pullbox.services.import_runtime_settings import ImportRuntimeCache
    from pullbox.services.metadata_service import MetadataService

    class ImportServiceFileOperationContext(Protocol):
        _metadata_service: MetadataService
        _settings: Any

        def _import_runtime_cache(self, job_id: int) -> ImportRuntimeCache: ...

        async def _raise_if_job_cancelled_immediately(
            self,
            session: AsyncSession,
            job_id: int,
        ) -> None: ...

        async def _log_event(
            self,
            session: AsyncSession,
            job_id: int,
            level: str,
            event: str,
            message: str | None = None,
            **kwargs: Any,
        ) -> None: ...

        async def _convert_import_file_interruptible(
            self,
            session: AsyncSession,
            job: ImportJob,
            source_path: Path,
            target_format: str,
            destination: Path | None = None,
            progress_callback: Callable[[str, int, int, str], Awaitable[None] | None] | None = None,
            allow_resource_safety_exception: bool = False,
        ) -> Path: ...

        async def _enrich_issue_for_comicinfo(
            self,
            session: AsyncSession,
            issue: Issue,
            *,
            defer_provider_fetch: bool = False,
            propagate_retryable_provider_errors: bool = False,
            timing: dict[str, Any] | None = None,
        ) -> Issue: ...

        async def _build_comicinfo_payload_for_issue(
            self,
            session: AsyncSession,
            issue: Issue,
            *,
            source_path: Path | None = None,
            defer_issue_enrichment: bool = False,
            propagate_retryable_provider_errors: bool = False,
            timing: dict[str, Any] | None = None,
        ) -> dict[str, Any]: ...

else:
    ImportServiceFileOperationContext = object

logger = structlog.get_logger(__name__)


class ImportServiceFileOperationsMixin:
    """Mixin for import file preparation, metadata, and archive operations."""

    async def _load_utility_trash_dir(
        self: ImportServiceFileOperationContext,
        session: AsyncSession,
        job: ImportJob,
    ) -> Path:
        """Resolve the effective utility trash directory for import rollback safety."""
        cache = self._import_runtime_cache(job.id)
        return await load_cached_utility_trash_dir(session, self._settings, job, cache)

    async def _prepare_import_file(
        self: ImportServiceFileOperationContext,
        session: AsyncSession,
        job: ImportJob,
        imp_file: ImportedFile,
        *,
        progress_callback: Callable[[str, int, int, str], Awaitable[None] | None] | None = None,
    ) -> PreparedImportFile:
        """Stage a source file for import, including optional conversion."""

        async def converter(
            source_path: Path,
            target_format: str,
            destination: Path | None = None,
            progress_callback: Callable[[str, int, int, str], Awaitable[None] | None] | None = None,
            allow_resource_safety_exception: bool = False,
        ) -> Path:
            return await self._convert_import_file_interruptible(
                session,
                job,
                source_path,
                target_format,
                destination=destination,
                progress_callback=progress_callback,
                allow_resource_safety_exception=allow_resource_safety_exception,
            )

        return await prepare_import_file(
            job,
            imp_file,
            converter=converter,
            progress_callback=progress_callback,
        )

    async def _convert_import_file_interruptible(
        self: ImportServiceFileOperationContext,
        session: AsyncSession,
        job: ImportJob,
        source_path: Path,
        target_format: str,
        destination: Path | None = None,
        progress_callback: Callable[[str, int, int, str], Awaitable[None] | None] | None = None,
        allow_resource_safety_exception: bool = False,
    ) -> Path:
        """Run a killable archive conversion for the active Step 4 file."""
        return await run_convert_import_file_interruptible(
            session,
            job,
            source_path,
            target_format,
            destination=destination,
            progress_callback=progress_callback,
            allow_resource_safety_exception=allow_resource_safety_exception,
            raise_if_cancelled_immediately=self._raise_if_job_cancelled_immediately,
        )

    async def _embed_import_comicinfo_interruptible(
        self: ImportServiceFileOperationContext,
        session: AsyncSession,
        job: ImportJob,
        artifact_path: Path,
        comicinfo_payload: dict[str, Any],
        *,
        progress_callback: Callable[[str, int, int, str], Awaitable[None] | None] | None = None,
    ) -> bool:
        """Run a killable ComicInfo archive rewrite for the active Step 4 file."""
        return await run_embed_import_comicinfo_interruptible(
            session,
            job,
            artifact_path,
            comicinfo_payload,
            progress_callback=progress_callback,
            raise_if_cancelled_immediately=self._raise_if_job_cancelled_immediately,
        )

    async def _transfer_import_artifact_interruptible(
        self: ImportServiceFileOperationContext,
        session: AsyncSession,
        job: ImportJob,
        source_path: Path,
        target_path: Path,
        transfer_method: str,
        *,
        transfer_progress_callback: Callable[[str, int, int, str], Awaitable[None] | None]
        | None = None,
    ) -> Path:
        """Run a killable library transfer for the active Step 4 file."""
        return await run_transfer_import_artifact_interruptible(
            session,
            job,
            source_path,
            target_path,
            transfer_method,
            transfer_progress_callback=transfer_progress_callback,
            raise_if_cancelled_immediately=self._raise_if_job_cancelled_immediately,
        )

    async def _materialize_import_cbz_with_comicinfo_interruptible(
        self: ImportServiceFileOperationContext,
        session: AsyncSession,
        job: ImportJob,
        source_path: Path,
        target_path: Path,
        comicinfo_payload: dict[str, Any],
        *,
        transfer_method: str,
        temp_path: Path | None = None,
        progress_callback: Callable[[str, int, int, str], Awaitable[None] | None] | None = None,
    ) -> bool:
        """Run a killable combined CBZ materialization and ComicInfo write."""
        return await run_materialize_cbz_with_comicinfo(
            session,
            job,
            source_path,
            target_path,
            comicinfo_payload,
            transfer_method=transfer_method,
            temp_path=temp_path,
            progress_callback=progress_callback,
            raise_if_cancelled_immediately=self._raise_if_job_cancelled_immediately,
        )

    @staticmethod
    def _apply_comicinfo_to_imported_artifact(
        artifact_path: Path,
        comicinfo_payload: dict[str, Any],
    ) -> None:
        """Write authoritative ComicInfo.xml to a final imported library artifact."""
        apply_comicinfo_to_imported_artifact(
            artifact_path,
            comicinfo_payload,
            embedder=embed_comicinfo_in_cbz,
        )

    @staticmethod
    def _cleanup_prepared_file(prepared: PreparedImportFile) -> None:
        """Remove temporary staging directories/files created during import prep."""
        cleanup_prepared_file(prepared)

    @staticmethod
    def _format_comicinfo_issue_number(issue_number: float | None) -> str | None:
        """Render an issue number for ComicInfo.xml output."""
        return format_comicinfo_issue_number(issue_number)

    async def _build_comicinfo_payload_for_issue(
        self: ImportServiceFileOperationContext,
        session: AsyncSession,
        issue: Issue,
        *,
        source_path: Path | None = None,
        defer_issue_enrichment: bool = False,
        propagate_retryable_provider_errors: bool = False,
        timing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build authoritative ComicInfo.xml fields for a chosen issue."""
        page_count = None
        if source_path is not None:
            page_count_started_at = time.monotonic()
            page_count = await asyncio.to_thread(inspect_archive_page_count, source_path)
            if timing is not None:
                timing.update(
                    {
                        "archive_page_count": page_count,
                        "archive_page_count_duration_ms": round(
                            (time.monotonic() - page_count_started_at) * 1000
                        ),
                        "archive_path": str(source_path),
                        "archive_file_name": source_path.name,
                        "archive_size_bytes": source_path.stat().st_size
                        if source_path.exists()
                        else None,
                    }
                )
        enriched_issue = await self._enrich_issue_for_comicinfo(
            session,
            issue,
            defer_provider_fetch=defer_issue_enrichment,
            propagate_retryable_provider_errors=propagate_retryable_provider_errors,
            timing=timing,
        )
        return await build_comicinfo_payload_for_issue(
            session,
            enriched_issue,
            page_count=page_count,
        )

    async def _build_cached_comicinfo_payload_for_issue(
        self: ImportServiceFileOperationContext,
        session: AsyncSession,
        job: ImportJob,
        issue: Issue,
        *,
        source_path: Path | None = None,
        defer_issue_enrichment: bool = False,
    ) -> dict[str, Any]:
        """Build authoritative ComicInfo.xml fields for a chosen issue."""
        cache = self._import_runtime_cache(job.id)
        cache_key = (
            issue.id,
            str(source_path) if source_path is not None else None,
            defer_issue_enrichment,
        )
        payload = cache.comicinfo_payloads.get(cache_key)
        if payload is None:
            timing: dict[str, Any] = {
                "issue_id": issue.id,
                "issue_cv_id": issue.comicvine_id,
                "source_path": str(source_path) if source_path is not None else None,
                "source_file_name": source_path.name if source_path is not None else None,
                "comicvine_issue_fetch_deferred": defer_issue_enrichment,
            }
            payload_started_at = time.monotonic()
            payload = await self._build_comicinfo_payload_for_issue(
                session,
                issue,
                source_path=source_path,
                defer_issue_enrichment=defer_issue_enrichment,
                timing=timing,
            )
            timing["comicinfo_payload_duration_ms"] = round(
                (time.monotonic() - payload_started_at) * 1000
            )
            cache.comicinfo_payloads[cache_key] = payload
            cache.comicinfo_payload_timings[cache_key] = timing
        return payload

    async def _enrich_issue_for_comicinfo(
        self: ImportServiceFileOperationContext,
        session: AsyncSession,
        issue: Issue,
        *,
        defer_provider_fetch: bool = False,
        propagate_retryable_provider_errors: bool = False,
        timing: dict[str, Any] | None = None,
    ) -> Issue:
        """Fetch full issue metadata once when ComicInfo needs authoritative fields."""
        return await enrich_import_issue_for_comicinfo(
            session,
            issue,
            metadata_service=self._metadata_service,
            defer_provider_fetch=defer_provider_fetch,
            propagate_retryable_provider_errors=propagate_retryable_provider_errors,
            timing=timing,
            log_warning=logger.warning,
        )

    async def _log_import_file_timing_events(
        self: ImportServiceFileOperationContext,
        session: AsyncSession,
        job: ImportJob,
        issue: Issue,
        source_path: Path,
        operation_timings: list[dict[str, Any]],
    ) -> None:
        """Write Step 4 timing diagnostics after slow file work is complete."""
        cache = self._import_runtime_cache(job.id)
        source_key = str(source_path)
        metadata_timing = cache.comicinfo_payload_timings.pop(
            (issue.id, source_key, True),
            None,
        )
        if metadata_timing is None:
            metadata_timing = cache.comicinfo_payload_timings.pop(
                (issue.id, source_key, False),
                None,
            )
        await log_import_file_timing_events(
            session,
            job_id=job.id,
            source_file_name=source_path.name,
            metadata_timing=metadata_timing,
            operation_timings=operation_timings,
            log_event=self._log_event,
        )

    @staticmethod
    async def _issue_needs_comicinfo_enrichment(
        session: AsyncSession,
        issue: Issue,
    ) -> bool:
        """Return true when full issue metadata could improve ComicInfo output."""
        return await import_issue_needs_comicinfo_enrichment(session, issue)

    @staticmethod
    def _repaired_cbz_output_path(source_path: Path) -> Path:
        """Return a unique sibling path for a repaired CBZ copy."""
        return repaired_cbz_output_path(source_path)

    async def _rewrite_import_file_comicinfo(
        self,
        imp_file: ImportedFile,
        comicinfo_payload: dict[str, Any],
    ) -> tuple[Path, str, str]:
        """Repair an imported archive's ComicInfo.xml, normalizing to CBZ when needed."""
        return await rewrite_import_file_comicinfo(
            imp_file,
            comicinfo_payload,
            converter=convert_file,
            embedder=embed_comicinfo_in_cbz,
        )

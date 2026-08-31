"""Protocol contracts for import file execution collaborators."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.file_ops import LibraryFileRegistrationOutcome
    from pullbox.core.library_permissions import LibraryPermissionPolicy
    from pullbox.core.library_policy import LibraryIngestPolicy
    from pullbox.models.import_job import ImportedFile, ImportJob, ImportJobAction
    from pullbox.models.issue import Issue
    from pullbox.models.library import LibraryFile, LibraryFileStorageMode, MatchConfidence
    from pullbox.services.import_file_preparation import PreparedImportFile


class LoadMediaSettingsFunc(Protocol):
    async def __call__(self, session: AsyncSession, job: ImportJob) -> dict[str, str]: ...


class LoadTrashDirFunc(Protocol):
    async def __call__(self, session: AsyncSession, job: ImportJob) -> Path: ...


class LoadIngestPolicyFunc(Protocol):
    async def __call__(self, session: AsyncSession, job: ImportJob) -> LibraryIngestPolicy: ...


class LoadPermissionPolicyFunc(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        job: ImportJob,
    ) -> LibraryPermissionPolicy: ...


class RaiseIfCancelledFunc(Protocol):
    async def __call__(self, session: AsyncSession, job_id: int) -> None: ...


class PrepareImportFileFunc(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        job: ImportJob,
        imp_file: ImportedFile,
        *,
        progress_callback: Callable[[str, int, int, str], Awaitable[None] | None] | None = None,
    ) -> PreparedImportFile: ...


class BuildComicInfoPayloadFunc(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        issue: Issue,
        *,
        source_path: Path | None = None,
        defer_issue_enrichment: bool = False,
    ) -> dict[str, Any]: ...


class ApplyComicInfoFunc(Protocol):
    def __call__(self, artifact_path: Path, comicinfo_payload: dict[str, Any]) -> None: ...


class CleanupPreparedFileFunc(Protocol):
    def __call__(self, prepared: PreparedImportFile) -> None: ...


class RecordActionFunc(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        job: ImportJob,
        *,
        phase: str,
        action_type: str,
        payload: dict[str, Any],
    ) -> ImportJobAction: ...


class LogEventFunc(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        *,
        message: str,
        **details: Any,
    ) -> None: ...


class RegisterLibraryFileFunc(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        source_path: Path,
        issue: Issue,
        confidence: MatchConfidence,
        *,
        move_to_library: bool,
        storage_mode: LibraryFileStorageMode,
        expected_source_signature: dict[str, object] | None,
        library_root_id: int | None,
        transfer_method: str | None,
        normalize_to_cbz: bool | None = None,
        update_embedded_comicinfo_from_match: bool | None = None,
        comicinfo_payload: dict[str, Any] | None = None,
        loaded_issue: Issue | None = None,
        ingest_policy: LibraryIngestPolicy | None = None,
        permission_policy: LibraryPermissionPolicy | None = None,
        transfer_progress_callback: Callable[[str, int, int, str], Awaitable[None] | None]
        | None = None,
        comicinfo_progress_callback: Callable[[str, int, int, str], Awaitable[None] | None]
        | None = None,
        placement_started_callback: Callable[..., Awaitable[None] | None] | None = None,
    ) -> LibraryFile | LibraryFileRegistrationOutcome: ...


class MoveToTrashFunc(Protocol):
    def __call__(
        self,
        source: Path,
        trash_dir: Path,
        *,
        relative_path: str | Path | None = None,
    ) -> Path: ...


class ReportFileProgressFunc(Protocol):
    async def __call__(
        self,
        *,
        imp_file: ImportedFile,
        file_index: int,
        total_files: int,
        stage: str,
        current: int,
        total: int,
        unit: str,
        live_only: bool = False,
    ) -> None: ...

"""File integrity checker executor — verify comic archive integrity.

Supports quick scan (archive opens, files countable) and deep scan
(every image decoded and verified). Exports standalone
check_file_integrity() for download post-processing reuse.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pullbox.core.filesystem_scan import iter_supported_files
from pullbox.utilities.base_executor import (
    ApplyResult,
    ExecutionMode,
    ItemResult,
    JobExecutor,
    JobRunSummary,
    ProcessedItem,
    RuntimeLogEntry,
)
from pullbox.utilities.executors.integrity_checks import (
    SCAN_EXTENSIONS as _SCAN_EXTENSIONS,
)
from pullbox.utilities.executors.integrity_checks import (
    IntegrityResult,
    _check_sync,
    check_file_integrity,
)
from pullbox.utilities.logging_config import persist_runtime_utility_log
from pullbox.utilities.settings import (
    move_file_to_utility_trash,
    resolve_utility_directory,
    restore_file_from_utility_trash,
)

logger = structlog.get_logger(__name__)
_background_search_tasks: set[asyncio.Task[None]] = set()
__all__ = ["IntegrityCheckerExecutor", "IntegrityResult", "check_file_integrity"]

_VALID_CORRUPT_ACTIONS = frozenset({"report", "quarantine"})


def _resolve_effective_trash_directory(trash_folder: str | None) -> Path:
    from pullbox.config import get_settings

    settings = get_settings()
    return resolve_utility_directory(
        db_value=(trash_folder or "").strip(),
        default_parent=settings.library_root,
        default_subdir=".trash",
        library_root=settings.library_root,
        data_dir=settings.data_dir,
    )


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def _common_parent(paths: list[Path]) -> Path | None:
    existing = [path.parent for path in paths if path.exists()]
    if not existing:
        return None
    return Path(os.path.commonpath([str(path) for path in existing]))


def _relative_trash_path(source: Path, relative_to: Path | None) -> str:
    if relative_to is None:
        return source.name
    try:
        return str(source.relative_to(relative_to))
    except ValueError:
        return source.name


class IntegrityCheckerExecutor(JobExecutor):
    """Job queue executor for batch file integrity checking."""

    _SCAN_EXTENSIONS = _SCAN_EXTENSIONS
    execution_mode = ExecutionMode.PROCESS

    async def build_job_context(
        self,
        session: Any,
        job_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Preload tracked library-file metadata using the caller's session."""
        from pullbox.models.library import LibraryFile

        result = await session.execute(
            select(LibraryFile)
            .options(selectinload(LibraryFile.library_root))
            .order_by(LibraryFile.file_path)
        )

        tracked_files: dict[str, dict[str, Any]] = {}
        library_items: list[dict[str, Any]] = []
        for library_file in result.scalars():
            root_path = (
                library_file.library_root.path if library_file.library_root is not None else None
            )
            payload = {
                "library_file_id": library_file.id,
                "library_root_path": root_path,
                "storage_mode": library_file.storage_mode.value,
            }
            tracked_files[library_file.file_path] = payload
            library_items.append(
                {
                    "file_path": library_file.file_path,
                    "library_file_id": library_file.id,
                    "library_root_path": root_path,
                    "storage_mode": library_file.storage_mode.value,
                }
            )

        return {
            "tracked_files": tracked_files,
            "library_items": library_items,
        }

    def validate_config(self, job_config: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        depth = str(job_config.get("scan_depth", "quick") or "quick")
        if depth not in {"quick", "deep"}:
            errors.append(f"Invalid scan_depth: {depth}. Must be 'quick' or 'deep'")

        corrupt_action = str(job_config.get("corrupt_action", "report") or "report").lower()
        if corrupt_action not in _VALID_CORRUPT_ACTIONS:
            errors.append("corrupt_action must be one of: report, quarantine")

        requeue_search = job_config.get("requeue_search", False)
        if not isinstance(requeue_search, bool):
            errors.append("requeue_search must be true or false")

        if corrupt_action == "quarantine":
            try:
                _resolve_effective_trash_directory(str(job_config.get("trash_folder", "") or ""))
            except Exception as exc:
                errors.append(f"Invalid trash_folder: {exc}")
        return errors

    async def _enrich_paths_with_library_data(
        self,
        candidate_paths: list[Path],
        job_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not candidate_paths:
            return []

        tracked_files = (
            (job_context or {}).get("tracked_files", {})
            if isinstance((job_context or {}).get("tracked_files", {}), dict)
            else {}
        )

        items: list[dict[str, Any]] = []
        for path in candidate_paths:
            item: dict[str, Any] = {
                "file_path": str(path),
                "operation": "check",
            }
            tracked = tracked_files.get(str(path))
            if isinstance(tracked, dict):
                library_file_id = tracked.get("library_file_id")
                if isinstance(library_file_id, int):
                    item["library_file_id"] = library_file_id
                library_root_path = tracked.get("library_root_path")
                if isinstance(library_root_path, str) and library_root_path:
                    item["trash_relative_path"] = _relative_trash_path(
                        path,
                        Path(library_root_path),
                    )
                storage_mode = tracked.get("storage_mode")
                if isinstance(storage_mode, str):
                    item["storage_mode"] = storage_mode
            items.append(item)
        return items

    async def generate_items(
        self,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Discover files to check based on scope."""
        scope = str(job_config.get("scope", "manual") or "manual").strip().lower()
        candidate_paths: list[Path] = []
        folder_roots: list[Path] = []
        trash_dir = _resolve_effective_trash_directory(
            str(job_config.get("trash_folder", "") or "")
        )

        if scope == "manual":
            for path_str in job_config.get("file_paths", []):
                path = Path(str(path_str))
                if (
                    path.exists()
                    and path.is_file()
                    and path.suffix.lower() in self._SCAN_EXTENSIONS
                ):
                    candidate_paths.append(path)

        elif scope == "folder":
            scan_folders = [
                Path(str(path).strip())
                for path in job_config.get("scan_folders", [])
                if str(path).strip()
            ]
            if not scan_folders:
                scan_folder = str(job_config.get("scan_folder", "")).strip()
                if scan_folder:
                    scan_folders = [Path(scan_folder)]

            folder_roots = scan_folders
            for folder in scan_folders:
                if not folder.is_dir():
                    continue
                for file_path in iter_supported_files(folder, self._SCAN_EXTENSIONS):
                    if _is_relative_to(file_path, trash_dir):
                        continue
                    candidate_paths.append(file_path)

        elif scope == "library":
            context_items = (job_context or {}).get("library_items", [])
            if not isinstance(context_items, list):
                context_items = []
            items: list[dict[str, Any]] = []
            for raw_item in context_items:
                if not isinstance(raw_item, dict):
                    continue
                file_path_value = raw_item.get("file_path")
                library_file_id = raw_item.get("library_file_id")
                if not isinstance(file_path_value, str) or not isinstance(library_file_id, int):
                    continue
                path = Path(file_path_value)
                if (
                    not path.exists()
                    or not path.is_file()
                    or path.suffix.lower() not in self._SCAN_EXTENSIONS
                    or _is_relative_to(path, trash_dir)
                ):
                    continue
                item: dict[str, Any] = {
                    "file_path": file_path_value,
                    "operation": "check",
                    "library_file_id": library_file_id,
                }
                storage_mode = raw_item.get("storage_mode")
                if isinstance(storage_mode, str):
                    item["storage_mode"] = storage_mode
                library_root_path = raw_item.get("library_root_path")
                if isinstance(library_root_path, str) and library_root_path:
                    item["trash_relative_path"] = _relative_trash_path(
                        path,
                        Path(library_root_path),
                    )
                items.append(item)
            return items

        deduped_paths = list(dict.fromkeys(candidate_paths))
        items = await self._enrich_paths_with_library_data(deduped_paths, job_context)

        relative_to: Path | None = None
        if scope == "folder":
            if len(folder_roots) == 1:
                relative_to = folder_roots[0]
            elif folder_roots:
                relative_to = Path(os.path.commonpath([str(path) for path in folder_roots]))
        elif scope == "manual":
            relative_to = _common_parent(deduped_paths)

        for item in items:
            if item.get("trash_relative_path"):
                continue
            item["trash_relative_path"] = _relative_trash_path(
                Path(str(item["file_path"])), relative_to
            )
        return items

    def process_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        """Check a single file's integrity."""
        start = time.monotonic()
        item_id = str(item_data.get("id", "unknown") or "unknown")
        file_path = str(item_data.get("file_path", "") or "")
        deep = str(job_config.get("scan_depth", "quick") or "quick") == "deep"
        corrupt_action = str(job_config.get("corrupt_action", "report") or "report").lower()
        is_referenced = item_data.get("storage_mode") == "referenced"

        try:
            path = Path(file_path)
            result = _check_sync(path, deep)
            duration_ms = int((time.monotonic() - start) * 1000)

            item_result = (
                ItemResult.COMPLETED
                if result.status in {"healthy", "warning"}
                else ItemResult.FAILED
            )
            warning_message = "; ".join(result.warnings) if result.warnings else None
            error_message = "; ".join(result.errors) if result.errors else None
            hash_info = f", hash: {result.file_hash[:12]}..." if result.file_hash else ""
            before_state: dict[str, Any] = {"path": file_path}
            after_state: dict[str, Any] = {
                "status": result.status,
                "page_count": result.page_count,
                "file_hash": result.file_hash,
                "warnings": result.warnings,
                "errors": result.errors,
                "action": "report",
            }
            log_entries: list[tuple[str, str, dict[str, Any]]] = []

            if result.status == "healthy":
                log_entries.append(
                    ("INFO", f"{path.name}: healthy ({result.page_count} pages{hash_info})", {})
                )
            elif result.status == "warning":
                log_entries.append(
                    (
                        "WARNING",
                        f"{path.name}: warnings detected ({result.page_count} pages{hash_info})",
                        {},
                    )
                )
                for warning in result.warnings:
                    log_entries.append(("WARNING", f"  {warning}", {}))
            else:
                error_detail = "; ".join(result.errors) if result.errors else "Unknown error"
                if corrupt_action == "quarantine" and not is_referenced:
                    trash_dir = _resolve_effective_trash_directory(
                        str(job_config.get("trash_folder", "") or "")
                    )
                    trash_path = move_file_to_utility_trash(
                        path,
                        trash_dir,
                        relative_path=item_data.get("trash_relative_path"),
                    )
                    item_result = ItemResult.COMPLETED
                    warning_message = f"Corrupt file quarantined: {path.name}"
                    error_message = None
                    after_state["action"] = "quarantine"
                    after_state["trash_path"] = str(trash_path)
                    log_entries.append(("ERROR", f"{path.name}: CORRUPT — {error_detail}", {}))
                    log_entries.append(("ERROR", f"  Path: {file_path}", {}))
                    log_entries.append(
                        (
                            "WARNING",
                            f"  Moved corrupt file to utility trash: {trash_path}",
                            {"trash_path": str(trash_path)},
                        )
                    )
                else:
                    if corrupt_action == "quarantine" and is_referenced:
                        warning_message = (
                            "Referenced file was reported as corrupt but left unchanged on disk."
                        )
                        after_state["quarantine_blocked_reason"] = "referenced_file"
                        log_entries.append(
                            (
                                "WARNING",
                                "  Quarantine skipped because this is a referenced library file.",
                                {"reason": "referenced_file"},
                            )
                        )
                    log_entries.append(("ERROR", f"{path.name}: CORRUPT — {error_detail}", {}))
                    log_entries.append(("ERROR", f"  Path: {file_path}", {}))
                    log_entries.append(
                        (
                            "INFO",
                            "  Fix: Re-download this file or restore from backup. "
                            "If the file is a mislabeled format, try converting it "
                            "with the File Type Converter utility.",
                            {},
                        )
                    )

            return ProcessedItem(
                item_id=item_id,
                result=item_result,
                before_state=before_state,
                after_state=after_state,
                duration_ms=duration_ms,
                warning_message=warning_message,
                error_message=error_message,
                log_entries=log_entries,
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.FAILED,
                before_state={"path": file_path},
                duration_ms=duration_ms,
                error_message=str(exc),
                log_entries=[
                    ("ERROR", f"{Path(file_path).name}: FAILED — {exc}", {}),
                    ("ERROR", f"  Path: {file_path}", {}),
                    (
                        "INFO",
                        "  Fix: Re-download this file or restore from backup. "
                        "If the file is a mislabeled format, try converting it "
                        "with the File Type Converter utility.",
                        {},
                    ),
                ],
            )

    def rollback_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        """Rollback restores a quarantined file from utility trash when needed."""
        item_id = str(item_data.get("id", "unknown") or "unknown")
        start = time.monotonic()
        try:
            before_state = item_data.get("before_state", {})
            after_state = item_data.get("after_state", {})
            if isinstance(before_state, str):
                before_state = json.loads(before_state)
            if isinstance(after_state, str):
                after_state = json.loads(after_state)

            if str(after_state.get("action", "report") or "report") != "quarantine":
                return ProcessedItem(
                    item_id=item_id,
                    result=ItemResult.COMPLETED,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    log_entries=[("INFO", "Integrity metadata rolled back", {})],
                )

            original_path_str = str(before_state.get("path", "") or "")
            trash_path_str = str(after_state.get("trash_path", "") or "")
            if not original_path_str or not trash_path_str:
                raise FileNotFoundError("Rollback metadata missing original or trash path")

            original_path = Path(original_path_str)
            trash_path = Path(trash_path_str)
            restore_file_from_utility_trash(trash_path, original_path)
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.COMPLETED,
                duration_ms=int((time.monotonic() - start) * 1000),
                log_entries=[
                    (
                        "INFO",
                        f"Restored quarantined file: {original_path.name}",
                        {
                            "restored_path": str(original_path),
                            "trash_path": str(trash_path),
                        },
                    )
                ],
            )
        except Exception as exc:
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.FAILED,
                duration_ms=int((time.monotonic() - start) * 1000),
                error_message=f"Rollback failed: {exc}",
                log_entries=[("ERROR", f"Rollback failed: {exc}", {})],
            )

    @staticmethod
    def _snapshot_library_file(library_file: Any) -> dict[str, Any]:
        return {
            "id": library_file.id,
            "file_path": library_file.file_path,
            "file_name": library_file.file_name,
            "file_size": library_file.file_size,
            "file_format": str(library_file.file_format),
            "file_hash": library_file.file_hash,
            "file_modified_at": (
                library_file.file_modified_at.isoformat()
                if library_file.file_modified_at is not None
                else None
            ),
            "match_confidence": str(library_file.match_confidence),
            "parsed_series": library_file.parsed_series,
            "parsed_issue_number": library_file.parsed_issue_number,
            "parsed_year": library_file.parsed_year,
            "parsed_publisher": library_file.parsed_publisher,
            "has_comicinfo": bool(library_file.has_comicinfo),
            "issue_id": library_file.issue_id,
            "library_root_id": library_file.library_root_id,
            "storage_mode": str(library_file.storage_mode),
            "source_signature": dict(library_file.source_signature or {}),
        }

    @staticmethod
    def _snapshot_issue(issue: Any) -> dict[str, Any]:
        return {
            "id": issue.id,
            "status": str(issue.status),
            "integrity_status": issue.integrity_status,
            "integrity_checked_at": issue.integrity_checked_at,
            "integrity_details": issue.integrity_details,
        }

    async def apply_item_result(
        self,
        session: Any,
        item: Any,
        item_data: dict[str, Any],
        processed: ProcessedItem,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None,
        summary: JobRunSummary,
    ) -> ApplyResult:
        from pullbox.models.issue import Issue, IssueStatus
        from pullbox.models.library import LibraryFile
        from pullbox.services.library_delete_service import _status_after_library_file_removed

        after_state = processed.after_state or {}
        if not after_state:
            return ApplyResult()

        library_file_id = item_data.get("library_file_id")
        original_path = str(item_data.get("file_path", "") or "")
        stmt = select(LibraryFile).options(
            selectinload(LibraryFile.issue).selectinload(Issue.series)
        )
        if library_file_id is not None:
            stmt = stmt.where(LibraryFile.id == library_file_id)
        else:
            stmt = stmt.where(LibraryFile.file_path == original_path)

        library_file = (await session.execute(stmt)).scalar_one_or_none()
        before_state = json.loads(item.before_state or "{}") if item.before_state else {}
        before_state.setdefault("path", original_path)

        if library_file is None:
            item.before_state = json.dumps(before_state)
            return ApplyResult()

        before_state.setdefault(
            "library_file_snapshot",
            self._snapshot_library_file(library_file),
        )

        issue = library_file.issue
        if issue is not None:
            before_state.setdefault("issue_snapshot", self._snapshot_issue(issue))
            issue.integrity_status = str(after_state.get("status") or "unchecked")
            issue.integrity_checked_at = datetime.now(UTC).isoformat()
            issue.integrity_details = json.dumps(
                {
                    "action": str(after_state.get("action") or "report"),
                    "errors": list(after_state.get("errors") or []),
                    "warnings": list(after_state.get("warnings") or []),
                    "file_path": original_path,
                    "page_count": int(after_state.get("page_count") or 0),
                    "trash_path": str(after_state.get("trash_path") or ""),
                }
            )

        file_hash = after_state.get("file_hash")
        if file_hash:
            library_file.file_hash = str(file_hash)

        item.before_state = json.dumps(before_state)

        if (
            str(after_state.get("status") or "") == "corrupt"
            and str(after_state.get("action") or "report") == "quarantine"
        ):
            search_series_id: int | None = None
            if issue is not None:
                issue.status = _status_after_library_file_removed(issue)
                if (
                    bool(job_config.get("requeue_search"))
                    and issue.status == IssueStatus.WANTED
                    and issue.series_id is not None
                ):
                    search_series_id = int(issue.series_id)
            await session.delete(library_file)
            payload: dict[str, Any] = {}
            if search_series_id is not None:
                payload["search_series_id"] = search_series_id
            return ApplyResult(
                extra_logs=[
                    RuntimeLogEntry(
                        level="INFO",
                        message="Quarantined corrupt tracked file and removed it from Library.",
                        file_path=original_path,
                        extra={
                            "action": "quarantine",
                            "trash_path": str(after_state.get("trash_path") or ""),
                        },
                    )
                ],
                post_commit_payload=payload,
            )

        return ApplyResult()

    @staticmethod
    async def apply_rollback_result(
        session: Any,
        item_data: dict[str, Any],
        processed: ProcessedItem,
    ) -> ApplyResult:
        from pullbox.models.issue import Issue, IssueStatus
        from pullbox.models.library import (
            FileFormat,
            LibraryFile,
            LibraryFileStorageMode,
            MatchConfidence,
        )

        if processed.result != ItemResult.COMPLETED:
            return ApplyResult()

        before_state = item_data.get("before_state", {})
        if isinstance(before_state, str):
            before_state = json.loads(before_state or "{}")

        issue_snapshot = before_state.get("issue_snapshot")
        if isinstance(issue_snapshot, dict):
            issue = await session.get(Issue, issue_snapshot.get("id"))
            if issue is not None:
                issue.status = IssueStatus(str(issue_snapshot.get("status") or IssueStatus.SKIPPED))
                issue.integrity_status = issue_snapshot.get("integrity_status")
                issue.integrity_checked_at = issue_snapshot.get("integrity_checked_at")
                issue.integrity_details = issue_snapshot.get("integrity_details")

        library_file_snapshot = before_state.get("library_file_snapshot")
        if not isinstance(library_file_snapshot, dict):
            return ApplyResult()

        library_file = await session.get(LibraryFile, library_file_snapshot.get("id"))
        file_modified_at = library_file_snapshot.get("file_modified_at")
        modified_at = (
            datetime.fromisoformat(str(file_modified_at)) if file_modified_at else datetime.now(UTC)
        )
        kwargs = {
            "file_path": str(library_file_snapshot.get("file_path") or ""),
            "file_name": str(library_file_snapshot.get("file_name") or ""),
            "file_size": int(library_file_snapshot.get("file_size") or 0),
            "file_format": FileFormat(str(library_file_snapshot.get("file_format") or "cbz")),
            "file_hash": library_file_snapshot.get("file_hash"),
            "file_modified_at": modified_at,
            "match_confidence": MatchConfidence(
                str(library_file_snapshot.get("match_confidence") or "unmatched")
            ),
            "parsed_series": library_file_snapshot.get("parsed_series"),
            "parsed_issue_number": library_file_snapshot.get("parsed_issue_number"),
            "parsed_year": library_file_snapshot.get("parsed_year"),
            "parsed_publisher": library_file_snapshot.get("parsed_publisher"),
            "has_comicinfo": bool(library_file_snapshot.get("has_comicinfo")),
            "issue_id": library_file_snapshot.get("issue_id"),
            "library_root_id": library_file_snapshot.get("library_root_id"),
            "storage_mode": LibraryFileStorageMode(
                str(library_file_snapshot.get("storage_mode") or "managed")
            ),
            "source_signature": dict(library_file_snapshot.get("source_signature") or {}),
        }
        if library_file is None:
            session.add(
                LibraryFile(
                    id=str(library_file_snapshot.get("id")),
                    **kwargs,
                )
            )
            return ApplyResult()

        for key, value in kwargs.items():
            setattr(library_file, key, value)
        return ApplyResult()

    async def after_item_commit(
        self,
        item_data: dict[str, Any],
        processed: ProcessedItem,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None,
        summary: JobRunSummary,
        apply_result: ApplyResult,
    ) -> list[RuntimeLogEntry]:
        search_series_id = apply_result.post_commit_payload.get("search_series_id")
        if search_series_id is None:
            return []

        _schedule_replacement_search(
            configured_level=str(summary.metadata.get("utility_log_level", "INFO")),
            job_id=str(summary.metadata.get("job_id", "")),
            item_id=str(item_data.get("id", "") or ""),
            file_path=str(item_data.get("file_path", "") or ""),
            series_id=int(search_series_id),
            worker_id=processed.worker_id,
            duration_ms=processed.duration_ms,
        )
        return [
            RuntimeLogEntry(
                level="INFO",
                message="Queued replacement search for wanted issue.",
                file_path=str(item_data.get("file_path", "") or ""),
                extra={"series_id": int(search_series_id)},
            )
        ]


def _schedule_replacement_search(
    *,
    configured_level: str,
    job_id: str,
    item_id: str,
    file_path: str,
    series_id: int,
    worker_id: int | None,
    duration_ms: int,
) -> None:
    async def _run() -> None:
        from pullbox.database import get_session_factory
        from pullbox.tasks.search_task import search_series_issues

        level = "INFO"
        message = "Replacement search finished."
        extra: dict[str, Any] = {"series_id": series_id}
        try:
            result = await search_series_issues(series_id)
            extra["search_result"] = result
            message = (
                "Replacement search started "
                f"(wanted={result.get('wanted', 0)}, sent={result.get('sent', 0)}, "
                f"queued={result.get('queued', 0)})."
            )
        except Exception as exc:
            level = "WARNING"
            message = f"Replacement search could not be started: {exc}"
            extra["error"] = str(exc)

        factory = get_session_factory()
        async with factory() as session:
            persist_runtime_utility_log(
                session,
                configured_level=configured_level,
                job_id=job_id,
                item_id=item_id,
                level=level,
                message=message,
                file_path=file_path,
                extra=extra,
                worker_id=worker_id,
                duration_ms=duration_ms,
            )
            await session.commit()

    task = asyncio.create_task(_run())
    _background_search_tasks.add(task)
    task.add_done_callback(_background_search_tasks.discard)

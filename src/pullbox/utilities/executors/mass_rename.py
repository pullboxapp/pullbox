"""Mass rename executor — rename folders and files using naming templates.

Renames comic folders and files to standardized names derived from
the project's naming templates. Supports conflict detection, case-only
renames (via temp-rename strategy), forbidden character sanitization,
and rollback.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from pullbox.models.library import LibraryFile, LibraryFileStorageMode
from pullbox.services.library_rename_service import _sync_file_record, _sync_folder_records
from pullbox.utilities.base_executor import (
    ApplyResult,
    ExecutionMode,
    ItemResult,
    JobExecutor,
    JobRunSummary,
    ProcessedItem,
    RuntimeLogEntry,
)

logger = structlog.get_logger(__name__)

# Characters illegal in file/folder names across Windows, macOS, Linux.
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_MAX_NAME_LENGTH = 255


def _sanitize_name(name: str) -> str:
    """Remove forbidden filesystem characters and trim whitespace."""
    sanitized = _ILLEGAL_CHARS.sub("", name)
    sanitized = re.sub(r"\s{2,}", " ", sanitized).strip()
    sanitized = sanitized.rstrip(". ")
    return sanitized


def _path_would_mutate_reference(
    path: Path,
    job_context: dict[str, Any] | None,
) -> bool:
    referenced_paths = (job_context or {}).get("referenced_paths", [])
    if not isinstance(referenced_paths, list):
        return False
    resolved_path = path.expanduser().resolve(strict=False)
    for referenced_value in referenced_paths:
        referenced = Path(str(referenced_value)).expanduser().resolve(strict=False)
        if referenced == resolved_path or referenced.is_relative_to(resolved_path):
            return True
    return False


class MassRenameExecutor(JobExecutor):
    """Executor for batch folder/file renames using naming templates."""

    execution_mode = ExecutionMode.THREAD

    async def build_job_context(
        self,
        session: Any,
        job_config: dict[str, Any],
    ) -> dict[str, Any]:
        _ = job_config
        referenced_paths = list(
            (
                await session.execute(
                    select(LibraryFile.file_path).where(
                        LibraryFile.storage_mode == LibraryFileStorageMode.REFERENCED
                    )
                )
            )
            .scalars()
            .all()
        )
        return {"referenced_paths": referenced_paths}

    def validate_config(self, job_config: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        target = job_config.get("target")
        if not target:
            errors.append("target is required ('folders' or 'files')")
        elif target not in ("folders", "files"):
            errors.append(f"Invalid target: {target}. Must be 'folders' or 'files'")
        if not job_config.get("template"):
            errors.append("template is required")
        return errors

    async def generate_items(
        self,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Discover items to rename based on scope."""
        explicit_items = job_config.get("items", [])
        if isinstance(explicit_items, list) and explicit_items:
            items: list[dict[str, Any]] = []
            for item in explicit_items:
                if not isinstance(item, dict):
                    continue
                file_path = str(item.get("file_path", "")).strip()
                if not file_path:
                    continue
                items.append(
                    {
                        "file_path": file_path,
                        "operation": str(item.get("operation", "rename") or "rename"),
                        "proposed_name": str(item.get("proposed_name", "") or ""),
                    }
                )
            return items

        scope = job_config.get("scope", "manual")
        discovered: list[dict[str, Any]] = []

        if scope == "manual":
            for path_str in job_config.get("file_paths", []):
                path = Path(path_str)
                if path.exists():
                    discovered.append(
                        {
                            "file_path": str(path),
                            "operation": "rename",
                        }
                    )

        return discovered

    def process_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        """Rename a single file or folder."""
        start = time.monotonic()
        item_id = item_data.get("id", "unknown")
        file_path = item_data.get("file_path", "")
        proposed_name = item_data.get("proposed_name", "")
        source = Path(file_path)

        try:
            if _path_would_mutate_reference(source, job_context):
                return ProcessedItem(
                    item_id=item_id,
                    result=ItemResult.SKIPPED,
                    before_state={"path": str(source)},
                    after_state={"path": str(source), "reason": "referenced_file"},
                    duration_ms=int((time.monotonic() - start) * 1000),
                    warning_message="Referenced library files cannot be renamed.",
                    log_entries=[
                        (
                            "WARNING",
                            f"Skipped referenced library path: {source}",
                            {"reason": "referenced_file"},
                        )
                    ],
                )
            # Validate source exists
            if not source.exists():
                raise FileNotFoundError(f"Source not found: {source}")

            # Validate proposed name
            if not proposed_name or not proposed_name.strip():
                return ProcessedItem(
                    item_id=item_id,
                    result=ItemResult.SKIPPED,
                    before_state={"path": str(source)},
                    after_state={"path": str(source)},
                    duration_ms=int((time.monotonic() - start) * 1000),
                    warning_message="Empty proposed name",
                    log_entries=[("WARNING", "Skipped: empty proposed name", {})],
                )

            # Sanitize the proposed name
            sanitized = _sanitize_name(proposed_name)

            # Check name length
            if len(sanitized) > _MAX_NAME_LENGTH:
                raise ValueError(
                    f"Name exceeds filesystem limit ({len(sanitized)} > {_MAX_NAME_LENGTH} chars)"
                )

            target = source.parent / sanitized

            # No-change detection
            if source.name == sanitized:
                return ProcessedItem(
                    item_id=item_id,
                    result=ItemResult.SKIPPED,
                    before_state={"path": str(source)},
                    after_state={"path": str(source)},
                    duration_ms=int((time.monotonic() - start) * 1000),
                    log_entries=[("INFO", f"No change needed: {source.name}", {})],
                )

            # Case-only rename detection (e.g., "batman" → "Batman")
            is_case_only = source.name.lower() == sanitized.lower()

            if not is_case_only and target.exists():
                raise FileExistsError(f"Target already exists: {target}")

            # Perform rename
            if is_case_only:
                # Two-step rename for case-insensitive filesystems
                tmp_name = source.parent / f".rename_tmp_{os.urandom(8).hex()}"
                source.rename(tmp_name)
                tmp_name.rename(target)
            else:
                source.rename(target)

            duration_ms = int((time.monotonic() - start) * 1000)
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.COMPLETED,
                before_state={"path": str(source), "name": source.name},
                after_state={"path": str(target), "name": sanitized},
                duration_ms=duration_ms,
                log_entries=[
                    ("INFO", f"Renamed: {source.name} \u2192 {sanitized}", {}),
                ],
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
                    ("ERROR", f"Rename failed for {file_path}: {exc}", {}),
                ],
            )

    def rollback_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        """Restore original name."""
        start = time.monotonic()
        item_id = item_data.get("id", "unknown")

        try:
            before_state = item_data.get("before_state", {})
            after_state = item_data.get("after_state", {})
            if isinstance(before_state, str):
                before_state = json.loads(before_state)
            if isinstance(after_state, str):
                after_state = json.loads(after_state)

            renamed_path = Path(after_state.get("path", ""))
            original_path = Path(before_state.get("path", ""))

            if not renamed_path.exists():
                raise FileNotFoundError(f"Renamed file/folder not found: {renamed_path}")

            # Case-only rollback
            is_case_only = renamed_path.name.lower() == original_path.name.lower()
            if is_case_only:
                tmp_name = renamed_path.parent / f".rollback_tmp_{os.urandom(8).hex()}"
                renamed_path.rename(tmp_name)
                tmp_name.rename(original_path)
            else:
                renamed_path.rename(original_path)

            duration_ms = int((time.monotonic() - start) * 1000)
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.COMPLETED,
                before_state={"path": str(renamed_path), "name": renamed_path.name},
                after_state={"path": str(original_path), "name": original_path.name},
                duration_ms=duration_ms,
                log_entries=[
                    ("INFO", f"Rolled back: {renamed_path.name} \u2192 {original_path.name}", {}),
                ],
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.FAILED,
                duration_ms=duration_ms,
                error_message=f"Rollback failed: {exc}",
                log_entries=[("ERROR", f"Rollback failed: {exc}", {})],
            )

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
        if processed.result != ItemResult.COMPLETED or not processed.after_state:
            return ApplyResult()

        target = str(job_config.get("target", "") or "")
        before_path = str(item_data.get("file_path", "") or "")
        after_path = str(processed.after_state.get("path", "") or "")
        if not before_path or not after_path or before_path == after_path:
            return ApplyResult()

        if target == "files":
            await _sync_file_record(session, before_path=before_path, after_path=after_path)
            return ApplyResult(
                extra_logs=[
                    RuntimeLogEntry(
                        level="INFO",
                        message=(
                            "Updated library file path: "
                            f"{Path(before_path).name} -> {Path(after_path).name}"
                        ),
                        file_path=after_path,
                        extra={
                            "previous_path": before_path,
                            "updated_path": after_path,
                            "record_type": "library_file",
                        },
                    )
                ]
            )

        if target == "folders":
            await _sync_folder_records(session, before_path=before_path, after_path=after_path)
            return ApplyResult(
                extra_logs=[
                    RuntimeLogEntry(
                        level="INFO",
                        message=(
                            "Updated series path: "
                            f"{Path(before_path).name} -> {Path(after_path).name}"
                        ),
                        file_path=after_path,
                        extra={
                            "previous_path": before_path,
                            "updated_path": after_path,
                            "record_type": "series",
                        },
                    )
                ]
            )

        return ApplyResult()

    @staticmethod
    async def apply_rollback_result(
        session: Any,
        item_data: dict[str, Any],
        processed: ProcessedItem,
    ) -> ApplyResult:
        if (
            processed.result != ItemResult.COMPLETED
            or not processed.before_state
            or not processed.after_state
        ):
            return ApplyResult()

        before_path = str(processed.before_state.get("path", "") or "")
        after_path = str(processed.after_state.get("path", "") or "")
        target = str(item_data.get("original_target", "") or "")
        if not before_path or not after_path or before_path == after_path:
            return ApplyResult()

        if target == "files":
            await _sync_file_record(session, before_path=before_path, after_path=after_path)
            return ApplyResult(
                extra_logs=[
                    RuntimeLogEntry(
                        level="INFO",
                        message=(
                            "Updated library file path: "
                            f"{Path(before_path).name} -> {Path(after_path).name}"
                        ),
                        file_path=after_path,
                        extra={
                            "previous_path": before_path,
                            "updated_path": after_path,
                            "record_type": "library_file",
                        },
                    )
                ]
            )

        if target == "folders":
            await _sync_folder_records(session, before_path=before_path, after_path=after_path)
            return ApplyResult(
                extra_logs=[
                    RuntimeLogEntry(
                        level="INFO",
                        message=(
                            "Updated series path: "
                            f"{Path(before_path).name} -> {Path(after_path).name}"
                        ),
                        file_path=after_path,
                        extra={
                            "previous_path": before_path,
                            "updated_path": after_path,
                            "record_type": "series",
                        },
                    )
                ]
            )

        return ApplyResult()

"""Mass convert pipeline executor — chains convert, metadata, and verify steps.

Processes each file through a configurable pipeline:
  Step 1: Convert archive to CBZ (required)
  Step 2: Embed ComicInfo.xml metadata (optional)
  Step 3: Rename to naming template (optional, future)
  Step 4: Verify archive integrity (optional)

Each step runs sequentially per item. If any step fails, the item is
marked FAILED and the original file is not moved to trash.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from pullbox.core.file_safety import classify_resource_safety_exception
from pullbox.core.filesystem_scan import iter_supported_files
from pullbox.core.issue_numbers import format_issue_number
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryFileStorageMode
from pullbox.models.series import Series
from pullbox.services.library_convert_service import _sync_converted_file_record
from pullbox.utilities.base_executor import (
    ApplyResult,
    ExecutionMode,
    ItemResult,
    JobExecutor,
    JobRunSummary,
    ProcessedItem,
    RuntimeLogEntry,
)
from pullbox.utilities.settings import (
    move_file_to_utility_trash,
    resolve_utility_directory,
    restore_file_from_utility_trash,
)

logger = structlog.get_logger(__name__)

_SUPPORTED_EXTENSIONS = frozenset({".cbr", ".cb7", ".cbz", ".pdf"})
_SUPPORTED_LIBRARY_FORMATS = (
    FileFormat.CBR,
    FileFormat.CB7,
    FileFormat.CBZ,
    FileFormat.PDF,
)
_PIPELINE_STEP_ORDER = (1, 2, 4)


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def _format_issue_number(value: float | int | None) -> str | None:
    if value is None:
        return None
    return format_issue_number(value)


def _build_comicinfo_metadata(library_file: LibraryFile) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    issue = library_file.issue
    series = issue.series if issue is not None else None
    publisher = series.publisher if series is not None else None

    if series is not None:
        metadata["Series"] = series.title
        if series.year_start is not None:
            metadata["Year"] = series.year_start
    if issue is not None:
        number = _format_issue_number(issue.issue_number)
        if number:
            metadata["Number"] = number
        if issue.title:
            metadata["Title"] = issue.title
        if issue.description:
            metadata["Summary"] = issue.description
        if issue.page_count is not None:
            metadata["PageCount"] = issue.page_count
        if isinstance(issue.release_date, date):
            metadata["Year"] = issue.release_date.year
            metadata["Month"] = issue.release_date.month
            metadata["Day"] = issue.release_date.day
    if publisher is not None and publisher.name:
        metadata["Publisher"] = publisher.name

    return metadata


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


def _display_step_labels(steps: list[int]) -> tuple[dict[int, int], int]:
    """Map internal pipeline step ids onto the enabled user-facing step order."""
    enabled = [step for step in _PIPELINE_STEP_ORDER if step in {int(value) for value in steps}]
    return {step: index + 1 for index, step in enumerate(enabled)}, len(enabled)


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


def _path_is_referenced(path: Path, job_context: dict[str, Any] | None) -> bool:
    referenced_paths = (job_context or {}).get("referenced_paths", [])
    if not isinstance(referenced_paths, list):
        return False
    resolved_path = path.expanduser().resolve(strict=False)
    return any(
        Path(str(referenced_path)).expanduser().resolve(strict=False) == resolved_path
        for referenced_path in referenced_paths
    )


class MassConvertPipelineExecutor(JobExecutor):
    """Multi-step pipeline: convert → metadata → rename → verify."""

    execution_mode = ExecutionMode.PROCESS

    def __init__(self, session: Any | None = None) -> None:
        self._session = session

    def validate_config(self, job_config: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        steps = job_config.get("steps")
        if not steps:
            errors.append("steps is required (list of step numbers, e.g. [1, 2, 4])")
        elif not isinstance(steps, list):
            errors.append("steps must be a list of step numbers")
        else:
            step_set = {int(step) for step in steps}
            if 1 not in step_set:
                errors.append("Step 1 (convert) must always be included in the pipeline")
            invalid_steps = sorted(step for step in step_set if step not in {1, 2, 4})
            if invalid_steps:
                errors.append(
                    "Unsupported pipeline step(s): "
                    + ", ".join(str(step) for step in invalid_steps)
                )
            if 3 in step_set:
                errors.append("Step 3 (rename) is not part of the Mass Convert workflow")

        scope = str(job_config.get("scope", "manual")).strip().lower()
        if scope not in {"manual", "folder", "library"}:
            errors.append("scope must be one of: manual, folder, library")
        elif scope == "manual" and not job_config.get("file_paths"):
            errors.append("file_paths is required when scope is manual")
        elif scope == "folder":
            scan_folder = str(job_config.get("scan_folder", "")).strip()
            scan_folders = [
                str(path).strip()
                for path in job_config.get("scan_folders", [])
                if str(path).strip()
            ]
            if not scan_folder and not scan_folders:
                errors.append("scan_folder or scan_folders is required when scope is folder")

        return errors

    @asynccontextmanager
    async def _session_ctx(self) -> Any:
        if self._session is not None:
            yield self._session
            return

        from pullbox.database import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            yield session

    @staticmethod
    async def _enrich_paths_with_library_data(
        session: Any,
        candidate_paths: list[Path],
    ) -> list[dict[str, Any]]:
        if not candidate_paths:
            return []

        normalized_paths = [str(path) for path in candidate_paths]
        result = await session.execute(
            select(LibraryFile)
            .options(
                joinedload(LibraryFile.issue).joinedload(Issue.series).joinedload(Series.publisher),
                joinedload(LibraryFile.library_root),
            )
            .where(LibraryFile.file_path.in_(normalized_paths))
        )
        tracked_files = {row.file_path: row for row in result.scalars().all()}

        items: list[dict[str, Any]] = []
        for path in candidate_paths:
            item: dict[str, Any] = {
                "file_path": str(path),
                "operation": "pipeline",
            }
            tracked = tracked_files.get(str(path))
            if tracked is not None:
                item["library_file_id"] = tracked.id
                item["storage_mode"] = tracked.storage_mode.value
                metadata = _build_comicinfo_metadata(tracked)
                if metadata:
                    item["metadata"] = metadata
                    item["metadata_source"] = "library"
            items.append(item)
        return items

    async def build_job_context(
        self,
        session: Any,
        job_config: dict[str, Any],
    ) -> dict[str, Any]:
        scope = str(job_config.get("scope", "manual")).strip().lower()
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
        try:
            trash_dir = _resolve_effective_trash_directory(job_config.get("trash_folder"))
        except Exception:
            trash_dir = Path(tempfile.gettempdir()) / ".pullbox-trash"

        candidate_paths: list[Path] = []
        folder_roots: list[Path] = []

        if scope == "manual":
            for path_str in job_config.get("file_paths", []):
                path = Path(path_str)
                if (
                    path.exists()
                    and path.is_file()
                    and path.suffix.lower() in _SUPPORTED_EXTENSIONS
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
            for root in scan_folders:
                for path in iter_supported_files(root, _SUPPORTED_EXTENSIONS):
                    if _is_relative_to(path, trash_dir):
                        continue
                    candidate_paths.append(path)
        elif scope == "library":
            result = await session.execute(
                select(LibraryFile)
                .options(
                    joinedload(LibraryFile.issue)
                    .joinedload(Issue.series)
                    .joinedload(Series.publisher),
                    joinedload(LibraryFile.library_root),
                )
                .where(LibraryFile.file_format.in_(_SUPPORTED_LIBRARY_FORMATS))
                .order_by(LibraryFile.file_path)
            )
            items: list[dict[str, Any]] = []
            for library_file in result.scalars().all():
                path = Path(library_file.file_path)
                if not path.exists() or not path.is_file():
                    continue
                if _is_relative_to(path, trash_dir):
                    continue
                item: dict[str, Any] = {
                    "file_path": str(path),
                    "operation": "pipeline",
                    "library_file_id": library_file.id,
                    "storage_mode": library_file.storage_mode.value,
                    "trash_relative_path": _relative_trash_path(
                        path,
                        Path(library_file.library_root.path)
                        if library_file.library_root is not None
                        else None,
                    ),
                }
                metadata = _build_comicinfo_metadata(library_file)
                if metadata:
                    item["metadata"] = metadata
                    item["metadata_source"] = "library"
                items.append(item)
            return {"items": items, "referenced_paths": referenced_paths}

        deduped_paths = list(dict.fromkeys(candidate_paths))
        try:
            items = await self._enrich_paths_with_library_data(session, deduped_paths)
        except Exception:
            # Fallback: return basic items without library enrichment
            items = [{"file_path": str(path), "operation": "pipeline"} for path in deduped_paths]

        relative_to = None
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
                Path(item["file_path"]),
                relative_to,
            )
        return {"items": items, "referenced_paths": referenced_paths}

    async def generate_items(
        self,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return items prepared during build_job_context."""
        if job_context is None:
            async with self._session_ctx() as session:
                job_context = await self.build_job_context(session, job_config)
        return list((job_context or {}).get("items", []))

    def process_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        """Run the configured pipeline steps on a single file."""
        start = time.monotonic()
        item_id = item_data.get("id", "unknown")
        file_path = item_data.get("file_path", "")
        source = Path(file_path)
        steps = job_config.get("steps", [1])
        display_steps, total_steps = _display_step_labels(steps)
        log_entries: list[tuple[str, str, dict[str, Any]]] = []
        created_paths: list[Path] = []

        try:
            if item_data.get(
                "storage_mode"
            ) == LibraryFileStorageMode.REFERENCED.value or _path_is_referenced(
                source, job_context
            ):
                return ProcessedItem(
                    item_id=item_id,
                    result=ItemResult.SKIPPED,
                    before_state={"path": str(source)},
                    after_state={"path": str(source), "reason": "referenced_file"},
                    duration_ms=int((time.monotonic() - start) * 1000),
                    warning_message="Referenced library files cannot be converted.",
                    log_entries=[
                        (
                            "WARNING",
                            f"Skipped referenced library file: {source.name}",
                            {"reason": "referenced_file"},
                        )
                    ],
                )
            if not source.exists():
                raise FileNotFoundError(f"Source file not found: {source}")
            if 1 not in steps:
                raise ValueError("Step 1 (convert to CBZ) is required for mass conversion")

            current_path = source
            target_path = source.with_suffix(".cbz")

            # ── Step 1: Convert to CBZ ─────────────────────────
            if 1 in steps:
                from pullbox.utilities.executors.file_converter import (
                    _convert_sync,
                )

                log_entries.append(
                    (
                        "DEBUG",
                        f"Step {display_steps[1]}/{total_steps}: converting {source.name} to CBZ",
                        {
                            "step": display_steps[1],
                            "pipeline_step": 1,
                            "source_path": str(source),
                        },
                    )
                )
                if target_path.exists() and target_path != source:
                    raise FileExistsError(f"Target already exists: {target_path}")

                if source.suffix.lower() == ".cbz":
                    temp_target = source.with_name(f"{source.stem}._mass_convert_.cbz")
                    _convert_sync(source, "cbz", temp_target)
                    current_path = temp_target
                    created_paths.append(temp_target)
                    log_entries.append(
                        (
                            "INFO",
                            f"Repacked {source.name} \u2192 {target_path.name}",
                            {
                                "step": display_steps[1],
                                "pipeline_step": 1,
                                "target_path": str(target_path),
                            },
                        )
                    )
                else:
                    _convert_sync(source, "cbz", target_path)
                    current_path = target_path
                    created_paths.append(target_path)
                    log_entries.append(
                        (
                            "INFO",
                            f"Converted {source.name} \u2192 {target_path.name}",
                            {
                                "step": display_steps[1],
                                "pipeline_step": 1,
                                "target_path": str(target_path),
                            },
                        )
                    )

            # ── Step 2: Embed ComicInfo.xml ────────────────────
            if 2 in steps and current_path.suffix.lower() == ".cbz":
                metadata = item_data.get("metadata", {})
                metadata_source = str(
                    item_data.get("metadata_source")
                    or job_config.get("metadata_source")
                    or "unknown"
                )
                log_entries.append(
                    (
                        "DEBUG",
                        "Step "
                        f"{display_steps[2]}/{total_steps}: embedding ComicInfo.xml into "
                        f"{current_path.name}",
                        {
                            "step": display_steps[2],
                            "pipeline_step": 2,
                            "target_path": str(current_path),
                        },
                    )
                )
                if metadata:
                    log_entries.append(
                        (
                            "DEBUG",
                            "Metadata source: "
                            f"{metadata_source} ({len(metadata)} fields) "
                            f"for {current_path.name}",
                            {
                                "step": display_steps[2],
                                "pipeline_step": 2,
                                "source": metadata_source,
                                "fields": len(metadata),
                            },
                        )
                    )
                    from pullbox.utilities.comicinfo import embed_comicinfo_in_cbz

                    embed_comicinfo_in_cbz(current_path, metadata)
                    log_entries.append(
                        (
                            "INFO",
                            f"ComicInfo.xml embedded in {current_path.name}",
                            {
                                "step": display_steps[2],
                                "pipeline_step": 2,
                                "fields": len(metadata),
                            },
                        )
                    )
                else:
                    log_entries.append(
                        (
                            "WARNING",
                            "No metadata available from "
                            f"{metadata_source} for {current_path.name}, "
                            "skipping step 2",
                            {
                                "step": display_steps[2],
                                "pipeline_step": 2,
                                "source": metadata_source,
                            },
                        )
                    )

            # ── Step 4: Verify integrity ───────────────────────
            if 4 in steps and current_path.suffix.lower() == ".cbz":
                log_entries.append(
                    (
                        "DEBUG",
                        f"Step {display_steps[4]}/{total_steps}: verifying {current_path.name}",
                        {
                            "step": display_steps[4],
                            "pipeline_step": 4,
                            "target_path": str(current_path),
                        },
                    )
                )
                with zipfile.ZipFile(current_path, "r") as zf:
                    bad = zf.testzip()
                    if bad is not None:
                        raise ValueError(f"Integrity check failed: corrupt entry '{bad}'")
                    file_count = len(zf.namelist())
                log_entries.append(
                    (
                        "INFO",
                        f"Integrity verified: {current_path.name} ({file_count} files)",
                        {
                            "step": display_steps[4],
                            "pipeline_step": 4,
                            "file_count": file_count,
                        },
                    )
                )

            # ── Move original to trash ─────────────────────────
            original_trash_path: str | None = None
            trash_dir = _resolve_effective_trash_directory(job_config.get("trash_folder"))
            trash_dest = move_file_to_utility_trash(
                source,
                trash_dir,
                relative_path=item_data.get("trash_relative_path"),
            )
            original_trash_path = str(trash_dest)
            log_entries.append(
                (
                    "INFO",
                    f"Moved original to trash: {source.name} -> {trash_dest}",
                    {
                        "trash_path": original_trash_path,
                        "trash_relative_path": item_data.get("trash_relative_path"),
                    },
                )
            )

            if current_path != target_path:
                current_path.rename(target_path)
                current_path = target_path

            duration_ms = int((time.monotonic() - start) * 1000)
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.COMPLETED,
                before_state={
                    "path": str(source),
                    "format": source.suffix.lstrip("."),
                },
                after_state={
                    "path": str(current_path),
                    "format": "cbz",
                    "original_path": original_trash_path,
                    "metadata_embedded": bool(2 in steps and item_data.get("metadata")),
                    "verified": bool(4 in steps),
                },
                duration_ms=duration_ms,
                log_entries=log_entries,
            )

        except Exception as exc:
            resource_block = classify_resource_safety_exception(exc)
            for created_path in created_paths:
                if created_path.exists():
                    created_path.unlink()
                    log_entries.append(
                        (
                            "WARNING",
                            f"Removed partial output after failure: {created_path.name}",
                            {"cleanup_path": str(created_path)},
                        )
                    )
            duration_ms = int((time.monotonic() - start) * 1000)
            if resource_block is not None:
                safety_payload = resource_block.to_diagnostics()
                log_entries.append(
                    (
                        "WARNING",
                        f"Pipeline stopped for safety review: {resource_block.reason}",
                        {"safety_block": safety_payload},
                    )
                )
                error_message = resource_block.reason
                before_state = {"path": file_path, "safety_block": safety_payload}
            else:
                log_entries.append(("ERROR", f"Pipeline failed: {exc}", {}))
                error_message = str(exc)
                before_state = {"path": file_path}
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.FAILED,
                before_state=before_state,
                duration_ms=duration_ms,
                error_message=error_message,
                log_entries=log_entries,
            )

    def rollback_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        """Restore original from trash and remove converted file."""
        start = time.monotonic()
        item_id = item_data.get("id", "unknown")
        log_entries: list[tuple[str, str, dict[str, Any]]] = []

        try:
            after_state = item_data.get("after_state", {})
            if isinstance(after_state, str):
                after_state = json.loads(after_state)

            converted_path = Path(after_state.get("path", ""))
            original_trash = after_state.get("original_path", "")

            if not original_trash:
                raise FileNotFoundError("Rollback metadata missing original trash path")

            trash_path = Path(original_trash)
            before_state = item_data.get("before_state", {})
            if isinstance(before_state, str):
                before_state = json.loads(before_state)
            original_path = Path(before_state.get("path", ""))
            had_converted_output = converted_path.exists()
            restore_file_from_utility_trash(
                trash_path,
                original_path,
                converted_path=converted_path,
            )
            log_entries.append(
                (
                    "INFO",
                    f"Restored original from trash: {trash_path.name} -> {original_path.name}",
                    {
                        "trash_path": str(trash_path),
                        "restored_path": str(original_path),
                    },
                )
            )
            if had_converted_output:
                log_entries.append(
                    (
                        "INFO",
                        f"Removed converted output: {converted_path.name}",
                        {"converted_path": str(converted_path)},
                    )
                )

            duration_ms = int((time.monotonic() - start) * 1000)
            log_entries.append(("INFO", f"Rollback completed for {item_id}", {}))
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.COMPLETED,
                duration_ms=duration_ms,
                log_entries=log_entries,
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.FAILED,
                duration_ms=duration_ms,
                error_message=f"Rollback failed: {exc}",
                log_entries=[*log_entries, ("ERROR", f"Rollback failed: {exc}", {})],
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

        before_path = str(item_data.get("file_path", "") or "")
        after_path = str(processed.after_state.get("path", "") or "")
        if not before_path or not after_path:
            return ApplyResult()

        await _sync_converted_file_record(
            session,
            before_path=before_path,
            after_path=after_path,
            metadata_embedded=bool(processed.after_state.get("metadata_embedded")),
        )
        return ApplyResult(
            extra_logs=[
                RuntimeLogEntry(
                    level="INFO",
                    message=(
                        "Updated library record: "
                        f"{Path(before_path).name} -> {Path(after_path).name}"
                    ),
                    file_path=after_path,
                    extra={
                        "previous_path": before_path,
                        "updated_path": after_path,
                        "metadata_embedded": bool(processed.after_state.get("metadata_embedded")),
                    },
                )
            ]
        )

    @staticmethod
    async def apply_rollback_result(
        session: Any,
        item_data: dict[str, Any],
        processed: ProcessedItem,
    ) -> ApplyResult:
        if processed.result != ItemResult.COMPLETED:
            return ApplyResult()

        before_state = item_data.get("before_state", {})
        if isinstance(before_state, str):
            before_state = json.loads(before_state or "{}")
        original_path = str(before_state.get("path", "") or "")
        converted_path = str(item_data.get("file_path", "") or "")
        if not original_path or not converted_path:
            return ApplyResult()

        await _sync_converted_file_record(
            session,
            before_path=converted_path,
            after_path=original_path,
            metadata_embedded=False,
        )
        return ApplyResult(
            extra_logs=[
                RuntimeLogEntry(
                    level="INFO",
                    message=(
                        "Updated library record: "
                        f"{Path(converted_path).name} -> {Path(original_path).name}"
                    ),
                    file_path=original_path,
                    extra={
                        "previous_path": converted_path,
                        "updated_path": original_path,
                        "metadata_embedded": False,
                    },
                )
            ]
        )

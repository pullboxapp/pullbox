"""Library permission utility executor."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select

from pullbox.core.library_permission_engine import (
    PermissionAction,
    PermissionCapabilityResult,
    apply_permission_change,
    probe_permission_capability,
)
from pullbox.core.library_permissions import (
    PermissionPolicyError,
    format_mode,
    parse_permission_mode,
)
from pullbox.core.library_root_resolution import resolve_path_inside_roots
from pullbox.models.library import LibraryFile, LibraryFileStorageMode, LibraryRoot
from pullbox.utilities.base_executor import (
    ApplyResult,
    ExecutionMode,
    FinalizeResult,
    ItemResult,
    JobExecutor,
    JobRunSummary,
    ProcessedItem,
    RuntimeLogEntry,
)

_SCOPES = frozenset({"library", "root", "folder", "paths"})
_RUN_MODES = frozenset({"dry_run", "apply"})


class LibraryPermissionsExecutor(JobExecutor):
    """Executor for dry-run, apply, and rollback chmod maintenance jobs."""

    execution_mode = ExecutionMode.THREAD

    def validate_config(self, job_config: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        scope = str(job_config.get("scope", "") or "").strip()
        run_mode = str(job_config.get("run_mode", "") or "").strip()

        if not scope:
            errors.append("scope is required")
        elif scope not in _SCOPES:
            errors.append(f"Invalid scope: {scope}. Must be one of: {', '.join(sorted(_SCOPES))}")

        if not run_mode:
            errors.append("run_mode is required")
        elif run_mode not in _RUN_MODES:
            errors.append(
                f"Invalid run_mode: {run_mode}. Must be one of: {', '.join(sorted(_RUN_MODES))}"
            )

        if run_mode == "apply" and job_config.get("confirm_apply") is not True:
            errors.append("confirm_apply must be true for permission apply jobs")

        include_files = _bool_config(job_config.get("include_files", True))
        include_folders = _bool_config(job_config.get("include_folders", True))
        if not include_files and not include_folders:
            errors.append("At least one of include_files or include_folders must be true")

        if include_folders:
            _append_mode_error(errors, job_config.get("folder_mode", "755"), "folder")
        if include_files:
            _append_mode_error(errors, job_config.get("file_mode", "644"), "file")

        if scope == "folder" and not str(job_config.get("selected_path", "") or "").strip():
            errors.append("selected_path is required for folder scope")
        if scope == "root" and job_config.get("library_root_id") in (None, ""):
            errors.append("library_root_id is required for root scope")
        if scope == "paths" and not isinstance(job_config.get("file_paths"), list):
            errors.append("file_paths is required for paths scope")

        return errors

    async def build_job_context(
        self,
        session: Any,
        job_config: dict[str, Any],
    ) -> dict[str, Any]:
        result = await session.execute(
            select(LibraryRoot.id, LibraryRoot.path).where(LibraryRoot.enabled.is_(True))
        )
        roots = [{"id": row[0], "path": row[1]} for row in result.all()]
        root_entries = _library_root_entries({"library_roots": roots})
        scoped_roots = _scoped_capability_roots(job_config, root_entries)
        file_mode = parse_permission_mode(
            str(job_config.get("file_mode", "644")),
            target_kind="file",
        )
        folder_mode = parse_permission_mode(
            str(job_config.get("folder_mode", "755")),
            target_kind="folder",
        )
        capabilities = [
            _serialize_capability(
                probe_permission_capability(
                    root,
                    file_mode=file_mode,
                    folder_mode=folder_mode,
                )
            )
            for root in scoped_roots
        ]
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
        return {
            "library_roots": roots,
            "permission_capabilities": capabilities,
            "referenced_paths": referenced_paths,
        }

    async def generate_items(
        self,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        explicit_items = job_config.get("items")
        if isinstance(explicit_items, list) and explicit_items:
            return [item for item in explicit_items if isinstance(item, dict)]

        root_entries = _library_root_entries(job_context)
        roots = [path for _, path in root_entries]
        selected_roots = _selected_roots(job_config, root_entries, roots)
        include_files = _bool_config(job_config.get("include_files", True))
        include_folders = _bool_config(job_config.get("include_folders", True))
        operation = (
            "permission_apply" if job_config.get("run_mode") == "apply" else "permission_dry_run"
        )

        items: list[dict[str, Any]] = []
        for root in selected_roots:
            items.extend(
                _scan_permission_items(
                    root,
                    operation=operation,
                    include_files=include_files,
                    include_folders=include_folders,
                )
            )

        return sorted(items, key=lambda item: str(item["file_path"]))

    def process_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        start = time.monotonic()
        item_id = str(item_data.get("id", "unknown"))
        path = Path(str(item_data.get("file_path", "")))
        run_mode = str(job_config.get("run_mode", "dry_run") or "dry_run")

        try:
            if _path_would_mutate_reference(path, job_context):
                return ProcessedItem(
                    item_id=item_id,
                    result=ItemResult.SKIPPED,
                    before_state={"path": str(path)},
                    after_state={"path": str(path), "reason": "referenced_file"},
                    duration_ms=int((time.monotonic() - start) * 1000),
                    warning_message="Referenced library paths cannot have permissions changed.",
                    log_entries=[
                        (
                            "WARNING",
                            f"Skipped referenced library path: {path}",
                            {"reason": "referenced_file"},
                        )
                    ],
                )
            requested_mode = _requested_mode_for_path(path, job_config)
            result = apply_permission_change(
                path,
                requested_mode,
                dry_run=run_mode != "apply",
                skip_hardlinks=True,
                skip_symlinks=True,
            )
            payload = result.serialized()
            duration_ms = int((time.monotonic() - start) * 1000)
            item_result = _item_result_for_action(result.action)
            log_level = _log_level_for_action(result.action)

            return ProcessedItem(
                item_id=item_id,
                result=item_result,
                before_state={
                    "path": str(path),
                    "previous_mode": payload["previous_mode"],
                    "target_kind": payload["target_kind"],
                },
                after_state=payload,
                duration_ms=duration_ms,
                error_message=result.error_message if item_result == ItemResult.FAILED else None,
                warning_message=_warning_for_payload(payload, item_result),
                log_entries=[
                    (
                        log_level,
                        _message_for_payload(payload),
                        payload,
                    )
                ],
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.FAILED,
                before_state={"path": str(path)},
                duration_ms=duration_ms,
                error_message=str(exc),
                log_entries=[("ERROR", f"Permission change failed for {path}: {exc}", {})],
            )

    def rollback_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        start = time.monotonic()
        item_id = str(item_data.get("id", "unknown"))
        path = Path(str(item_data.get("file_path", "")))

        try:
            before_state = _state_dict(item_data.get("before_state"))
            after_state = _state_dict(item_data.get("after_state"))
            previous_mode = _parse_stored_mode(before_state.get("previous_mode"))
            applied_mode = _parse_stored_mode(after_state.get("resulting_mode"))

            if previous_mode is None or applied_mode is None:
                return _rollback_skipped(
                    item_id,
                    start,
                    "Permission item does not have rollback mode data",
                    path,
                )
            if not path.exists() and not path.is_symlink():
                return _rollback_skipped(item_id, start, "Path no longer exists", path)

            current_mode = stat.S_IMODE(path.lstat().st_mode)
            if current_mode != applied_mode:
                return _rollback_skipped(
                    item_id,
                    start,
                    "Path mode changed after apply; rollback skipped",
                    path,
                    current_mode=current_mode,
                    applied_mode=applied_mode,
                )

            result = apply_permission_change(
                path,
                previous_mode,
                dry_run=False,
                skip_hardlinks=True,
                skip_symlinks=True,
            )
            payload = result.serialized()
            duration_ms = int((time.monotonic() - start) * 1000)
            item_result = _item_result_for_action(result.action)
            return ProcessedItem(
                item_id=item_id,
                result=item_result,
                before_state=after_state,
                after_state=payload,
                duration_ms=duration_ms,
                error_message=result.error_message if item_result == ItemResult.FAILED else None,
                warning_message=_warning_for_payload(payload, item_result),
                log_entries=[
                    (_log_level_for_action(result.action), _message_for_payload(payload), payload)
                ],
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.FAILED,
                duration_ms=duration_ms,
                error_message=f"Permission rollback failed: {exc}",
                log_entries=[("ERROR", f"Permission rollback failed for {path}: {exc}", {})],
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
        action = str(processed.after_state.get("action", "") or "")
        if action:
            counts = summary.metadata.setdefault("permission_actions", {})
            if isinstance(counts, dict):
                counts[action] = int(counts.get(action, 0)) + 1
        return ApplyResult()

    async def finalize_job(
        self,
        session: Any,
        job: Any,
        summary: JobRunSummary,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None,
    ) -> FinalizeResult:
        final_parts = ["apply mode" if job_config.get("run_mode") == "apply" else "dry-run mode"]
        capabilities = (job_context or {}).get("permission_capabilities", [])
        unsupported_count = 0
        if isinstance(capabilities, list):
            unsupported_count = sum(
                1
                for capability in capabilities
                if isinstance(capability, dict) and capability.get("supported") is False
            )
        if unsupported_count:
            label = "root" if unsupported_count == 1 else "roots"
            final_parts.append(f"{unsupported_count} {label} unsupported")

        return FinalizeResult(
            extra_logs=[
                RuntimeLogEntry(
                    level="INFO",
                    message=(
                        "Library permissions utility is chmod-only; ownership and group "
                        "changes are not attempted."
                    ),
                    extra={
                        "ownership_capability": "unsupported",
                        "chown_attempted": False,
                        "chgrp_attempted": False,
                    },
                )
            ],
            final_parts=final_parts,
            final_log_level="WARNING" if unsupported_count else None,
        )


def _append_mode_error(errors: list[str], value: object, target_kind: str) -> None:
    try:
        parse_permission_mode(str(value), target_kind=target_kind)  # type: ignore[arg-type]
    except PermissionPolicyError as exc:
        errors.append(str(exc))


def _serialize_capability(result: PermissionCapabilityResult) -> dict[str, object]:
    return {
        "path": str(result.path),
        "supported": result.supported,
        "reason": result.reason.value,
        "can_stat_root": result.can_stat_root,
        "can_create_file": result.can_create_file,
        "can_create_directory": result.can_create_directory,
        "file_chmod_supported": result.file_chmod_supported,
        "directory_chmod_supported": result.directory_chmod_supported,
        "restore_supported": result.restore_supported,
        "error_message": result.error_message,
    }


def _bool_config(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


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


def _library_root_entries(job_context: dict[str, Any] | None) -> list[tuple[str, Path]]:
    raw_roots = (job_context or {}).get("library_roots", [])
    roots: list[tuple[str, Path]] = []
    if not isinstance(raw_roots, list):
        return roots
    for root in raw_roots:
        if not isinstance(root, dict):
            continue
        path = str(root.get("path", "") or "").strip()
        if path:
            roots.append((str(root.get("id", "") or ""), Path(path)))
    return roots


def _selected_roots(
    job_config: dict[str, Any],
    root_entries: list[tuple[str, Path]],
    roots: list[Path],
) -> list[Path]:
    scope = str(job_config.get("scope", "") or "")
    if scope == "library":
        return roots
    if scope == "root":
        root_id = str(job_config.get("library_root_id", "") or "")
        for candidate_id, path in root_entries:
            if candidate_id == root_id:
                return [path]
        raise ValueError(f"Library root not found: {root_id}")
    if scope == "folder":
        selected_raw = Path(str(job_config.get("selected_path", "") or ""))
        selected = resolve_path_inside_roots(selected_raw, roots, require_dir=True)
        # The raw path has been constrained to enabled library roots above; this
        # preserves the no-selected-symlink policy without probing arbitrary paths.
        # codeql[py/path-injection]
        if selected_raw.is_symlink():
            raise ValueError("Selected folder cannot be a symlink")
        return [selected]
    if scope == "paths":
        paths = []
        for path_value in job_config.get("file_paths", []):
            path = resolve_path_inside_roots(str(path_value), roots)
            paths.append(path)
        return paths
    return []


def _scoped_capability_roots(
    job_config: dict[str, Any],
    root_entries: list[tuple[str, Path]],
) -> list[Path]:
    roots = [path for _, path in root_entries]
    scope = str(job_config.get("scope", "library") or "library")
    if scope == "library":
        return roots
    if scope == "root":
        root_id = str(job_config.get("library_root_id", "") or "")
        for candidate_id, path in root_entries:
            if candidate_id == root_id:
                return [path]
        raise ValueError(f"Library root not found: {root_id}")
    if scope == "folder":
        selected_raw = Path(str(job_config.get("selected_path", "") or ""))
        selected = resolve_path_inside_roots(selected_raw, roots, require_dir=True)
        # The raw path has been constrained to enabled library roots above; this
        # preserves the no-selected-symlink policy without probing arbitrary paths.
        # codeql[py/path-injection]
        if selected_raw.is_symlink():
            raise ValueError("Selected folder cannot be a symlink")
        return [_containing_library_root(selected, roots)]
    if scope == "paths":
        scoped_roots: list[Path] = []
        for path_value in job_config.get("file_paths", []):
            path = resolve_path_inside_roots(str(path_value), roots)
            root = _containing_library_root(path, roots)
            if root not in scoped_roots:
                scoped_roots.append(root)
        return scoped_roots
    return roots


def _assert_inside_library_roots(path: Path, roots: list[Path]) -> None:
    _containing_library_root(path, roots)


def _containing_library_root(path: Path, roots: list[Path]) -> Path:
    resolved_path = resolve_path_inside_roots(path, roots)
    for root in roots:
        resolved_root = root.resolve(strict=False)
        if resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root):
            return root
    raise ValueError(f"Selected path is outside enabled library roots: {path}")


def _scan_permission_items(
    root: Path,
    *,
    operation: str,
    include_files: bool,
    include_folders: bool,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if root.is_file() or root.is_symlink():
        if include_files or root.is_symlink():
            return [{"file_path": str(root), "operation": operation}]
        return []

    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        if include_folders:
            items.append({"file_path": str(current_path), "operation": operation})

        for dirname in sorted(list(dirnames)):
            child = current_path / dirname
            if child.is_symlink():
                if include_folders:
                    items.append({"file_path": str(child), "operation": operation})
                dirnames.remove(dirname)

        if include_files:
            for filename in sorted(filenames):
                items.append({"file_path": str(current_path / filename), "operation": operation})

    return items


def _requested_mode_for_path(path: Path, job_config: dict[str, Any]) -> int:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return parse_permission_mode(str(job_config.get("file_mode", "644")), target_kind="file")

    if stat.S_ISDIR(path_stat.st_mode) and not path.is_symlink():
        return parse_permission_mode(
            str(job_config.get("folder_mode", "755")), target_kind="folder"
        )
    return parse_permission_mode(str(job_config.get("file_mode", "644")), target_kind="file")


def _item_result_for_action(action: PermissionAction) -> ItemResult:
    if action == PermissionAction.FAILED:
        return ItemResult.FAILED
    if action in {PermissionAction.SKIPPED, PermissionAction.UNSUPPORTED}:
        return ItemResult.SKIPPED
    return ItemResult.COMPLETED


def _log_level_for_action(action: PermissionAction) -> str:
    if action == PermissionAction.FAILED:
        return "ERROR"
    if action in {PermissionAction.SKIPPED, PermissionAction.UNSUPPORTED}:
        return "WARNING"
    return "INFO"


def _warning_for_payload(payload: dict[str, object], item_result: ItemResult) -> str | None:
    if item_result != ItemResult.SKIPPED:
        return None
    return str(payload.get("reason") or "permission_skipped")


def _message_for_payload(payload: dict[str, object]) -> str:
    return f"Permission {payload.get('action')} for {payload.get('path')} ({payload.get('reason')})"


def _state_dict(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_stored_mode(value: object) -> int | None:
    if value is None:
        return None
    raw = str(value).strip()
    if raw.startswith("0") and len(raw) == 4:
        raw = raw[1:]
    if len(raw) != 3 or any(char not in "01234567" for char in raw):
        return None
    return int(raw, 8)


def _rollback_skipped(
    item_id: str,
    start: float,
    message: str,
    path: Path,
    *,
    current_mode: int | None = None,
    applied_mode: int | None = None,
) -> ProcessedItem:
    payload: dict[str, object] = {
        "path": str(path),
        "reason": "rollback_skipped",
    }
    if current_mode is not None:
        payload["current_mode"] = format_mode(current_mode)
    if applied_mode is not None:
        payload["applied_mode"] = format_mode(applied_mode)
    return ProcessedItem(
        item_id=item_id,
        result=ItemResult.SKIPPED,
        duration_ms=int((time.monotonic() - start) * 1000),
        warning_message=message,
        log_entries=[("WARNING", message, payload)],
    )

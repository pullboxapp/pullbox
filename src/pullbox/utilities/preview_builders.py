"""Builder helpers for utility workflow preview endpoints."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func as sa_func
from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

from pullbox.core.exceptions import ValidationError
from pullbox.core.filesystem_scan import iter_supported_files
from pullbox.core.library_naming import build_series_folder_name, compute_target_filename
from pullbox.core.library_policy import load_library_naming_policy
from pullbox.core.library_root_resolution import resolve_path_inside_roots
from pullbox.core.naming import (
    issue_type_uses_collection_template,
    normalize_issue_type_for_naming,
    resolve_collection_non_standard_file_template,
    resolve_single_non_standard_file_template,
)
from pullbox.models.issue import Issue, IssueType, is_non_standard_issue_type
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.utilities.schemas import (
    ConvertPreviewRequest,
    ConvertPreviewResponse,
    LibraryPermissionsPreviewItem,
    LibraryPermissionsPreviewRequest,
    LibraryPermissionsPreviewResponse,
    MassConvertPreviewItem,
    MassConvertPreviewRequest,
    MassConvertPreviewResponse,
    MassRenamePreviewItem,
    MassRenamePreviewRequest,
    MassRenamePreviewResponse,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence


_MASS_CONVERT_SUPPORTED_EXTS = frozenset({".cbr", ".cb7", ".cbz", ".pdf"})
_MASS_CONVERT_SUPPORTED_FORMATS = {
    FileFormat.CBR,
    FileFormat.CB7,
    FileFormat.CBZ,
    FileFormat.PDF,
}


def build_convert_preview_response(
    body: ConvertPreviewRequest,
    *,
    allowed_roots: Sequence[str | Path] | None = None,
) -> ConvertPreviewResponse:
    """Preview files that would be converted without submitting a job."""
    from pullbox.utilities.executors.file_converter import build_convert_preview

    try:
        result: ConvertPreviewResponse = build_convert_preview(
            source_format=body.source_format,
            target_format=body.target_format,
            scope=body.scope,
            file_paths=body.file_paths,
            allowed_roots=allowed_roots,
        )
        return result
    except ValueError as exc:
        raise ValidationError(str(exc)) from None


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.expanduser().resolve(strict=False).relative_to(
            other.expanduser().resolve(strict=False)
        )
        return True
    except ValueError:
        return False


async def _load_enabled_library_root_paths(session: Any) -> list[Path]:
    result = await session.execute(select(LibraryRoot.path).where(LibraryRoot.enabled.is_(True)))
    return [Path(row[0]) for row in result.all()]


def _infer_mass_convert_source_format(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix == "cbr":
        return "CBR"
    if suffix == "cb7":
        return "CB7"
    if suffix == "pdf":
        return "PDF"
    return "CBZ"


def _build_mass_convert_preview_item(path: Path) -> MassConvertPreviewItem:
    return MassConvertPreviewItem(
        file_path=str(path),
        source_name=path.name,
        source_format=_infer_mass_convert_source_format(path),
        output_name=f"{path.stem}.cbz",
        size_bytes=path.stat().st_size,
    )


def _resolve_preview_path(path: str | Path, roots: Sequence[Path]) -> Path:
    """Return a preview path only after it has been constrained to library roots."""
    try:
        return resolve_path_inside_roots(path, roots)
    except ValueError as exc:
        raise ValidationError(str(exc)) from None


def _lexical_absolute_path(path: str | Path) -> Path:
    """Return an absolute path without following the final symlink target."""
    # Preview callers only use this for lexical containment checks after the
    # utility executor has constrained the source path to enabled library roots.
    # codeql[py/path-injection]
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _generated_preview_root_paths(root: str | Path) -> tuple[Path, ...]:
    """Return acceptable root prefixes for executor-generated preview paths."""
    lexical_root = _lexical_absolute_path(root)
    resolved_root = Path(root).expanduser().resolve(strict=False)
    if resolved_root == lexical_root:
        return (lexical_root,)
    return (lexical_root, resolved_root)


def _resolve_generated_preview_path(path: str | Path, roots: Sequence[Path]) -> Path:
    """Constrain an executor-generated preview path without following symlinks."""
    candidate = _lexical_absolute_path(path)
    for root in roots:
        for root_path in _generated_preview_root_paths(root):
            if candidate == root_path or candidate.is_relative_to(root_path):
                return candidate
    msg = f"Selected path is outside enabled library roots: {path}"
    raise ValidationError(msg)


def _preview_lstat(path: Path) -> Any:
    """Stat a path generated by a library-root-constrained preview executor."""
    # Library-permission preview items are produced by LibraryPermissionsExecutor
    # after selected roots/paths have been constrained to enabled library roots.
    # codeql[py/path-injection]
    return path.lstat()


def _is_preview_symlink(path: Path) -> bool:
    """Return symlink state for a library-root-constrained preview path."""
    # The path was constrained to enabled library roots before this filesystem probe.
    # codeql[py/path-injection]
    return path.is_symlink()


def _rename_target_path(
    current_path: str,
    proposed_name: str,
    library_roots: Sequence[Path],
) -> Path:
    """Build a rename target beside a source path constrained to library roots."""
    source_path = _resolve_preview_path(current_path, library_roots)
    target_path = source_path.parent / proposed_name
    if not any(
        target_path == root.expanduser().resolve(strict=False)
        or target_path.is_relative_to(root.expanduser().resolve(strict=False))
        for root in library_roots
    ):
        raise ValidationError("Proposed rename target is outside enabled library roots.")
    return target_path


def _rename_target_exists(target_path: Path) -> bool:
    """Return whether a root-constrained rename target already exists."""
    # Rename targets are generated beside a source path already constrained to
    # enabled library roots, using Pullbox-generated filenames.
    # codeql[py/path-injection]
    return target_path.exists()


async def build_mass_convert_preview(
    body: MassConvertPreviewRequest,
    *,
    session: Any,
    load_trash_context: Callable[[Any], Awaitable[tuple[Path, int]]] | None,
) -> MassConvertPreviewResponse:
    """Preview files that would be queued by the mass-convert workflow."""
    scope = (body.scope or "library").strip().lower()
    if scope not in {"library", "folder", "manual"}:
        raise ValidationError("scope must be 'library', 'folder', or 'manual'")
    if scope == "manual" and not body.file_paths:
        raise ValidationError("Choose at least one file to preview.")
    if scope == "folder" and not body.file_paths:
        raise ValidationError("Choose at least one folder to preview.")

    if body.trash_folder and body.trash_folder.strip():
        # Preview-only exclusion path supplied by an authenticated operator. It
        # is resolved before comparison and never listed, opened, moved, or mutated.
        # codeql[py/path-injection]
        trash_dir = Path(body.trash_folder.strip()).expanduser()
    else:
        if load_trash_context is None:
            raise ValidationError("trash_folder is required for this preview scope.")
        trash_dir, _ = await load_trash_context(session)
    trash_dir = trash_dir.expanduser().resolve(strict=False)

    library_roots: list[Path] = []
    if scope in {"manual", "folder"}:
        if session is None:
            raise ValidationError("A database session is required for this preview scope.")
        library_roots = await _load_enabled_library_root_paths(session)
        if not library_roots:
            raise ValidationError("No enabled library roots are available for this preview.")

    candidate_paths: list[Path] = []

    if scope == "manual":
        for path_str in body.file_paths:
            try:
                path = resolve_path_inside_roots(path_str, library_roots, require_file=True)
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
            if path.suffix.lower() in _MASS_CONVERT_SUPPORTED_EXTS and not _is_relative_to(
                path, trash_dir
            ):
                candidate_paths.append(path)
    elif scope == "folder":
        for root_path in body.file_paths:
            try:
                root = resolve_path_inside_roots(root_path, library_roots, require_dir=True)
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
            for path in iter_supported_files(root, _MASS_CONVERT_SUPPORTED_EXTS):
                if _is_relative_to(path, trash_dir):
                    continue
                candidate_paths.append(path)
    else:
        if session is None:
            raise ValidationError("A database session is required for library preview.")
        result = await session.execute(
            select(LibraryFile)
            .where(LibraryFile.file_format.in_(_MASS_CONVERT_SUPPORTED_FORMATS))
            .order_by(LibraryFile.file_path.asc())
        )
        for library_file in result.scalars().all():
            path = Path(library_file.file_path)
            if not path.exists() or not path.is_file():
                continue
            if _is_relative_to(path, trash_dir):
                continue
            candidate_paths.append(path)

    deduped_paths = list(dict.fromkeys(candidate_paths))
    items = [_build_mass_convert_preview_item(path) for path in deduped_paths[:100]]
    total_size_bytes = sum(path.stat().st_size for path in deduped_paths)

    return MassConvertPreviewResponse(
        scope=scope,
        item_count=len(deduped_paths),
        total_size_bytes=total_size_bytes,
        items=items,
    )


async def build_library_permissions_preview(
    body: LibraryPermissionsPreviewRequest,
    *,
    session: Any,
) -> LibraryPermissionsPreviewResponse:
    """Preview recursive chmod scope before queueing the permissions job."""
    from pullbox.utilities.executors.library_permissions import LibraryPermissionsExecutor

    scope = (body.scope or "library").strip().lower()
    if scope not in {"library", "folder", "files"}:
        raise ValidationError("scope must be 'library', 'folder', or 'files'")
    if scope == "folder" and not body.file_paths:
        raise ValidationError("Choose at least one folder to preview.")
    if scope == "files" and not body.file_paths:
        raise ValidationError("Choose at least one file to preview.")
    if scope == "files" and not body.include_files:
        raise ValidationError("include_files must be true when previewing selected files.")

    root_result = await session.execute(
        select(LibraryRoot.id, LibraryRoot.path).where(LibraryRoot.enabled.is_(True))
    )
    job_context = {
        "library_roots": [
            {"id": root_id, "path": root_path} for root_id, root_path in root_result.all()
        ]
    }
    job_config: dict[str, Any] = {
        "scope": "library",
        "run_mode": "dry_run",
        "folder_mode": body.folder_mode,
        "file_mode": body.file_mode,
        "include_folders": body.include_folders,
        "include_files": body.include_files,
    }
    if scope in {"folder", "files"}:
        job_config["scope"] = "paths"
        job_config["file_paths"] = body.file_paths

    executor = LibraryPermissionsExecutor()
    errors = executor.validate_config(job_config)
    if errors:
        raise ValidationError(errors[0])

    try:
        generated_items = await executor.generate_items(job_config, job_context)
    except ValueError as exc:
        raise ValidationError(str(exc)) from None

    folder_count = 0
    file_count = 0
    preview_items: list[LibraryPermissionsPreviewItem] = []
    for item in generated_items:
        path = _resolve_generated_preview_path(
            str(item.get("file_path", "")),
            [Path(root["path"]) for root in job_context["library_roots"]],
        )
        try:
            path_stat = _preview_lstat(path)
        except OSError:
            item_type = "file"
        else:
            if _is_preview_symlink(path):
                item_type = "symlink"
            elif stat.S_ISDIR(path_stat.st_mode):
                item_type = "folder"
            else:
                item_type = "file"

        if item_type == "folder":
            folder_count += 1
            target_mode = body.folder_mode
        else:
            file_count += 1
            target_mode = body.file_mode

        if len(preview_items) < 100:
            preview_items.append(
                LibraryPermissionsPreviewItem(
                    file_path=str(path),
                    name=path.name,
                    item_type=item_type,
                    target_mode=target_mode,
                )
            )

    return LibraryPermissionsPreviewResponse(
        scope=scope,
        item_count=len(generated_items),
        folder_count=folder_count,
        file_count=file_count,
        items=preview_items,
    )


async def _load_naming_config(session: Any) -> dict[str, str]:
    """Load naming-related system config with defaults filled in."""
    policy = await load_library_naming_policy(session)
    return {
        "series_folder_template": policy.series_folder_template,
        "comic_file_template": policy.comic_file_template,
        "annual_file_template": policy.annual_file_template,
        "non_standard_file_template": resolve_collection_non_standard_file_template(
            policy.non_standard_file_template
        ),
        "single_non_standard_file_template": resolve_single_non_standard_file_template(
            policy.single_non_standard_file_template
        ),
        "replace_illegal_characters": "true" if policy.replace_illegal_characters else "false",
        "colon_replacement": policy.colon_replacement,
    }


def _resolve_file_template_key(issue_type: str | None) -> tuple[str, str]:
    """Map an issue type to the system config key that drives its rename template."""
    if issue_type == "annual":
        return ("annual_file_template", "Annual Template")
    if is_non_standard_issue_type(issue_type):
        if issue_type_uses_collection_template(issue_type):
            return ("non_standard_file_template", "Collection Template")
        return ("single_non_standard_file_template", "Single-Release Template")
    return ("comic_file_template", "Issue Template")


async def build_mass_rename_preview(
    body: MassRenamePreviewRequest,
    *,
    session: Any,
) -> MassRenamePreviewResponse:
    """Preview proposed Mass Rename results using current naming settings."""
    target = body.target.strip().lower()
    if target not in {"files", "folders"}:
        raise ValidationError("target must be 'files' or 'folders'")
    scope = (body.scope or "manual").strip().lower()
    if scope not in {"library", "folder", "manual"}:
        raise ValidationError("scope must be 'library', 'folder', or 'manual'")
    if scope == "manual" and not body.file_paths:
        raise ValidationError("Choose at least one file or folder to preview.")
    if scope == "folder" and not body.file_paths:
        raise ValidationError("Choose at least one folder to preview this scope.")

    request_paths = list(body.file_paths)
    library_roots = await _load_enabled_library_root_paths(session)
    if not library_roots:
        raise ValidationError("No enabled library roots are available for this preview.")
    if scope != "library":
        require_dir = scope == "folder" or target == "folders"
        require_file = target == "files" and scope == "manual"
        resolved_paths: list[str] = []
        for path_str in request_paths:
            try:
                resolved = resolve_path_inside_roots(
                    path_str,
                    library_roots,
                    require_dir=require_dir,
                    require_file=require_file,
                )
            except ValueError as exc:
                raise ValidationError(str(exc)) from None
            resolved_paths.append(str(resolved))
        request_paths = resolved_paths

    naming_config = await _load_naming_config(session)

    preview_items: list[MassRenamePreviewItem] = []
    preview_targets: dict[str, list[MassRenamePreviewItem]] = {}

    if target == "files":
        query = select(LibraryFile).options(
            joinedload(LibraryFile.issue).joinedload(Issue.series).joinedload(Series.publisher)
        )
        if scope == "library":
            query = query.order_by(LibraryFile.file_path.asc())
            selected_paths: list[str] = []
        elif scope == "folder":
            folder_filters = [
                LibraryFile.file_path.like(f"{folder_path.rstrip('/')}/%")
                for folder_path in request_paths
            ]
            query = query.where(or_(*folder_filters)).order_by(LibraryFile.file_path.asc())
            selected_paths = []
        else:
            query = query.where(LibraryFile.file_path.in_(request_paths))
            selected_paths = list(request_paths)

        result = await session.execute(query)
        matched_files = list(result.scalars().all())
        files_by_path = {file.file_path: file for file in matched_files}
        candidate_paths = selected_paths or [file.file_path for file in matched_files]
        counted_series_ids: set[int] = set()
        for library_file in matched_files:
            issue = library_file.issue
            if issue is None or issue.issue_type not in {IssueType.TPB, IssueType.VOLUME}:
                continue
            counted_series_ids.add(issue.series_id)
        series_collection_counts: dict[int, int] = {}
        if counted_series_ids:
            counts_result = await session.execute(
                select(Issue.series_id, sa_func.count())
                .where(
                    Issue.series_id.in_(counted_series_ids),
                    Issue.issue_type.in_([IssueType.TPB, IssueType.VOLUME]),
                )
                .group_by(Issue.series_id)
            )
            series_collection_counts = {
                int(series_id): int(count)
                for series_id, count in counts_result.all()
                if series_id is not None
            }

        for file_path in candidate_paths:
            current_name = Path(file_path).name
            library_file_match = files_by_path.get(file_path)
            if library_file_match is None:
                preview_items.append(
                    MassRenamePreviewItem(
                        file_path=file_path,
                        current_name=current_name,
                        proposed_name=current_name,
                        actionable=False,
                        status="unmatched",
                        reason="No linked library metadata found for this file.",
                    )
                )
                continue
            if library_file_match.issue is None:
                preview_items.append(
                    MassRenamePreviewItem(
                        file_path=file_path,
                        current_name=current_name,
                        proposed_name=current_name,
                        actionable=False,
                        status="unmatched",
                        reason="No linked library metadata found for this file.",
                    )
                )
                continue
            if library_file_match.issue.series is None:
                preview_items.append(
                    MassRenamePreviewItem(
                        file_path=file_path,
                        current_name=current_name,
                        proposed_name=current_name,
                        actionable=False,
                        status="unmatched",
                        reason="No linked library metadata found for this file.",
                    )
                )
                continue

            library_file = library_file_match
            issue = library_file.issue
            assert issue is not None
            series = issue.series
            assert series is not None
            effective_issue_type = normalize_issue_type_for_naming(
                issue.issue_type.value,
                collection_series_entry_count=series_collection_counts.get(issue.series_id),
            )
            proposed_name = compute_target_filename(
                issue,
                series,
                Path(file_path),
                naming_config,
                issue_type_override=effective_issue_type,
            )
            template_key, template_label = _resolve_file_template_key(effective_issue_type)
            target_path = _rename_target_path(file_path, proposed_name, library_roots)
            reason: str | None = None
            status = "ready"
            actionable = proposed_name != current_name
            if not actionable:
                status = "unchanged"
                reason = "Already matches the current naming template."
            elif _rename_target_exists(target_path) and str(target_path) != file_path:
                status = "conflict"
                reason = f"Target already exists: {target_path.name}"

            preview_item = MassRenamePreviewItem(
                file_path=file_path,
                current_name=current_name,
                proposed_name=proposed_name,
                template_key=template_key,
                template_label=template_label,
                actionable=actionable,
                status=status,
                reason=reason,
            )
            preview_items.append(preview_item)
            if actionable and status == "ready":
                preview_targets.setdefault(str(target_path), []).append(preview_item)
    else:
        series_query = (
            select(Series).options(joinedload(Series.publisher)).where(Series.path.is_not(None))
        )
        if scope == "library":
            series_query = series_query.order_by(Series.path.asc())
            selected_paths = []
        elif scope == "folder":
            series_folder_filters = [
                or_(
                    Series.path == folder_path.rstrip("/"),
                    Series.path.like(f"{folder_path.rstrip('/')}/%"),
                )
                for folder_path in request_paths
            ]
            series_query = series_query.where(or_(*series_folder_filters)).order_by(
                Series.path.asc()
            )
            selected_paths = []
        else:
            series_query = series_query.where(Series.path.in_(request_paths))
            selected_paths = list(request_paths)

        series_result = await session.execute(series_query)
        all_series: Sequence[Series] = series_result.scalars().all()
        series_by_path = {s.path or "": s for s in all_series}
        candidate_paths = selected_paths or [s.path or "" for s in all_series if s.path]

        for folder_path in candidate_paths:
            current_name = Path(folder_path).name or folder_path
            series_match = series_by_path.get(folder_path)
            if series_match is None:
                preview_items.append(
                    MassRenamePreviewItem(
                        file_path=folder_path,
                        current_name=current_name,
                        proposed_name=current_name,
                        template_key="series_folder_template",
                        template_label="Folder Template",
                        actionable=False,
                        status="unmatched",
                        reason="No linked series record found for this folder.",
                    )
                )
                continue

            series = series_match
            proposed_name = build_series_folder_name(series, naming_config)
            target_path = _rename_target_path(folder_path, proposed_name, library_roots)
            reason = None
            status = "ready"
            actionable = proposed_name != current_name
            if not actionable:
                status = "unchanged"
                reason = "Already matches the current folder template."
            elif _rename_target_exists(target_path) and str(target_path) != folder_path:
                status = "conflict"
                reason = f"Target already exists: {target_path.name}"

            preview_item = MassRenamePreviewItem(
                file_path=folder_path,
                current_name=current_name,
                proposed_name=proposed_name,
                template_key="series_folder_template",
                template_label="Folder Template",
                actionable=actionable,
                status=status,
                reason=reason,
            )
            preview_items.append(preview_item)
            if actionable and status == "ready":
                preview_targets.setdefault(str(target_path), []).append(preview_item)

    for conflicted_items in preview_targets.values():
        if len(conflicted_items) < 2:
            continue
        for item in conflicted_items:
            item.status = "conflict"
            item.actionable = False
            item.reason = "Another selected item resolves to the same target name."

    actionable_count = sum(1 for item in preview_items if item.actionable)

    return MassRenamePreviewResponse(
        target=target,
        scope=scope,
        item_count=len(preview_items),
        actionable_count=actionable_count,
        items=preview_items,
    )

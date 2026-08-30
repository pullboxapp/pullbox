"""File registration utility — single entry point for all library file creation.

Every path that adds a file to the library (import, download, manual) calls
``register_library_file()``.  It handles move/rename, series folder creation,
LibraryFile record creation, Issue status updates, and duplicate detection.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from pullbox.core.exceptions import ConfigurationError
from pullbox.core.library_comicinfo import (
    apply_comicinfo_to_imported_artifact as _apply_comicinfo_to_imported_artifact,
)
from pullbox.core.library_comicinfo import (
    build_comicinfo_payload_for_issue as _build_comicinfo_payload_for_issue,
)
from pullbox.core.library_comicinfo import (
    cleanup_prepared_paths as _cleanup_prepared_paths,
)
from pullbox.core.library_comicinfo import (
    prepare_source_artifact as _prepare_source_artifact,
)
from pullbox.core.library_file_ownership import (
    build_file_identity_signature,
    resolve_referenced_library_root,
    validate_file_identity_signature,
)
from pullbox.core.library_leave_in_place import handle_leave_in_place as _handle_leave_in_place
from pullbox.core.library_materialization import (
    paths_on_same_filesystem,
    plan_library_materialization,
)
from pullbox.core.library_naming import (
    build_naming_snapshot as _build_naming_snapshot,
)
from pullbox.core.library_naming import (
    build_series_folder_name as _build_series_folder_name,
)
from pullbox.core.library_naming import (
    compute_target_filename as _compute_target_filename,
)
from pullbox.core.library_naming import (
    resolve_naming_issue_type as _resolve_naming_issue_type,
)
from pullbox.core.library_permission_application import (
    apply_materialized_file_permissions as _apply_materialized_file_permissions,
)
from pullbox.core.library_policy import LibraryIngestPolicy, load_library_ingest_policy
from pullbox.core.library_root_resolution import materialize_series_path as _materialize_series_path
from pullbox.core.library_root_resolution import path_is_inside_root as _path_is_inside_root
from pullbox.core.library_root_resolution import resolve_library_root as _resolve_library_root
from pullbox.core.library_target_paths import (
    predict_library_target_path as _predict_library_target_path,
)
from pullbox.core.library_target_paths import (
    resolve_library_target_path as _resolve_library_target_path,
)
from pullbox.core.library_transfer import safe_move as _library_safe_move
from pullbox.core.library_transfer import transfer_into_library as _transfer_into_library
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.models.series import Series
from pullbox.services.series_delete_targets import trash_relative_path
from pullbox.utilities.executors.file_converter import convert_file
from pullbox.utilities.settings import move_file_to_utility_trash, restore_file_from_utility_trash

if TYPE_CHECKING:
    from collections.abc import Callable

    from pullbox.core.library_permission_engine import PermissionChangeResult
    from pullbox.core.library_permissions import LibraryPermissionPolicy
    from pullbox.models.download import DownloadClientType

logger = structlog.get_logger(__name__)

_DEFERRED_REPLACEMENT_STASHES_KEY = "pullbox_deferred_replacement_stashes"


def _safe_move(src: Path, dst: Path) -> None:
    """Compatibility wrapper for older move-path callers."""
    _library_safe_move(src, dst)


@dataclass(frozen=True, slots=True)
class LibraryFileRegistrationOutcome:
    """Detailed result for import-aware library registration callers."""

    library_file: LibraryFile
    series_folder_created: bool
    series_folder_path: Path | None
    permission_results: tuple[PermissionChangeResult, ...] = ()


@dataclass(slots=True)
class _ReplacementStash:
    """Temporarily moved file state while a replacement import is materialized."""

    library_file: LibraryFile
    original_path: Path
    staged_path: Path | None
    staged_in_trash: bool = False


def _pending_replacement_stashes(session: AsyncSession) -> list[_ReplacementStash]:
    """Return the transaction-local replacement stashes awaiting durability."""
    stashes = session.sync_session.info.setdefault(_DEFERRED_REPLACEMENT_STASHES_KEY, [])
    return cast("list[_ReplacementStash]", stashes)


@event.listens_for(AsyncSession.sync_session_class, "after_commit")
def _cleanup_deferred_replacement_stashes(session: Any) -> None:
    """Discard staged originals only after the database commit succeeds."""
    stashes = session.info.pop(_DEFERRED_REPLACEMENT_STASHES_KEY, [])
    for stash in stashes:
        _discard_replacement_stash_sync(stash)


@event.listens_for(AsyncSession.sync_session_class, "after_rollback")
def _restore_deferred_replacement_stashes(session: Any) -> None:
    """Restore staged originals when a caller rolls back after registration."""
    stashes = session.info.pop(_DEFERRED_REPLACEMENT_STASHES_KEY, [])
    for stash in reversed(stashes):
        _restore_replacement_stash_sync(stash)


async def _load_issue_with_series_and_publisher(
    session: AsyncSession,
    issue: Issue,
) -> Issue:
    """Reload an Issue with eager relationships even if it is already identity-mapped."""
    statement = (
        select(Issue)
        .options(
            joinedload(Issue.series).joinedload(Series.publisher),
            joinedload(Issue.library_file).joinedload(LibraryFile.library_root),
        )
        .where(Issue.id == issue.id)
        .execution_options(populate_existing=True)
    )
    result = await session.execute(statement)
    loaded_issue = result.scalars().unique().one_or_none()
    return loaded_issue or issue


_FORMAT_MAP: dict[str, FileFormat] = {
    "cbz": FileFormat.CBZ,
    "cbr": FileFormat.CBR,
    "cb7": FileFormat.CB7,
    "cbt": FileFormat.CBT,
    "pdf": FileFormat.PDF,
    "epub": FileFormat.EPUB,
}


async def resolve_library_destination(
    session: AsyncSession,
    source_path: Path,
    issue: Issue,
    *,
    rename: bool | None = None,
    library_root_id: int | None = None,
) -> tuple[Path, LibraryRoot]:
    """Resolve the canonical library destination path for an issue/source pair."""
    ingest_policy = await load_library_ingest_policy(session)
    if rename is None:
        rename = ingest_policy.rename_on_import

    loaded_issue = await _load_issue_with_series_and_publisher(session, issue)
    series = loaded_issue.series

    root = await _resolve_library_root(
        session,
        source_path,
        library_root_id,
        series=series,
    )
    if isinstance(series, Series) and series.path:
        series_folder = Path(series.path)
    else:
        series_folder = Path(root.path) / _build_series_folder_name(series, ingest_policy)

    if rename:
        effective_issue_type = await _resolve_naming_issue_type(session, loaded_issue)
        target_name = _compute_target_filename(
            loaded_issue,
            series,
            source_path,
            ingest_policy,
            issue_type_override=effective_issue_type,
        )
    else:
        target_name = source_path.name

    return series_folder / target_name, root


async def register_library_file(
    session: AsyncSession,
    source_path: Path,
    issue: Issue,
    confidence: MatchConfidence,
    *,
    move_to_library: bool = True,
    storage_mode: LibraryFileStorageMode | None = None,
    expected_source_signature: dict[str, object] | None = None,
    rename: bool | None = None,
    library_root_id: int | None = None,
    transfer_method: str | None = None,
    normalize_to_cbz: bool | None = None,
    update_embedded_comicinfo_from_match: bool | None = None,
    comicinfo_payload: dict[str, Any] | None = None,
    loaded_issue: Issue | None = None,
    ingest_policy: LibraryIngestPolicy | None = None,
    permission_policy: LibraryPermissionPolicy | None = None,
    transfer_progress_callback: Callable[[int, int], None] | None = None,
    download_client: DownloadClientType | None = None,
    converter: Callable[[Path, str, Path | None], Any] | None = None,
    comicinfo_embedder: Callable[[Path, dict[str, Any]], Any] | None = None,
    comicinfo_progress_callback: Callable[[str, int, int, str], Any] | None = None,
    artifact_transfer: Callable[..., Any] | None = None,
    comicinfo_materializer: Callable[..., Any] | None = None,
    placement_started_callback: Callable[..., Any] | None = None,
    allow_resource_safety_exception: bool = False,
    replace_existing_library_file: bool = False,
    replacement_trash_dir: Path | None = None,
) -> LibraryFile:
    """Register a file in the library, optionally moving/renaming it."""
    outcome = await register_library_file_with_metadata(
        session,
        source_path,
        issue,
        confidence,
        move_to_library=move_to_library,
        storage_mode=storage_mode,
        expected_source_signature=expected_source_signature,
        rename=rename,
        library_root_id=library_root_id,
        transfer_method=transfer_method,
        normalize_to_cbz=normalize_to_cbz,
        update_embedded_comicinfo_from_match=update_embedded_comicinfo_from_match,
        comicinfo_payload=comicinfo_payload,
        loaded_issue=loaded_issue,
        ingest_policy=ingest_policy,
        permission_policy=permission_policy,
        transfer_progress_callback=transfer_progress_callback,
        download_client=download_client,
        converter=converter,
        comicinfo_embedder=comicinfo_embedder,
        comicinfo_progress_callback=comicinfo_progress_callback,
        artifact_transfer=artifact_transfer,
        comicinfo_materializer=comicinfo_materializer,
        placement_started_callback=placement_started_callback,
        allow_resource_safety_exception=allow_resource_safety_exception,
        replace_existing_library_file=replace_existing_library_file,
        replacement_trash_dir=replacement_trash_dir,
    )
    return outcome.library_file


async def register_library_file_with_metadata(
    session: AsyncSession,
    source_path: Path,
    issue: Issue,
    confidence: MatchConfidence,
    *,
    move_to_library: bool = True,
    storage_mode: LibraryFileStorageMode | None = None,
    expected_source_signature: dict[str, object] | None = None,
    rename: bool | None = None,
    library_root_id: int | None = None,
    transfer_method: str | None = None,
    normalize_to_cbz: bool | None = None,
    update_embedded_comicinfo_from_match: bool | None = None,
    comicinfo_payload: dict[str, Any] | None = None,
    loaded_issue: Issue | None = None,
    ingest_policy: LibraryIngestPolicy | None = None,
    permission_policy: LibraryPermissionPolicy | None = None,
    transfer_progress_callback: Callable[[int, int], None] | None = None,
    download_client: DownloadClientType | None = None,
    converter: Callable[[Path, str, Path | None], Any] | None = None,
    comicinfo_embedder: Callable[[Path, dict[str, Any]], Any] | None = None,
    comicinfo_progress_callback: Callable[[str, int, int, str], Any] | None = None,
    artifact_transfer: Callable[..., Any] | None = None,
    comicinfo_materializer: Callable[..., Any] | None = None,
    placement_started_callback: Callable[..., Any] | None = None,
    allow_resource_safety_exception: bool = False,
    replace_existing_library_file: bool = False,
    replacement_trash_dir: Path | None = None,
) -> LibraryFileRegistrationOutcome:
    """Register a file in the library, optionally moving/renaming it.

    Args:
        session: Active database session.
        source_path: Path to the source file on disk.
        issue: The Issue this file is matched to.
        confidence: Match confidence level.
        move_to_library: If True, move file into the comics directory.
        rename: If True, rename per naming template.  If None, read from config.
        library_root_id: Explicit LibraryRoot id.  Auto-detected if omitted.

    Returns:
        Detailed registration data for callers that need rollback metadata.

    Raises:
        FileNotFoundError: If source_path does not exist.
        ConfigurationError: If no comics directory is configured when needed.
    """
    prepared_source = source_path
    final_path: Path
    cleanup_paths: list[Path] = []
    normalized_source = False
    effective_converter = converter or convert_file
    effective_comicinfo_embedder = comicinfo_embedder or _apply_comicinfo_to_imported_artifact
    transfer_artifact = artifact_transfer or _materialize_library_artifact
    materialize_with_comicinfo = comicinfo_materializer
    comicinfo_already_embedded = False
    replacement_stash: _ReplacementStash | None = None
    replacement_finalized = False
    requested_rename = rename
    requested_transfer_method = transfer_method
    requested_normalize_to_cbz = normalize_to_cbz
    requested_comicinfo_update = update_embedded_comicinfo_from_match
    effective_storage_mode = storage_mode or (
        LibraryFileStorageMode.MANAGED if move_to_library else LibraryFileStorageMode.REFERENCED
    )
    referenced_signature: dict[str, int | str] | None = None

    if effective_storage_mode == LibraryFileStorageMode.REFERENCED and move_to_library:
        raise ConfigurationError("Referenced storage cannot materialize a managed library file.")
    if effective_storage_mode == LibraryFileStorageMode.MANAGED and not move_to_library:
        raise ConfigurationError("Managed storage requires library materialization.")

    async def notify_placement_started(
        *,
        artifact_source: Path,
        target_path: Path,
        effective_transfer_method: str,
        series_folder_created: bool,
    ) -> None:
        if placement_started_callback is None:
            return
        temp_paths: list[Path] = []
        if (
            update_embedded_comicinfo_from_match
            and artifact_source.suffix.lower() == ".cbz"
            and target_path.suffix.lower() == ".cbz"
            and effective_transfer_method in {"move", "copy"}
        ):
            temp_paths.append(target_path.with_name(f"{target_path.name}.pullbox-write.tmp"))
        callback_result = placement_started_callback(
            artifact_source_path=artifact_source,
            target_path=target_path,
            transfer_method=effective_transfer_method,
            series_folder_created=series_folder_created,
            series_folder_path=target_path.parent,
            temp_paths=tuple(temp_paths),
        )
        if inspect.isawaitable(callback_result):
            await callback_result

    try:
        # Prevent callers with dirty ORM state from opening a SQLite write
        # transaction before we finish the slow planning/materialization work.
        with session.no_autoflush:
            # 2. Load effective ingest policy
            effective_ingest_policy = ingest_policy or await load_library_ingest_policy(session)

            # 3. Resolve effective compatibility overrides
            if rename is None:
                rename = effective_ingest_policy.rename_on_import
            if transfer_method is None:
                transfer_method = effective_ingest_policy.post_processing_method
            if normalize_to_cbz is None:
                normalize_to_cbz = effective_ingest_policy.normalize_imported_archives_to_cbz
            if update_embedded_comicinfo_from_match is None:
                update_embedded_comicinfo_from_match = (
                    effective_ingest_policy.update_embedded_comicinfo_from_match
                )

            if effective_storage_mode == LibraryFileStorageMode.REFERENCED:
                if requested_rename is True:
                    raise ConfigurationError("Referenced library files cannot rename source files.")
                if requested_normalize_to_cbz is True:
                    raise ConfigurationError(
                        "Referenced library files cannot normalize or convert source files."
                    )
                if requested_comicinfo_update is True:
                    raise ConfigurationError(
                        "Referenced library files cannot update embedded ComicInfo.xml."
                    )
                if replace_existing_library_file:
                    raise ConfigurationError(
                        "Referenced library files cannot replace an existing artifact."
                    )
                if requested_transfer_method not in {None, "leave_in_place", "referenced"}:
                    raise ConfigurationError(
                        "Referenced library files cannot use a transfer method."
                    )
                rename = False
                normalize_to_cbz = False
                update_embedded_comicinfo_from_match = False
                transfer_method = "leave_in_place"

            # 4. Load series with publisher (need for naming and root resolution)
            effective_issue = (
                await _load_issue_with_series_and_publisher(session, issue)
                if loaded_issue is None or replace_existing_library_file
                else loaded_issue
            )
            series = effective_issue.series

            # 5. Resolve library root
            if effective_storage_mode == LibraryFileStorageMode.REFERENCED:
                root, source_path, referenced_signature = await resolve_referenced_library_root(
                    session,
                    source_path,
                    library_root_id,
                )
                if expected_source_signature is not None:
                    validate_file_identity_signature(
                        expected_source_signature,
                        referenced_signature,
                    )
                prepared_source = source_path
            else:
                root = await _resolve_library_root(
                    session,
                    source_path,
                    library_root_id,
                    series=series,
                )
            replace_existing_path = (
                Path(effective_issue.library_file.file_path)
                if replace_existing_library_file and effective_issue.library_file is not None
                else None
            )

            if not source_path.exists():
                recovered = await _recover_materialized_target_without_source(
                    session,
                    source_path=source_path,
                    issue=effective_issue,
                    series=series,
                    root=root,
                    confidence=confidence,
                    rename=bool(rename),
                    naming_policy=effective_ingest_policy,
                    normalize_to_cbz=bool(normalize_to_cbz),
                    update_embedded_comicinfo_from_match=bool(update_embedded_comicinfo_from_match),
                )
                if recovered is not None:
                    return recovered
                raise FileNotFoundError(f"Source file not found: {source_path}")

            seed_safe_torrent_import = (
                move_to_library
                and download_client is not None
                and download_client.is_torrent
                and effective_ingest_policy.torrent_import_strategy == "seed_safe"
            )

            if (
                not seed_safe_torrent_import
                and update_embedded_comicinfo_from_match
                and transfer_method not in {"move", "copy"}
            ):
                raise ConfigurationError(
                    "Updating embedded ComicInfo.xml from matched issue requires the transfer "
                    "method to be Move or Copy."
                )
            if (
                not seed_safe_torrent_import
                and (normalize_to_cbz or update_embedded_comicinfo_from_match)
                and transfer_method in {"hardlink", "symlink"}
            ):
                raise ConfigurationError(
                    "Archive normalization requires the transfer method to be Move or Copy."
                )

            if move_to_library and (normalize_to_cbz or update_embedded_comicinfo_from_match):
                prepared_source, cleanup_paths = await _prepare_source_artifact(
                    source_path,
                    normalize_to_cbz=normalize_to_cbz,
                    update_embedded_comicinfo_from_match=update_embedded_comicinfo_from_match,
                    converter=effective_converter,
                    allow_resource_safety_exception=allow_resource_safety_exception,
                )
                normalized_source = prepared_source != source_path

            # 6. Determine final file path
            if move_to_library:
                if seed_safe_torrent_import and download_client is not None:
                    target = await _resolve_library_target_path(
                        session,
                        prepared_source,
                        effective_issue,
                        series,
                        root,
                        effective_ingest_policy,
                        rename,
                        replace_existing_path=replace_existing_path,
                    )
                    target_path = target.path
                    replacement_stash = await _stage_replacement_file(
                        effective_issue,
                        prepared_source,
                        replace_existing_library_file=replace_existing_library_file,
                        replacement_trash_dir=replacement_trash_dir,
                    )
                    same_filesystem = await asyncio.to_thread(
                        paths_on_same_filesystem,
                        prepared_source,
                        target_path,
                    )
                    materialization_plan = plan_library_materialization(
                        download_client=download_client,
                        torrent_import_strategy=effective_ingest_policy.torrent_import_strategy,
                        preferred_transfer_method=transfer_method,
                        same_filesystem=same_filesystem,
                        normalize_to_cbz=normalize_to_cbz,
                        update_embedded_comicinfo=update_embedded_comicinfo_from_match,
                    )
                    transfer_method = materialization_plan.materialization_method
                    await notify_placement_started(
                        artifact_source=prepared_source,
                        target_path=target_path,
                        effective_transfer_method=transfer_method,
                        series_folder_created=target.series_folder_created,
                    )
                    final_path = await transfer_artifact(
                        prepared_source,
                        target_path,
                        transfer_method,
                        transfer_progress_callback=transfer_progress_callback,
                    )
                    series_folder_created = target.series_folder_created
                else:
                    target = await _resolve_library_target_path(
                        session,
                        prepared_source,
                        effective_issue,
                        series,
                        root,
                        effective_ingest_policy,
                        rename,
                        replace_existing_path=replace_existing_path,
                    )
                    replacement_stash = await _stage_replacement_file(
                        effective_issue,
                        prepared_source,
                        replace_existing_library_file=replace_existing_library_file,
                        replacement_trash_dir=replacement_trash_dir,
                    )
                    if _can_materialize_cbz_with_comicinfo(
                        prepared_source,
                        transfer_method,
                        update_embedded_comicinfo_from_match=bool(
                            update_embedded_comicinfo_from_match
                        ),
                        materialize_with_comicinfo=materialize_with_comicinfo,
                    ):
                        materializer = materialize_with_comicinfo
                        if materializer is None:
                            raise RuntimeError("ComicInfo materializer unavailable")
                        payload = comicinfo_payload or await _build_comicinfo_payload_for_issue(
                            session,
                            effective_issue,
                            source_path=prepared_source,
                        )
                        await notify_placement_started(
                            artifact_source=prepared_source,
                            target_path=target.path,
                            effective_transfer_method=transfer_method,
                            series_folder_created=target.series_folder_created,
                        )
                        materialize_result = materializer(
                            prepared_source,
                            target.path,
                            payload,
                            transfer_method=transfer_method,
                            progress_callback=comicinfo_progress_callback
                            or transfer_progress_callback,
                        )
                        if inspect.isawaitable(materialize_result):
                            await materialize_result
                        final_path = target.path
                        series_folder_created = target.series_folder_created
                        comicinfo_already_embedded = True
                    else:
                        await notify_placement_started(
                            artifact_source=prepared_source,
                            target_path=target.path,
                            effective_transfer_method=transfer_method,
                            series_folder_created=target.series_folder_created,
                        )
                        final_path = await transfer_artifact(
                            prepared_source,
                            target.path,
                            transfer_method,
                            transfer_progress_callback=transfer_progress_callback,
                        )
                        series_folder_created = target.series_folder_created
            else:
                final_path = await _handle_leave_in_place(
                    session,
                    prepared_source,
                    effective_issue,
                    series,
                    root,
                    effective_ingest_policy,
                    rename,
                )
                series_folder_created = False

            # 7. Check for existing LibraryFile with same path (idempotent)
            existing_result = await session.execute(
                select(LibraryFile).where(LibraryFile.file_path == str(final_path))
            )
            existing = existing_result.scalars().first()
            if update_embedded_comicinfo_from_match and not comicinfo_already_embedded:
                payload = comicinfo_payload or await _build_comicinfo_payload_for_issue(
                    session,
                    effective_issue,
                    source_path=final_path,
                )
                embed_kwargs: dict[str, Any] = {}
                if comicinfo_progress_callback is not None:
                    with suppress(TypeError, ValueError):
                        if (
                            "progress_callback"
                            in inspect.signature(effective_comicinfo_embedder).parameters
                        ):
                            embed_kwargs["progress_callback"] = comicinfo_progress_callback
                embed_result = effective_comicinfo_embedder(
                    final_path,
                    payload,
                    **embed_kwargs,
                )
                if inspect.isawaitable(embed_result):
                    await embed_result
            effective_issue_type = await _resolve_naming_issue_type(session, effective_issue)
            if move_to_library or _path_is_inside_root(final_path, root):
                _materialize_series_path(series, final_path.parent, root)
            naming_snapshot = _build_naming_snapshot(
                source_path=source_path,
                prepared_source=prepared_source,
                target_path=final_path,
                issue=effective_issue,
                series=series,
                root=root,
                naming_policy=effective_ingest_policy,
                rename=bool(rename),
                effective_issue_type=effective_issue_type,
                transfer_method=transfer_method,
                move_to_library=move_to_library,
                normalized_source=normalized_source,
                update_embedded_comicinfo_from_match=bool(update_embedded_comicinfo_from_match),
                normalize_to_cbz=bool(normalize_to_cbz),
            )
            if existing is not None:
                if existing.issue_id is not None and existing.issue_id != effective_issue.id:
                    raise ConfigurationError(
                        "This library path is already registered to a different issue."
                    )
                if existing.storage_mode != effective_storage_mode:
                    raise ConfigurationError(
                        "Existing library-file ownership cannot be changed during registration."
                    )
                # Update match info on existing record
                await _update_existing_library_file_from_path(
                    existing,
                    final_path,
                    issue=effective_issue,
                    series=series,
                    root=root,
                    confidence=confidence,
                    naming_snapshot=naming_snapshot,
                    storage_mode=effective_storage_mode,
                    source_signature=(
                        referenced_signature
                        if referenced_signature is not None
                        else build_file_identity_signature(final_path)
                    ),
                )
                await _finalize_replacement_stash_db_state(
                    session,
                    replacement_stash,
                    registered_file=existing,
                )
                await session.flush()
                _defer_replacement_stash_cleanup(session, replacement_stash)
                replacement_finalized = True
                logger.info(
                    "library_file_already_exists",
                    file_path=str(final_path),
                    library_file_id=existing.id,
                )
                if (
                    normalized_source
                    and move_to_library
                    and transfer_method == "move"
                    and source_path.exists()
                ):
                    await asyncio.to_thread(source_path.unlink)
                return LibraryFileRegistrationOutcome(
                    library_file=existing,
                    series_folder_created=series_folder_created,
                    series_folder_path=final_path.parent,
                )

            permission_results = await _apply_materialized_file_permissions(
                session,
                final_path,
                move_to_library=move_to_library,
                series_folder_created=series_folder_created,
                policy=permission_policy,
            )

        # 8. Stat the final file
        stat = await asyncio.to_thread(final_path.stat)

        # 9. Create LibraryFile record
        extension = final_path.suffix.lstrip(".").lower()
        file_format = _FORMAT_MAP.get(extension, FileFormat.CBZ)

        lf = LibraryFile(
            file_path=str(final_path),
            file_name=final_path.name,
            file_size=stat.st_size,
            file_format=file_format,
            file_modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            match_confidence=confidence,
            parsed_series=series.title if series else None,
            parsed_issue_number=effective_issue.issue_number,
            parsed_year=series.year_start if series else None,
            issue_id=issue.id,
            library_root_id=root.id,
            naming_snapshot=naming_snapshot,
            storage_mode=effective_storage_mode,
            source_signature=(
                referenced_signature
                if referenced_signature is not None
                else build_file_identity_signature(final_path)
            ),
        )
        session.add(lf)

        # 10. Set Issue status to OWNED
        issue.status = IssueStatus.OWNED
        effective_issue.status = IssueStatus.OWNED

        await _finalize_replacement_stash_db_state(
            session,
            replacement_stash,
            registered_file=lf,
        )

        await session.flush()
        _defer_replacement_stash_cleanup(session, replacement_stash)
        replacement_finalized = True

        logger.info(
            "library_file_registered",
            file_path=str(final_path),
            issue_id=issue.id,
            confidence=confidence.value,
            library_file_id=lf.id,
        )
        if (
            normalized_source
            and move_to_library
            and transfer_method == "move"
            and source_path.exists()
        ):
            await asyncio.to_thread(source_path.unlink)
        return LibraryFileRegistrationOutcome(
            library_file=lf,
            series_folder_created=series_folder_created,
            series_folder_path=final_path.parent,
            permission_results=permission_results,
        )
    except asyncio.CancelledError:
        if replacement_stash is not None and not replacement_finalized:
            await _restore_replacement_stash(replacement_stash)
        raise
    except Exception:
        if replacement_stash is not None and not replacement_finalized:
            await _restore_replacement_stash(replacement_stash)
        raise
    finally:
        await asyncio.to_thread(_cleanup_prepared_paths, cleanup_paths)


async def _materialize_library_artifact(
    source_path: Path,
    target_path: Path,
    transfer_method: str,
    *,
    transfer_progress_callback: Callable[[int, int], None] | None = None,
) -> Path:
    """Transfer the prepared artifact to the already-resolved library path."""
    if source_path.resolve(strict=False) != target_path.resolve(strict=False):
        await asyncio.to_thread(
            _transfer_into_library,
            source_path,
            target_path,
            transfer_method,
            transfer_progress_callback,
        )

    logger.info(
        "file_moved_to_library",
        source=str(source_path),
        destination=str(target_path),
        transfer_method=transfer_method,
    )
    return target_path


async def _stage_replacement_file(
    issue: Issue,
    artifact_source: Path,
    *,
    replace_existing_library_file: bool,
    replacement_trash_dir: Path | None,
) -> _ReplacementStash | None:
    """Move an existing issue file aside before materializing a replacement."""
    if not replace_existing_library_file or issue.library_file is None:
        return None

    library_file = issue.library_file
    original_path = Path(library_file.file_path)
    if not await asyncio.to_thread(original_path.exists):
        return _ReplacementStash(
            library_file=library_file,
            original_path=original_path,
            staged_path=None,
        )

    if original_path.resolve(strict=False) == artifact_source.resolve(strict=False):
        return _ReplacementStash(
            library_file=library_file,
            original_path=original_path,
            staged_path=None,
        )

    if replacement_trash_dir is not None:
        staged_path = await asyncio.to_thread(
            move_file_to_utility_trash,
            original_path,
            replacement_trash_dir,
            relative_path=trash_relative_path(original_path, library_file.library_root),
        )
        return _ReplacementStash(
            library_file=library_file,
            original_path=original_path,
            staged_path=staged_path,
            staged_in_trash=True,
        )

    staged_path = _replacement_staging_path(original_path)
    await asyncio.to_thread(original_path.rename, staged_path)
    return _ReplacementStash(
        library_file=library_file,
        original_path=original_path,
        staged_path=staged_path,
    )


def _replacement_staging_path(original_path: Path) -> Path:
    """Return a hidden, non-conflicting sibling path for a replaced file."""
    candidate = original_path.with_name(f".{original_path.name}.pullbox-replace")
    if not candidate.exists():
        return candidate

    counter = 1
    while True:
        numbered = original_path.with_name(f".{original_path.name}.pullbox-replace-{counter}")
        if not numbered.exists():
            return numbered
        counter += 1


async def _restore_replacement_stash(stash: _ReplacementStash) -> None:
    """Restore a staged old file after a failed replacement attempt."""
    await asyncio.to_thread(_restore_replacement_stash_sync, stash)


def _restore_replacement_stash_sync(stash: _ReplacementStash) -> None:
    """Synchronously restore a staged old file for transaction event hooks."""
    if stash.staged_path is None:
        return

    if stash.original_path.exists():
        with suppress(OSError):
            stash.original_path.unlink()

    if stash.staged_in_trash:
        restore_file_from_utility_trash(stash.staged_path, stash.original_path)
    else:
        stash.original_path.parent.mkdir(parents=True, exist_ok=True)
        stash.staged_path.rename(stash.original_path)


async def _finalize_replacement_stash_db_state(
    session: AsyncSession,
    stash: _ReplacementStash | None,
    *,
    registered_file: LibraryFile,
) -> None:
    """Stage superseded DB row removal before the caller flushes."""
    if stash is None:
        return

    if stash.library_file.id != registered_file.id:
        await session.delete(stash.library_file)


def _defer_replacement_stash_cleanup(
    session: AsyncSession,
    stash: _ReplacementStash | None,
) -> None:
    """Defer staged original cleanup until the active transaction is durable."""
    if stash is None:
        return
    _pending_replacement_stashes(session).append(stash)


def _discard_replacement_stash_sync(stash: _ReplacementStash) -> None:
    """Discard a staged original after the replacement transaction commits."""
    if stash.staged_path is None or stash.staged_in_trash:
        return
    with suppress(FileNotFoundError):
        stash.staged_path.unlink()


async def _update_existing_library_file_from_path(
    library_file: LibraryFile,
    final_path: Path,
    *,
    issue: Issue,
    series: object,
    root: LibraryRoot,
    confidence: MatchConfidence,
    naming_snapshot: dict[str, Any],
    storage_mode: LibraryFileStorageMode,
    source_signature: dict[str, int | str],
) -> None:
    """Refresh an existing LibraryFile row from the current artifact on disk."""
    stat = await asyncio.to_thread(final_path.stat)
    extension = final_path.suffix.lstrip(".").lower()

    library_file.file_path = str(final_path)
    library_file.file_name = final_path.name
    library_file.file_size = stat.st_size
    library_file.file_format = _FORMAT_MAP.get(extension, FileFormat.CBZ)
    library_file.file_modified_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    library_file.match_confidence = confidence
    library_file.parsed_series = series.title if isinstance(series, Series) else None
    library_file.parsed_issue_number = issue.issue_number
    library_file.parsed_year = series.year_start if isinstance(series, Series) else None
    library_file.issue_id = issue.id
    library_file.library_root_id = root.id
    library_file.naming_snapshot = naming_snapshot
    library_file.storage_mode = storage_mode
    library_file.source_signature = source_signature
    issue.status = IssueStatus.OWNED


def _can_materialize_cbz_with_comicinfo(
    source_path: Path,
    transfer_method: str,
    *,
    update_embedded_comicinfo_from_match: bool,
    materialize_with_comicinfo: Callable[..., Any] | None,
) -> bool:
    """Return true when final CBZ placement can include ComicInfo in one archive write."""
    return (
        update_embedded_comicinfo_from_match
        and materialize_with_comicinfo is not None
        and source_path.suffix.lower() == ".cbz"
        and transfer_method in {"move", "copy"}
    )


async def _recover_materialized_target_without_source(
    session: AsyncSession,
    *,
    source_path: Path,
    issue: Issue,
    series: object,
    root: LibraryRoot,
    confidence: MatchConfidence,
    rename: bool,
    naming_policy: LibraryIngestPolicy,
    normalize_to_cbz: bool,
    update_embedded_comicinfo_from_match: bool,
) -> LibraryFileRegistrationOutcome | None:
    """Recover a previously materialized library file when the source is already gone.

    This can happen if a prior move into the library succeeded but the database
    transaction was interrupted before the LibraryFile row committed.
    """
    recovery_source = source_path
    if source_path.suffix.lower() != ".cbz" and (
        normalize_to_cbz or update_embedded_comicinfo_from_match
    ):
        recovery_source = source_path.with_suffix(".cbz")

    target_path = await _predict_library_target_path(
        session,
        recovery_source,
        issue,
        series,
        root,
        naming_policy,
        rename,
    )
    if not target_path.exists():
        return None

    effective_issue_type = await _resolve_naming_issue_type(session, issue)
    _materialize_series_path(series, target_path.parent, root)
    naming_snapshot = _build_naming_snapshot(
        source_path=source_path,
        prepared_source=recovery_source,
        target_path=target_path,
        issue=issue,
        series=series,
        root=root,
        naming_policy=naming_policy,
        rename=rename,
        effective_issue_type=effective_issue_type,
        transfer_method="recovered",
        move_to_library=True,
        normalized_source=recovery_source != source_path,
        update_embedded_comicinfo_from_match=update_embedded_comicinfo_from_match,
        normalize_to_cbz=normalize_to_cbz,
    )

    existing_result = await session.execute(
        select(LibraryFile).where(LibraryFile.file_path == str(target_path))
    )
    existing = existing_result.scalars().first()
    if existing is not None:
        existing.issue_id = issue.id
        existing.match_confidence = confidence
        existing.naming_snapshot = naming_snapshot
        issue.status = IssueStatus.OWNED
        await session.flush()
        logger.warning(
            "library_file_recovered_existing_record",
            source=str(source_path),
            recovered_path=str(target_path),
            library_file_id=existing.id,
            issue_id=issue.id,
        )
        return LibraryFileRegistrationOutcome(
            library_file=existing,
            series_folder_created=False,
            series_folder_path=target_path.parent,
        )

    stat = await asyncio.to_thread(target_path.stat)
    extension = target_path.suffix.lstrip(".").lower()
    file_format = _FORMAT_MAP.get(extension, FileFormat.CBZ)

    lf = LibraryFile(
        file_path=str(target_path),
        file_name=target_path.name,
        file_size=stat.st_size,
        file_format=file_format,
        file_modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        match_confidence=confidence,
        parsed_series=series.title if isinstance(series, Series) else None,
        parsed_issue_number=issue.issue_number,
        parsed_year=series.year_start if isinstance(series, Series) else None,
        issue_id=issue.id,
        library_root_id=root.id,
        naming_snapshot=naming_snapshot,
    )
    session.add(lf)
    issue.status = IssueStatus.OWNED
    await session.flush()

    logger.warning(
        "library_file_recovered_orphaned_materialization",
        source=str(source_path),
        recovered_path=str(target_path),
        issue_id=issue.id,
        library_file_id=lf.id,
    )

    return LibraryFileRegistrationOutcome(
        library_file=lf,
        series_folder_created=False,
        series_folder_path=target_path.parent,
    )

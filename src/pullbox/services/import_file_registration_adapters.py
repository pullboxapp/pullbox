"""Adapter construction for import library-file registration."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pullbox.core.exceptions import ImportDestinationValidationError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    ProgressCallback = Callable[[str, int, int, str], Awaitable[None] | None]


@dataclass(frozen=True)
class ImportLibraryFileAdapters:
    """Registration callbacks plus operation timings collected by those callbacks."""

    converter: Any
    comicinfo_embedder: Any
    artifact_transfer: Any
    comicinfo_materializer: Any
    placement_temp_paths: Any
    operation_timings: list[dict[str, Any]]


_PUBLISH_LOCK = threading.Lock()


def build_import_library_file_adapters(
    *,
    session: Any,
    job: Any,
    convert_file_interruptible: Any,
    embed_comicinfo_interruptible: Any,
    transfer_artifact_interruptible: Any,
    materialize_cbz_with_comicinfo_interruptible: Any,
    clock: Callable[[], float] = time.monotonic,
) -> ImportLibraryFileAdapters:
    """Build interruptible register_library_file adapters for import execution."""
    operation_timings: list[dict[str, Any]] = []

    def placement_temp_paths(artifact_source: Path, artifact_target: Path) -> tuple[Path, Path]:
        identity = "\0".join(
            (
                str(getattr(job, "id", "unknown")),
                str(artifact_source.expanduser().resolve(strict=False)),
                str(artifact_target.expanduser().resolve(strict=False)),
            )
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        stage_path = artifact_target.with_name(
            f".pullbox-import-{getattr(job, 'id', 'unknown')}-{digest}{artifact_target.suffix}"
        )
        return (
            stage_path,
            stage_path.with_name(f"{stage_path.name}.pullbox-write.tmp"),
        )

    async def convert_import_file(
        convert_source: Path,
        target_format: str,
        destination: Path | None = None,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        return cast(
            "Path",
            await convert_file_interruptible(
                session,
                job,
                convert_source,
                target_format,
                destination=destination,
                progress_callback=progress_callback,
            ),
        )

    async def embed_import_comicinfo(
        artifact_path: Path,
        payload: dict[str, Any],
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> bool:
        started_at = clock()
        result = bool(
            await embed_comicinfo_interruptible(
                session,
                job,
                artifact_path,
                payload,
                progress_callback=progress_callback,
            )
        )
        operation_timings.append(
            {
                "kind": "comicinfo_rewrite",
                "artifact_path": str(artifact_path),
                "artifact_file_name": artifact_path.name,
                "artifact_size_bytes": artifact_path.stat().st_size
                if artifact_path.exists()
                else None,
                "duration_ms": round((clock() - started_at) * 1000),
                "changed": result,
            }
        )
        return result

    async def transfer_import_artifact(
        artifact_source: Path,
        artifact_target: Path,
        transfer_method: str,
        **transfer_kwargs: Any,
    ) -> Path:
        transfer_progress_callback = transfer_kwargs.get("transfer_progress_callback")
        source_size = artifact_source.stat().st_size if artifact_source.exists() else None
        started_at = clock()
        stage_path, _materializer_temp_path = placement_temp_paths(
            artifact_source,
            artifact_target,
        )
        _require_unused_stage(stage_path)
        try:
            await transfer_artifact_interruptible(
                session,
                job,
                artifact_source,
                stage_path,
                transfer_method,
                transfer_progress_callback=transfer_progress_callback
                if callable(transfer_progress_callback)
                else None,
            )
            _publish_stage_without_overwrite(stage_path, artifact_target)
        except BaseException:
            _restore_source_or_cleanup_stage(
                stage_path,
                artifact_source,
                transfer_method=transfer_method,
            )
            raise
        result = artifact_target
        operation_timings.append(
            {
                "kind": "transfer",
                "source_path": str(artifact_source),
                "target_path": str(artifact_target),
                "target_file_name": artifact_target.name,
                "transfer_method": transfer_method,
                "source_size_bytes": source_size,
                "target_size_bytes": result.stat().st_size if result.exists() else None,
                "duration_ms": round((clock() - started_at) * 1000),
            }
        )
        return result

    async def materialize_import_cbz_with_comicinfo(
        artifact_source: Path,
        artifact_target: Path,
        payload: dict[str, Any],
        *,
        transfer_method: str,
        progress_callback: ProgressCallback | None = None,
    ) -> bool:
        source_size = artifact_source.stat().st_size if artifact_source.exists() else None
        started_at = clock()
        stage_path, materializer_temp_path = placement_temp_paths(
            artifact_source,
            artifact_target,
        )
        _require_unused_stage(stage_path)
        _require_unused_stage(materializer_temp_path)
        try:
            result = bool(
                await materialize_cbz_with_comicinfo_interruptible(
                    session,
                    job,
                    artifact_source,
                    stage_path,
                    payload,
                    transfer_method=transfer_method,
                    temp_path=materializer_temp_path,
                    progress_callback=progress_callback,
                )
            )
            _publish_stage_without_overwrite(stage_path, artifact_target)
        except BaseException:
            _restore_source_or_cleanup_stage(
                stage_path,
                artifact_source,
                transfer_method=transfer_method,
            )
            raise
        operation_timings.append(
            {
                "kind": "cbz_comicinfo_materialize",
                "source_path": str(artifact_source),
                "target_path": str(artifact_target),
                "target_file_name": artifact_target.name,
                "transfer_method": transfer_method,
                "source_size_bytes": source_size,
                "target_size_bytes": artifact_target.stat().st_size
                if artifact_target.exists()
                else None,
                "duration_ms": round((clock() - started_at) * 1000),
                "changed": result,
            }
        )
        return result

    return ImportLibraryFileAdapters(
        converter=convert_import_file,
        comicinfo_embedder=embed_import_comicinfo,
        artifact_transfer=transfer_import_artifact,
        comicinfo_materializer=materialize_import_cbz_with_comicinfo,
        placement_temp_paths=placement_temp_paths,
        operation_timings=operation_timings,
    )


def _require_unused_stage(stage_path: Path) -> None:
    if os.path.lexists(stage_path):
        raise ImportDestinationValidationError(
            "staging_path_exists",
            f"Import staging path already exists and was preserved for review: {stage_path}",
        )


def _publish_stage_without_overwrite(stage_path: Path, target_path: Path) -> None:
    """Publish a same-directory stage with an atomic no-overwrite filesystem claim."""
    if not os.path.lexists(stage_path):
        raise FileNotFoundError(f"Import staging artifact is missing: {stage_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with _PUBLISH_LOCK:
        collision = _casefold_destination_collision(target_path)
        if collision is not None:
            raise ImportDestinationValidationError(
                "destination_appeared",
                f"Managed import destination appeared during import and was preserved: {collision}",
            )
        try:
            if stage_path.is_symlink():
                os.symlink(os.readlink(stage_path), target_path)
            else:
                os.link(stage_path, target_path, follow_symlinks=False)
        except FileExistsError as exc:
            raise ImportDestinationValidationError(
                "destination_appeared",
                "Managed import destination appeared during import and was preserved: "
                f"{target_path}",
            ) from exc
        stage_path.unlink()


def _casefold_destination_collision(target_path: Path) -> Path | None:
    key = target_path.name.casefold()
    try:
        with os.scandir(target_path.parent) as entries:
            for entry in entries:
                if entry.name.casefold() == key:
                    return target_path.parent / entry.name
    except FileNotFoundError:
        return None
    return None


def _restore_source_or_cleanup_stage(
    stage_path: Path,
    source_path: Path,
    *,
    transfer_method: str,
) -> None:
    if not os.path.lexists(stage_path):
        return
    if transfer_method == "move":
        if os.path.lexists(source_path):
            # A source reappeared after the move. Its identity is unknown, so
            # preserve both it and the journaled stage for explicit review.
            return
        source_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _publish_stage_without_overwrite(stage_path, source_path)
        except (ImportDestinationValidationError, OSError):
            # Both paths are preserved when restoration cannot be proven safe.
            return
        return
    with suppress(FileNotFoundError):
        stage_path.unlink()

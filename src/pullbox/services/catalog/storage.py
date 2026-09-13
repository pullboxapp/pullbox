"""Bounded artifact decompression and atomic catalog state on the data volume."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import zstandard

from pullbox.services.catalog.contract import CatalogError
from pullbox.services.catalog.database import safe_path

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")
MAX_DATABASE_BYTES = 4 * 1024**3


async def disk_work[T](func: Callable[..., T], *args: Any) -> T:
    """Finish an owned disk operation before cancellation releases the update lock."""
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".catalog-", dir=path.parent)
    stage = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, path)
        sync_directory(path.parent)
    finally:
        stage.unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_json(path: Path) -> dict[str, Any]:
    safe_path(path)
    if not path.exists():
        return {}
    if path.stat().st_size > 1024 * 1024:
        raise CatalogError("Catalog state is invalid. Check the data volume.")
    try:
        result = json.loads(path.read_bytes())
    except (ValueError, OSError) as exc:
        raise CatalogError("Catalog state could not be read. Check the data volume.") from exc
    if not isinstance(result, dict):
        raise CatalogError("Catalog state is invalid.")
    return result


def decompress(archive: Path, output: Path) -> None:
    safe_path(archive)
    safe_path(output)
    total = 0
    try:
        with archive.open("rb") as source, output.open("xb") as destination:
            # The C backend passes this limit to ZSTD_DCtx_setMaxWindowSize in bytes.
            with zstandard.ZstdDecompressor(max_window_size=128 * 1024 * 1024).stream_reader(
                source
            ) as reader:
                while chunk := reader.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_DATABASE_BYTES:
                        raise CatalogError("Catalog exceeds the supported storage size.")
                    if (
                        total % (32 * 1024 * 1024) == 0
                        and shutil.disk_usage(output.parent).free < 64 * 1024 * 1024
                    ):
                        raise CatalogError("Not enough disk space to install the catalog.")
                    destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
    except zstandard.ZstdError as exc:
        raise CatalogError("Catalog decompression failed. Retry the download.") from exc


def stage_path(root: Path, suffix: str) -> Path:
    directory = safe_path(root / "staging")
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="catalog-", suffix=suffix, dir=directory)
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def activate_file(stage: Path, target: Path) -> None:
    safe_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage, target)
    sync_directory(target.parent)

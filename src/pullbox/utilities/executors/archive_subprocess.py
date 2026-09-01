"""Interruptible wrappers for archive-heavy Step 4 operations.

Step 4 import pause/cancel needs to interrupt long-running archive work
immediately, not after a Python thread finishes a PDF conversion or CBZ
rewrite. These helpers run the heavy sync operations in a child Python
process so the parent import runner can terminate the active file task at
the next control poll without waiting for the underlying library call to
return. Tiny CBZ rewrites can use an inline fast path because process startup
cost dominates the work and the operation finishes quickly.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, cast

from pullbox.core.exceptions import JobCancelledError, JobPausedError

ControlCheck = Callable[[], Awaitable[None]]
ProgressCallback = Callable[[str, int, int, str], Awaitable[None] | None]
_ArchiveOperation = Literal["convert", "transfer", "embed", "materialize_embed"]

_CONTROL_POLL_INTERVAL_SECONDS = 0.2
_PROCESS_TERMINATE_TIMEOUT_SECONDS = 2.0
_INLINE_MATERIALIZE_MAX_BYTES = 4 * 1024 * 1024
_CORRUPT_ARCHIVE_ERROR_TYPES = {
    "BadRarFile",
    "NeedFirstVolume",
    "NotRarFile",
    "RarCRCError",
}
_CORRUPT_ARCHIVE_MESSAGE_MARKERS = (
    "failed the read enough data",
    "corrupt file",
    "crc check failed",
    "data is corrupted",
)


async def convert_file_interruptible(
    source: Path,
    target_format: str,
    destination: Path | None = None,
    *,
    cancellation_check: ControlCheck | None = None,
    pdf_quality: str = "medium",
    progress_callback: ProgressCallback | None = None,
    allow_resource_safety_exception: bool = False,
) -> Path:
    """Convert an archive in a child process that can be terminated on cancel."""
    dest_dir = destination or source.parent
    target_path = dest_dir / f"{source.stem}.{target_format}"
    progress_state_path = _create_progress_state_path(dest_dir)
    payload = {
        "source": str(source),
        "target_format": target_format,
        "target_path": str(target_path),
        "pdf_quality": pdf_quality,
        "progress_path": str(progress_state_path),
        "allow_resource_safety_exception": allow_resource_safety_exception,
    }
    result = await _run_archive_operation(
        "convert",
        payload,
        cancellation_check=cancellation_check,
        progress_state_path=progress_state_path,
        progress_callback=progress_callback,
        cleanup_paths=[target_path],
    )
    return Path(str(result["target_path"]))


async def transfer_file_interruptible(
    source: Path,
    target: Path,
    method: str,
    *,
    cancellation_check: ControlCheck | None = None,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """Move/copy/link a materialized artifact in a killable child process."""
    total_bytes = source.stat().st_size if source.exists() else 0
    payload = {
        "source": str(source),
        "target": str(target),
        "method": method,
    }
    result = await _run_archive_operation(
        "transfer",
        payload,
        cancellation_check=cancellation_check,
        progress_path=target,
        progress_total=total_bytes,
        progress_callback=progress_callback,
        cleanup_paths=[target],
    )
    return Path(str(result["target_path"]))


async def embed_comicinfo_in_cbz_interruptible(
    cbz_path: Path,
    data: dict[str, Any],
    *,
    cancellation_check: ControlCheck | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Rewrite ComicInfo.xml in a child process that can be terminated on cancel."""
    temp_path = cbz_path.with_name(f"{cbz_path.name}.pullbox-write.tmp")
    progress_state_path = _create_progress_state_path(cbz_path.parent)
    payload = {
        "cbz_path": str(cbz_path),
        "data": data,
        "temp_path": str(temp_path),
        "progress_path": str(progress_state_path),
    }
    result = await _run_archive_operation(
        "embed",
        payload,
        cancellation_check=cancellation_check,
        progress_state_path=progress_state_path,
        progress_callback=progress_callback,
        cleanup_paths=[temp_path],
    )
    return bool(result.get("changed"))


async def materialize_cbz_with_comicinfo_interruptible(
    source: Path,
    target: Path,
    data: dict[str, Any],
    *,
    transfer_method: str,
    temp_path: Path | None = None,
    cancellation_check: ControlCheck | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Materialize a CBZ and write ComicInfo through the safest available path."""
    if _should_inline_cbz_materialize(source, target, transfer_method=transfer_method):
        return await _materialize_cbz_with_comicinfo_inline(
            source,
            target,
            data,
            transfer_method=transfer_method,
            temp_path=temp_path,
            cancellation_check=cancellation_check,
            progress_callback=progress_callback,
        )

    temp_path = temp_path or target.with_name(f"{target.name}.pullbox-write.tmp")
    progress_state_path = _create_progress_state_path(target.parent)
    payload = {
        "source": str(source),
        "target": str(target),
        "data": data,
        "transfer_method": transfer_method,
        "temp_path": str(temp_path),
        "progress_path": str(progress_state_path),
    }
    result = await _run_archive_operation(
        "materialize_embed",
        payload,
        cancellation_check=cancellation_check,
        progress_state_path=progress_state_path,
        progress_callback=progress_callback,
        cleanup_paths=[temp_path],
    )
    return bool(result.get("changed"))


def _should_inline_cbz_materialize(
    source: Path,
    target: Path,
    *,
    transfer_method: str,
) -> bool:
    if _INLINE_MATERIALIZE_MAX_BYTES <= 0:
        return False
    if transfer_method not in {"copy", "move"}:
        return False
    if source.suffix.lower() != ".cbz" or target.suffix.lower() != ".cbz":
        return False
    try:
        return source.stat().st_size <= _INLINE_MATERIALIZE_MAX_BYTES
    except OSError:
        return False


async def _materialize_cbz_with_comicinfo_inline(
    source: Path,
    target: Path,
    data: dict[str, Any],
    *,
    transfer_method: str,
    temp_path: Path | None = None,
    cancellation_check: ControlCheck | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Materialize a tiny CBZ without paying child Python startup cost."""
    from pullbox.utilities.comicinfo import materialize_cbz_with_comicinfo

    if cancellation_check is not None:
        await cancellation_check()

    loop = asyncio.get_running_loop()
    progress_futures: list[Any] = []

    def sync_progress_callback(stage: str, current: int, total: int, unit: str) -> None:
        if progress_callback is None:
            return
        progress_futures.append(
            asyncio.run_coroutine_threadsafe(
                _dispatch_progress_callback(progress_callback, stage, current, total, unit),
                loop,
            )
        )

    changed = await asyncio.to_thread(
        materialize_cbz_with_comicinfo,
        source,
        target,
        data,
        transfer_method=transfer_method,
        temp_path=temp_path,
        progress_callback=sync_progress_callback if progress_callback is not None else None,
    )
    for future in progress_futures:
        await asyncio.wrap_future(future)
    return bool(changed)


async def _run_archive_operation(
    operation: _ArchiveOperation,
    payload: dict[str, Any],
    *,
    cancellation_check: ControlCheck | None = None,
    progress_path: Path | None = None,
    progress_total: int | None = None,
    progress_state_path: Path | None = None,
    progress_callback: ProgressCallback | None = None,
    cleanup_paths: list[Path] | None = None,
) -> dict[str, Any]:
    stdout: bytes = b""
    stderr: bytes = b""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pullbox.utilities.executors.archive_subprocess",
        "--worker",
        operation,
        json.dumps(payload),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    communicate_task = asyncio.create_task(proc.communicate())
    last_reported_transfer = -1
    last_reported_state: tuple[str, int, int, str] | None = None
    effective_cleanup_paths = list(cleanup_paths or [])
    if progress_state_path is not None:
        effective_cleanup_paths.append(progress_state_path)

    try:
        while True:
            done, _pending = await asyncio.wait(
                {communicate_task},
                timeout=_CONTROL_POLL_INTERVAL_SECONDS,
            )
            if done:
                stdout, stderr = communicate_task.result()
                break

            if (
                progress_callback is not None
                and progress_path is not None
                and progress_total is not None
            ):
                try:
                    transferred = min(progress_path.stat().st_size, progress_total)
                except FileNotFoundError:
                    transferred = 0
                if transferred != last_reported_transfer:
                    last_reported_transfer = transferred
                    await _dispatch_progress_callback(
                        progress_callback,
                        "transferring",
                        transferred,
                        progress_total,
                        "bytes",
                    )

            if progress_callback is not None and progress_state_path is not None:
                progress_state = _read_progress_state(progress_state_path)
                if progress_state is not None and progress_state != last_reported_state:
                    last_reported_state = progress_state
                    await _dispatch_progress_callback(progress_callback, *progress_state)

            if cancellation_check is not None:
                try:
                    await cancellation_check()
                except (JobCancelledError, JobPausedError):
                    await _terminate_worker_process(proc, communicate_task)
                    _cleanup_paths(effective_cleanup_paths)
                    raise
        if progress_callback is not None and progress_total is not None:
            final_bytes = progress_total
            if progress_path is not None:
                try:
                    final_bytes = min(progress_path.stat().st_size, progress_total)
                except FileNotFoundError:
                    final_bytes = 0
            if final_bytes != last_reported_transfer:
                await _dispatch_progress_callback(
                    progress_callback,
                    "transferring",
                    final_bytes,
                    progress_total,
                    "bytes",
                )
        if progress_callback is not None and progress_state_path is not None:
            progress_state = _read_progress_state(progress_state_path)
            if progress_state is not None and progress_state != last_reported_state:
                await _dispatch_progress_callback(progress_callback, *progress_state)
    except Exception:
        if proc.returncode is None:
            await _terminate_worker_process(proc, communicate_task)
        raise

    if proc.returncode != 0:
        _cleanup_paths(effective_cleanup_paths)
        _raise_worker_error(operation, payload, stdout, stderr, returncode=proc.returncode)

    try:
        try:
            stdout_text = stdout.decode("utf-8")
            payload_text = stdout_text.strip()
            if payload_text:
                payload_text = payload_text.splitlines()[-1]
            decoded = json.loads(payload_text or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Archive worker returned invalid JSON for {operation}: {stdout!r}"
            ) from exc
        return cast("dict[str, Any]", decoded)
    finally:
        if progress_state_path is not None:
            _cleanup_paths([progress_state_path])


async def _dispatch_progress_callback(
    progress_callback: ProgressCallback,
    stage: str,
    current: int,
    total: int,
    unit: str,
) -> None:
    result = progress_callback(stage, current, total, unit)
    if inspect.isawaitable(result):
        await result


async def _terminate_worker_process(
    proc: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
) -> None:
    if proc.returncode is not None:
        with contextlib.suppress(Exception):
            await communicate_task
        return

    proc.terminate()
    try:
        await asyncio.wait_for(communicate_task, timeout=_PROCESS_TERMINATE_TIMEOUT_SECONDS)
        return
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await communicate_task


def _cleanup_paths(paths: list[Path]) -> None:
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def _raise_worker_error(
    operation: _ArchiveOperation,
    payload: dict[str, Any],
    stdout: bytes,
    stderr: bytes,
    *,
    returncode: int | None = None,
) -> None:
    details: dict[str, Any] | None = None
    raw = (
        stderr.decode("utf-8", errors="replace").strip()
        or stdout.decode("utf-8", errors="replace").strip()
    )
    if raw:
        with contextlib.suppress(json.JSONDecodeError):
            details = json.loads(raw)

    exc_type = str((details or {}).get("type") or "")
    message = str((details or {}).get("message") or raw or f"{operation} worker failed")
    if message == f"{operation} worker failed" and returncode is not None:
        if returncode in {-9, 137}:
            message = "worker process exited with signal 9 (likely out of memory)"
        elif returncode < 0:
            message = f"worker process exited with signal {-returncode}"
        elif returncode >= 128:
            message = f"worker process exited with status {returncode} (signal {returncode - 128})"
        else:
            message = f"worker process exited with status {returncode}"

    if exc_type == "FileNotFoundError":
        raise FileNotFoundError(message)
    if exc_type == "FileExistsError":
        raise FileExistsError(message)
    if exc_type == "ValueError":
        raise ValueError(message)
    if operation == "convert" and _is_corrupt_archive_error(exc_type, message):
        source = Path(str(payload.get("source") or "archive"))
        raise ValueError(_format_corrupt_archive_worker_message(source, message))

    raise RuntimeError(f"Archive worker failed during {operation}: {message} payload={payload!r}")


def _is_corrupt_archive_error(exc_type: str, message: str) -> bool:
    if exc_type in _CORRUPT_ARCHIVE_ERROR_TYPES:
        return True
    normalized = message.lower()
    return any(marker in normalized for marker in _CORRUPT_ARCHIVE_MESSAGE_MARKERS)


def _format_corrupt_archive_worker_message(source: Path, detail: str) -> str:
    detail = detail.strip() or "the archive extractor could not read the file"
    return (
        f"CBR archive appears corrupt or incomplete and could not be converted: "
        f"{source.name}. Try re-downloading or replacing the file. Details: {detail}"
    )


def _create_progress_state_path(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(
        prefix="pullbox-archive-progress-",
        suffix=".json",
        dir=str(parent),
    )
    os.close(fd)
    return Path(raw_path)


def _read_progress_state(path: Path) -> tuple[str, int, int, str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    stage = str(payload.get("stage") or "").strip()
    unit = str(payload.get("unit") or "").strip()
    if not stage or not unit:
        return None
    try:
        current = int(payload.get("current") or 0)
        total = int(payload.get("total") or 0)
    except (TypeError, ValueError):
        return None
    return (stage, current, total, unit)


def _worker_main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] != "--worker":
        raise SystemExit(
            "usage: python -m pullbox.utilities.executors.archive_subprocess "
            "--worker <op> <json-payload>"
        )

    operation = argv[1]
    payload = json.loads(argv[2])

    try:
        if operation == "convert":
            result = _worker_convert(payload)
        elif operation == "transfer":
            result = _worker_transfer(payload)
        elif operation == "embed":
            result = _worker_embed(payload)
        elif operation == "materialize_embed":
            result = _worker_materialize_embed(payload)
        else:
            raise ValueError(f"Unsupported archive worker operation: {operation}")
    except Exception as exc:  # pragma: no cover - exercised via parent wrapper
        sys.stderr.write(
            json.dumps(
                {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            )
        )
        return 1

    sys.stdout.write(json.dumps(result))
    return 0


def _worker_convert(payload: dict[str, Any]) -> dict[str, Any]:
    from pullbox.utilities.executors.file_converter import _convert_sync

    source = Path(str(payload["source"]))
    target_format = str(payload["target_format"])
    target_path = Path(str(payload["target_path"]))
    pdf_quality = str(payload.get("pdf_quality") or "medium")
    progress_state_path = payload.get("progress_path")
    converted = _convert_sync(
        source,
        target_format,
        target_path,
        progress_callback=(
            None
            if not progress_state_path
            else lambda stage, current, total, unit: _write_progress_state(
                Path(str(progress_state_path)),
                stage,
                current,
                total,
                unit,
            )
        ),
        pdf_quality=pdf_quality,
        allow_resource_safety_exception=bool(payload.get("allow_resource_safety_exception")),
    )
    return {"target_path": str(converted)}


def _worker_transfer(payload: dict[str, Any]) -> dict[str, Any]:
    source = Path(str(payload["source"]))
    target = Path(str(payload["target"]))
    method = str(payload["method"])
    _transfer_sync(source, target, method)
    return {"target_path": str(target)}


def _worker_embed(payload: dict[str, Any]) -> dict[str, Any]:
    from pullbox.utilities.comicinfo import embed_comicinfo_in_cbz

    cbz_path = Path(str(payload["cbz_path"]))
    data = dict(payload["data"])
    temp_path_raw = payload.get("temp_path")
    temp_path = Path(str(temp_path_raw)) if temp_path_raw else None
    progress_state_path = payload.get("progress_path")
    changed = embed_comicinfo_in_cbz(
        cbz_path,
        data,
        temp_path=temp_path,
        progress_callback=(
            None
            if not progress_state_path
            else lambda stage, current, total, unit: _write_progress_state(
                Path(str(progress_state_path)),
                stage,
                current,
                total,
                unit,
            )
        ),
    )
    return {"changed": changed}


def _worker_materialize_embed(payload: dict[str, Any]) -> dict[str, Any]:
    from pullbox.utilities.comicinfo import materialize_cbz_with_comicinfo

    source = Path(str(payload["source"]))
    target = Path(str(payload["target"]))
    data = dict(payload["data"])
    transfer_method = str(payload["transfer_method"])
    temp_path_raw = payload.get("temp_path")
    temp_path = Path(str(temp_path_raw)) if temp_path_raw else None
    progress_state_path = payload.get("progress_path")
    changed = materialize_cbz_with_comicinfo(
        source,
        target,
        data,
        transfer_method=transfer_method,
        temp_path=temp_path,
        progress_callback=(
            None
            if not progress_state_path
            else lambda stage, current, total, unit: _write_progress_state(
                Path(str(progress_state_path)),
                stage,
                current,
                total,
                unit,
            )
        ),
    )
    return {"changed": changed}


def _write_progress_state(
    path: Path,
    stage: str,
    current: int,
    total: int,
    unit: str,
) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "stage": stage,
        "current": int(current),
        "total": int(total),
        "unit": unit,
    }
    temp_path.write_text(json.dumps(payload), encoding="utf-8")
    temp_path.replace(path)


def _transfer_sync(src: Path, dst: Path, method: str) -> None:
    match method:
        case "move":
            _safe_move(src, dst)
        case "copy":
            _copy_with_retries(src, dst, preserve_metadata=True)
        case "hardlink":
            os.link(str(src), str(dst))
        case "symlink":
            os.symlink(str(src), str(dst))
        case _:
            raise ValueError(f"Unsupported transfer method: {method}")


def _copy_with_retries(src: Path, dst: Path, *, preserve_metadata: bool) -> None:
    copier = shutil.copy2 if preserve_metadata else shutil.copy
    delays = (0.0, 0.5, 1.0, 2.0, 4.0)

    for attempt, delay_seconds in enumerate(delays, start=1):
        if delay_seconds:
            time.sleep(delay_seconds)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            copier(str(src), str(dst))
            return
        except FileNotFoundError:
            with contextlib.suppress(FileNotFoundError):
                dst.unlink()
            if attempt >= len(delays):
                raise


def _safe_move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != 18:  # Cross-device link
            raise

    _copy_with_retries(src, dst, preserve_metadata=False)
    with contextlib.suppress(FileNotFoundError):
        src.unlink()


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(_worker_main(sys.argv[1:]))

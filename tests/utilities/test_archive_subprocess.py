"""Tests for interruptible archive subprocess helpers used by Step 4 import."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from pullbox.core.exceptions import JobCancelledError, JobPausedError
from pullbox.utilities.executors import archive_subprocess
from pullbox.utilities.executors.archive_subprocess import (
    _raise_worker_error,
    _run_archive_operation,
    convert_file_interruptible,
    embed_comicinfo_in_cbz_interruptible,
    materialize_cbz_with_comicinfo_interruptible,
    transfer_file_interruptible,
)


def _create_test_cbz(path: Path, *, include_comicinfo: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("page_000.jpg", "FAKE_JPEG")
        if include_comicinfo:
            archive.writestr(
                "ComicInfo.xml",
                '<?xml version="1.0"?><ComicInfo><Series>Old</Series></ComicInfo>',
            )
    return path


def _create_test_cb7(path: Path) -> Path:
    import py7zr

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = path.parent / "page_000.jpg"
    payload.write_text("FAKE_JPEG")
    with py7zr.SevenZipFile(path, "w") as archive:
        archive.write(payload, payload.name)
    payload.unlink()
    return path


@pytest.mark.asyncio
async def test_convert_file_interruptible_converts_cb7_to_cbz(tmp_path: Path) -> None:
    source = _create_test_cb7(tmp_path / "source" / "test.cb7")
    destination = tmp_path / "output"
    destination.mkdir()
    progress_events: list[tuple[str, int, int, str]] = []

    async def _capture_progress(stage: str, current: int, total: int, unit: str) -> None:
        progress_events.append((stage, current, total, unit))

    result = await convert_file_interruptible(
        source,
        "cbz",
        destination=destination,
        progress_callback=_capture_progress,
    )

    assert result.exists()
    assert result.suffix == ".cbz"
    with zipfile.ZipFile(result) as archive:
        assert "page_000.jpg" in archive.namelist()
    assert any(stage in {"extracting", "packing"} for stage, *_rest in progress_events)
    assert any(stage == "packing" for stage, *_rest in progress_events)
    assert not list(destination.glob("pullbox-archive-progress-*.json"))


@pytest.mark.asyncio
async def test_embed_comicinfo_in_cbz_interruptible_updates_archive(tmp_path: Path) -> None:
    cbz = _create_test_cbz(tmp_path / "target.cbz", include_comicinfo=True)
    progress_events: list[tuple[str, int, int, str]] = []

    async def _capture_progress(stage: str, current: int, total: int, unit: str) -> None:
        progress_events.append((stage, current, total, unit))

    changed = await embed_comicinfo_in_cbz_interruptible(
        cbz,
        {"Series": "Chicken Devil", "Number": "4"},
        progress_callback=_capture_progress,
    )

    assert changed is True
    with zipfile.ZipFile(cbz) as archive:
        content = archive.read("ComicInfo.xml").decode("utf-8")
    assert "Chicken Devil" in content
    assert "4" in content
    assert progress_events
    assert progress_events[-1][0] == "rewriting"
    assert progress_events[-1][1] == progress_events[-1][2]
    assert not list(tmp_path.glob("pullbox-archive-progress-*.json"))


@pytest.mark.asyncio
async def test_small_materialize_cbz_with_comicinfo_uses_inline_fast_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _create_test_cbz(tmp_path / "source.cbz", include_comicinfo=True)
    target = tmp_path / "library" / "target.cbz"
    progress_events: list[tuple[str, int, int, str]] = []

    async def fail_if_subprocess_starts(*_args: Any, **_kwargs: Any) -> object:
        raise AssertionError("small CBZ materialization should not spawn a worker process")

    async def _capture_progress(stage: str, current: int, total: int, unit: str) -> None:
        progress_events.append((stage, current, total, unit))

    monkeypatch.setattr(
        "pullbox.utilities.executors.archive_subprocess.asyncio.create_subprocess_exec",
        fail_if_subprocess_starts,
    )

    changed = await materialize_cbz_with_comicinfo_interruptible(
        source,
        target,
        {"Series": "Fast Path", "Number": "7"},
        transfer_method="copy",
        progress_callback=_capture_progress,
    )

    assert changed is True
    assert source.exists()
    assert target.exists()
    with zipfile.ZipFile(target) as archive:
        content = archive.read("ComicInfo.xml").decode("utf-8")
    assert "Fast Path" in content
    assert "7" in content
    assert progress_events
    assert progress_events[-1][0] == "rewriting"
    assert progress_events[-1][1] == progress_events[-1][2]
    assert not list(target.parent.glob("pullbox-archive-progress-*.json"))


@pytest.mark.asyncio
async def test_materialize_cbz_with_comicinfo_threshold_zero_uses_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _create_test_cbz(tmp_path / "source.cbz", include_comicinfo=True)
    target = tmp_path / "library" / "target.cbz"
    subprocess_started = False

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b'{"changed": true}', b"")

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> FakeProcess:
        nonlocal subprocess_started
        subprocess_started = True
        return FakeProcess()

    monkeypatch.setattr(
        "pullbox.utilities.executors.archive_subprocess._INLINE_MATERIALIZE_MAX_BYTES",
        0,
        raising=False,
    )
    monkeypatch.setattr(
        "pullbox.utilities.executors.archive_subprocess.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    changed = await materialize_cbz_with_comicinfo_interruptible(
        source,
        target,
        {"Series": "Worker Path"},
        transfer_method="copy",
    )

    assert changed is True
    assert subprocess_started is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control_exc", "expected_exc"),
    [
        (JobCancelledError("cancelled"), JobCancelledError),
        (JobPausedError("paused"), JobPausedError),
    ],
)
async def test_archive_operation_terminates_worker_and_cleans_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_exc: Exception,
    expected_exc: type[Exception],
) -> None:
    partial = tmp_path / "partial.cbz"
    partial.write_bytes(b"partial")

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self._done = asyncio.Event()
            self.terminate_called = False
            self.kill_called = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await self._done.wait()
            return (b"{}", b"")

        def terminate(self) -> None:
            self.terminate_called = True
            self.returncode = 143
            self._done.set()

        def kill(self) -> None:
            self.kill_called = True
            self.returncode = 137
            self._done.set()

    fake_proc = FakeProcess()

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> FakeProcess:
        return fake_proc

    async def cancel_now() -> None:
        raise control_exc

    monkeypatch.setattr(
        "pullbox.utilities.executors.archive_subprocess.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(expected_exc):
        await _run_archive_operation(
            "convert",
            {"source": "/tmp/source.cb7", "target_path": str(partial)},
            cancellation_check=cancel_now,
            cleanup_paths=[partial],
        )

    assert fake_proc.terminate_called is True
    assert not partial.exists()


def test_raise_worker_error_reports_signal_kill() -> None:
    with pytest.raises(RuntimeError, match="signal 9"):
        _raise_worker_error(
            "convert",
            {"source": "/tmp/source.pdf"},
            b"",
            b"",
            returncode=137,
        )


def test_raise_worker_error_normalizes_corrupt_cbr_without_payload() -> None:
    details = {
        "type": "BadRarFile",
        "message": "Failed the read enough data: req=262144 got=29",
    }

    with pytest.raises(ValueError) as exc_info:
        _raise_worker_error(
            "convert",
            {
                "source": "/imports/test5/Alice in Leatherland 001.cbr",
                "target_format": "cbz",
                "target_path": "/tmp/output.cbz",
            },
            b"",
            json.dumps(details).encode("utf-8"),
            returncode=1,
        )

    message = str(exc_info.value)
    assert "CBR archive appears corrupt or incomplete" in message
    assert "Alice in Leatherland 001.cbr" in message
    assert "Try re-downloading or replacing the file" in message
    assert "payload=" not in message


@pytest.mark.asyncio
async def test_transfer_file_interruptible_reports_progress_and_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.cbz"
    target = tmp_path / "library" / "target.cbz"
    source.write_bytes(b"source-bytes")
    progress_events: list[tuple[str, int, int, str]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            return (json.dumps({"target_path": str(target)}).encode(), b"")

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> FakeProcess:
        return FakeProcess()

    def capture_progress(stage: str, current: int, total: int, unit: str) -> None:
        progress_events.append((stage, current, total, unit))

    monkeypatch.setattr(
        "pullbox.utilities.executors.archive_subprocess.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = await transfer_file_interruptible(
        source,
        target,
        "copy",
        progress_callback=capture_progress,
    )

    assert result == target
    assert progress_events[-1] == (
        "transferring",
        source.stat().st_size,
        source.stat().st_size,
        "bytes",
    )


@pytest.mark.asyncio
async def test_archive_operation_reports_progress_state_and_invalid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress_path = tmp_path / "progress.json"
    progress_events: list[tuple[str, int, int, str]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            archive_subprocess._write_progress_state(progress_path, "packing", 2, 4, "files")
            return (b"not-json", b"")

    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> FakeProcess:
        return FakeProcess()

    async def capture_progress(stage: str, current: int, total: int, unit: str) -> None:
        progress_events.append((stage, current, total, unit))

    monkeypatch.setattr(
        "pullbox.utilities.executors.archive_subprocess.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    with pytest.raises(RuntimeError, match="invalid JSON"):
        await _run_archive_operation(
            "embed",
            {"cbz_path": "/tmp/source.cbz"},
            progress_state_path=progress_path,
            progress_callback=capture_progress,
        )

    assert progress_events == [("packing", 2, 4, "files")]
    assert not progress_path.exists()


@pytest.mark.asyncio
async def test_terminate_worker_process_handles_completed_and_kill_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_task = asyncio.create_task(asyncio.sleep(0, result=(b"{}", b"")))
    completed_proc = type("CompletedProc", (), {"returncode": 0})()
    await archive_subprocess._terminate_worker_process(completed_proc, completed_task)

    class HangingProcess:
        returncode: int | None = None

        def __init__(self) -> None:
            self.done = asyncio.Event()
            self.terminate_called = False
            self.kill_called = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await self.done.wait()
            return (b"{}", b"")

        def terminate(self) -> None:
            self.terminate_called = True

        def kill(self) -> None:
            self.kill_called = True
            self.returncode = 137
            self.done.set()

    async def fake_wait_for(_awaitable: object, timeout: float) -> object:
        raise TimeoutError

    proc = HangingProcess()
    task = asyncio.create_task(proc.communicate())
    monkeypatch.setattr(archive_subprocess.asyncio, "wait_for", fake_wait_for)

    await archive_subprocess._terminate_worker_process(proc, task)

    assert proc.terminate_called is True
    assert proc.kill_called is True


@pytest.mark.parametrize("ignore_terminate", [False, True])
@pytest.mark.parametrize("operation", ["convert", "nested_repair"])
async def test_task_cancellation_reaps_real_worker_before_returning(
    tmp_path, monkeypatch, ignore_terminate, operation
):
    """Task.cancel is distinct from a polled utility cancel/pause exception."""
    import os

    if ignore_terminate and os.name == "nt":
        pytest.skip("Windows termination cannot be ignored by a signal handler")
    started = asyncio.Event()
    captured = []
    spawn = asyncio.create_subprocess_exec
    marker = tmp_path / "ready"
    partial = tmp_path / "partial.cbz"
    partial.write_bytes(b"partial")
    script = (
        "import signal,time,pathlib; "
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_terminate else "")
        + f"pathlib.Path({str(marker)!r}).touch(); time.sleep(60)"
    )

    async def launch(*_args, **kwargs):
        proc = await spawn(sys.executable, "-c", script, **kwargs)
        captured.append(proc)
        started.set()
        return proc

    monkeypatch.setattr(archive_subprocess.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(archive_subprocess, "_PROCESS_TERMINATE_TIMEOUT_SECONDS", 0.05)
    task = asyncio.create_task(_run_archive_operation(operation, {}, cleanup_paths=[partial]))
    try:
        await asyncio.wait_for(started.wait(), 5)
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert captured[0].returncode is not None, "cancel returned while child was still alive"
        assert not partial.exists()
    finally:
        if captured and captured[0].returncode is None:
            captured[0].kill()
            await captured[0].wait()


async def test_cancellation_during_spawn_still_reaps_acquired_worker(tmp_path, monkeypatch):
    acquired = asyncio.Event()
    release = asyncio.Event()
    captured = []
    spawn = asyncio.create_subprocess_exec

    async def launch(*_args, **kwargs):
        proc = await spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        captured.append(proc)
        acquired.set()
        await release.wait()
        return proc

    monkeypatch.setattr(archive_subprocess.asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(_run_archive_operation("convert", {}))
    try:
        await asyncio.wait_for(acquired.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert captured[0].returncode is not None, "spawn cancellation orphaned the worker"
    finally:
        release.set()
        if captured and captured[0].returncode is None:
            captured[0].kill()
            await captured[0].wait()


async def test_second_cancel_cannot_interrupt_worker_reaping(tmp_path, monkeypatch):
    ready = asyncio.Event()
    terminating = asyncio.Event()
    done = asyncio.Event()

    class Process:
        returncode = None

        async def communicate(self):
            ready.set()
            await done.wait()
            return b"{}", b""

        def terminate(self):
            terminating.set()

        def kill(self):
            self.returncode = -9
            done.set()

    proc = Process()

    async def launch(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(archive_subprocess.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(archive_subprocess, "_PROCESS_TERMINATE_TIMEOUT_SECONDS", 0.05)
    task = asyncio.create_task(_run_archive_operation("convert", {}))
    await asyncio.wait_for(ready.wait(), 5)
    task.cancel()
    await asyncio.wait_for(terminating.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.returncode == -9
    assert done.is_set()


@pytest.mark.parametrize("control", ["task", "cancel", "pause", "failure"])
async def test_interrupted_move_preserves_destination_after_source_was_moved(
    tmp_path, monkeypatch, control
):
    source = tmp_path / "source.cbz"
    target = tmp_path / "destination.cbz"
    source.write_bytes(b"only copy")
    moved = asyncio.Event()
    done = asyncio.Event()

    class Process:
        returncode = None

        async def communicate(self):
            source.rename(target)
            moved.set()
            if control == "failure":
                self.returncode = 1
                return b"", b'{"type":"RuntimeError","message":"worker failed after move"}'
            await done.wait()
            return b"{}", b""

        def terminate(self):
            self.returncode = -15
            done.set()

    async def launch(*_args, **_kwargs):
        return Process()

    async def check():
        if control == "cancel":
            raise JobCancelledError("stop")
        if control == "pause":
            raise JobPausedError("stop")

    monkeypatch.setattr(archive_subprocess.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(archive_subprocess, "_CONTROL_POLL_INTERVAL_SECONDS", 0.01)
    task = asyncio.create_task(
        transfer_file_interruptible(source, target, "move", cancellation_check=check)
    )
    await asyncio.wait_for(moved.wait(), 5)
    if control == "task":
        task.cancel()
    expected = {
        "task": asyncio.CancelledError,
        "cancel": JobCancelledError,
        "pause": JobPausedError,
        "failure": RuntimeError,
    }[control]
    with pytest.raises(expected):
        await task
    assert target.exists(), "cleanup deleted the sole surviving copy after the move"
    assert target.read_bytes() == b"only copy"
    assert not source.exists()


def test_progress_state_cleanup_and_worker_error_branches(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"stage": "packing", "current": 1}), encoding="utf-8")
    bad_numbers = tmp_path / "bad-numbers.json"
    bad_numbers.write_text(
        json.dumps({"stage": "packing", "current": "nope", "total": 2, "unit": "files"}),
        encoding="utf-8",
    )
    valid = tmp_path / "valid.json"
    archive_subprocess._write_progress_state(valid, "packing", 1, 2, "files")

    assert archive_subprocess._read_progress_state(missing) is None
    assert archive_subprocess._read_progress_state(invalid) is None
    assert archive_subprocess._read_progress_state(incomplete) is None
    assert archive_subprocess._read_progress_state(bad_numbers) is None
    assert archive_subprocess._read_progress_state(valid) == ("packing", 1, 2, "files")

    cleanup_dir = tmp_path / "cleanup-dir"
    cleanup_dir.mkdir()
    cleanup_file = tmp_path / "cleanup.tmp"
    cleanup_file.write_text("delete me")
    archive_subprocess._cleanup_paths([cleanup_dir, cleanup_file, tmp_path / "gone.tmp"])
    assert not cleanup_dir.exists()
    assert not cleanup_file.exists()

    for exc_type, expected in (
        ("FileNotFoundError", FileNotFoundError),
        ("FileExistsError", FileExistsError),
        ("ValueError", ValueError),
    ):
        with pytest.raises(expected):
            _raise_worker_error(
                "embed",
                {"cbz_path": "/tmp/source.cbz"},
                b"",
                json.dumps({"type": exc_type, "message": "boom"}).encode(),
                returncode=1,
            )

    with pytest.raises(RuntimeError, match="signal 15"):
        _raise_worker_error("embed", {}, b"", b"", returncode=-15)
    with pytest.raises(RuntimeError, match="signal 2"):
        _raise_worker_error("embed", {}, b"", b"", returncode=130)
    with pytest.raises(RuntimeError, match="status 2"):
        _raise_worker_error("embed", {}, b"", b"", returncode=2)
    assert archive_subprocess._is_corrupt_archive_error("", "CRC check failed") is True
    assert archive_subprocess._is_corrupt_archive_error("", "ordinary failure") is False
    assert "extractor could not read" in archive_subprocess._format_corrupt_archive_worker_message(
        tmp_path / "broken.cbr",
        "",
    )


def test_worker_main_dispatches_and_reports_usage(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit, match="usage:"):
        archive_subprocess._worker_main([])

    monkeypatch.setattr(
        archive_subprocess,
        "_worker_convert",
        lambda payload: {"target_path": payload["target_path"]},
    )
    assert (
        archive_subprocess._worker_main(
            ["--worker", "convert", json.dumps({"target_path": "/tmp/out.cbz"})]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["target_path"] == "/tmp/out.cbz"

    assert archive_subprocess._worker_main(["--worker", "unknown", "{}"]) == 1
    error_payload = json.loads(capsys.readouterr().err)
    assert error_payload["type"] == "ValueError"
    assert "Unsupported archive worker operation" in error_payload["message"]


def test_worker_helpers_write_progress_and_transfer_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.cbz"
    target = tmp_path / "target.cbz"
    source.write_text("comic")
    progress_path = tmp_path / "progress.json"

    def fake_convert_sync(
        _source: Path,
        _target_format: str,
        target_path: Path,
        *,
        progress_callback: Any,
        **_kwargs: Any,
    ) -> Path:
        progress_callback("packing", 1, 1, "files")
        target_path.write_text("converted")
        return target_path

    monkeypatch.setattr(
        "pullbox.utilities.executors.file_converter._convert_sync",
        fake_convert_sync,
    )
    converted = archive_subprocess._worker_convert(
        {
            "source": str(source),
            "target_format": "cbz",
            "target_path": str(target),
            "progress_path": str(progress_path),
        }
    )
    assert converted == {"target_path": str(target)}
    assert archive_subprocess._read_progress_state(progress_path) == ("packing", 1, 1, "files")

    transfer_target = tmp_path / "copy" / "copied.cbz"
    assert archive_subprocess._worker_transfer(
        {"source": str(source), "target": str(transfer_target), "method": "copy"}
    ) == {"target_path": str(transfer_target)}
    assert transfer_target.read_text() == "comic"

    def fake_embed_comicinfo_in_cbz(
        _cbz_path: Path,
        _data: dict[str, Any],
        *,
        progress_callback: Any,
        **_kwargs: Any,
    ) -> bool:
        progress_callback("rewriting", 1, 1, "entries")
        return True

    def fake_materialize_cbz_with_comicinfo(
        _source: Path,
        _target: Path,
        _data: dict[str, Any],
        *,
        progress_callback: Any,
        **_kwargs: Any,
    ) -> bool:
        progress_callback("rewriting", 2, 2, "entries")
        return False

    monkeypatch.setattr(
        "pullbox.utilities.comicinfo.embed_comicinfo_in_cbz",
        fake_embed_comicinfo_in_cbz,
    )
    monkeypatch.setattr(
        "pullbox.utilities.comicinfo.materialize_cbz_with_comicinfo",
        fake_materialize_cbz_with_comicinfo,
    )
    assert archive_subprocess._worker_embed(
        {
            "cbz_path": str(source),
            "data": {"Series": "Test"},
            "temp_path": str(tmp_path / "tmp.cbz"),
            "progress_path": str(progress_path),
        }
    ) == {"changed": True}
    assert archive_subprocess._read_progress_state(progress_path) == ("rewriting", 1, 1, "entries")
    assert archive_subprocess._worker_materialize_embed(
        {
            "source": str(source),
            "target": str(target),
            "data": {"Series": "Test"},
            "transfer_method": "copy",
            "temp_path": str(tmp_path / "tmp.cbz"),
            "progress_path": str(progress_path),
        }
    ) == {"changed": False}
    assert archive_subprocess._read_progress_state(progress_path) == ("rewriting", 2, 2, "entries")

    hardlink_target = tmp_path / "hardlink.cbz"
    archive_subprocess._transfer_sync(source, hardlink_target, "hardlink")
    assert hardlink_target.exists()

    symlink_target = tmp_path / "symlink.cbz"
    archive_subprocess._transfer_sync(source, symlink_target, "symlink")
    assert symlink_target.is_symlink()

    with pytest.raises(ValueError, match="Unsupported transfer method"):
        archive_subprocess._transfer_sync(source, tmp_path / "bad.cbz", "teleport")


def test_safe_move_falls_back_on_cross_device_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.cbz"
    target = tmp_path / "target.cbz"
    source.write_text("comic")

    def fake_rename(_src: Path, _dst: Path) -> None:
        error = OSError("cross-device")
        error.errno = 18
        raise error

    monkeypatch.setattr(archive_subprocess.os, "rename", fake_rename)
    monkeypatch.setattr(archive_subprocess.time, "sleep", lambda _seconds: None)

    archive_subprocess._safe_move(source, target)

    assert target.read_text() == "comic"
    assert not source.exists()


def test_copy_with_retries_cleans_partial_file_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.cbz"
    target = tmp_path / "nested" / "target.cbz"
    source.write_text("comic")
    calls = 0

    def flaky_copy(src: str, dst: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            Path(dst).write_text("partial")
            raise FileNotFoundError("temporary source miss")
        shutil.copyfile(src, dst)

    monkeypatch.setattr(archive_subprocess.shutil, "copy2", flaky_copy)
    monkeypatch.setattr(archive_subprocess.time, "sleep", lambda _seconds: None)

    archive_subprocess._copy_with_retries(source, target, preserve_metadata=True)

    assert calls == 2
    assert target.read_text() == "comic"


@pytest.mark.skipif(os.name == "nt", reason="POSIX isolated worker group contract")
async def test_crashed_paired_worker_does_not_leave_a_running_helper(tmp_path, monkeypatch):
    from pullbox.utilities.executors.archive_metadata_staging import ArchiveMetadataStagingError

    spawn = asyncio.create_subprocess_exec
    marker = tmp_path / "child.pid"
    script = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(marker)!r}).write_text(str(child.pid)); "
        "time.sleep(0.1); sys.exit(1)"
    )

    async def launch(*_args, **kwargs):
        return await spawn(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(archive_subprocess.asyncio, "create_subprocess_exec", launch)
    child_pid = None
    try:
        with pytest.raises(ArchiveMetadataStagingError):
            await _run_archive_operation("paired_stage", {})
        child_pid = int(marker.read_text())
        for _attempt in range(50):
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child_pid)],
                capture_output=True,
                text=True,
                check=False,
                timeout=1,
            ).stdout.strip()
            if not state or state.startswith("Z"):
                break
            await asyncio.sleep(0.01)
        assert not state or state.startswith("Z"), "worker exited but its helper is still executing"
    finally:
        if child_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)

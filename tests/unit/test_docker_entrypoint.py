"""Tests for the shell-less Docker entrypoint runtime."""

from __future__ import annotations

import io
import os
import runpy
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from pullbox.core.shutdown import RESTART_EXIT_CODE


class _OriginalStream(io.StringIO):
    @property
    def encoding(self) -> str | None:
        return None


class _Mirror:
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 1024 * 1024,
        backup_count: int = 5,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.writes: list[str] = []
        self.closed = False

    def write(self, text: str) -> None:
        self.writes.append(text)

    def close(self) -> None:
        self.closed = True


def test_tee_stream_mirrors_writes_and_exposes_fallback_encoding() -> None:
    from pullbox.docker_entrypoint import _TeeStream

    original = _OriginalStream()
    mirror = _Mirror(Path("/tmp/startup.log"))
    stream = _TeeStream(original, mirror)

    assert stream.encoding == "utf-8"
    assert stream.writable() is True
    assert stream.write("hello") == 5
    stream.flush()

    assert original.getvalue() == "hello"
    assert mirror.writes == ["hello"]


def test_env_int_defaults_invalid_or_non_positive_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from pullbox.docker_entrypoint import _env_int

    for value in ("not-an-int", "0", "-3"):
        monkeypatch.setenv("PULLBOX_TEST_INT", value)
        assert _env_int("PULLBOX_TEST_INT", 7) == 7

    monkeypatch.setenv("PULLBOX_TEST_INT", "9")
    assert _env_int("PULLBOX_TEST_INT", 7) == 9


def test_configure_runtime_environment_sets_defaults_and_creates_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.docker_entrypoint import _configure_runtime_environment

    logs_dir = tmp_path / "logs"
    temp_dir = tmp_path / "tmp"
    backup_dir = tmp_path / "backups"
    monkeypatch.delenv("PULLBOX_DB_URL", raising=False)
    monkeypatch.setenv("PULLBOX_LOGS_DIR", str(logs_dir))
    monkeypatch.setenv("PULLBOX_TEMP_DIR", str(temp_dir))
    monkeypatch.setenv("PULLBOX_BACKUP_DIR", str(backup_dir))

    startup_log = _configure_runtime_environment()

    assert startup_log == logs_dir / "startup.log"
    assert logs_dir.is_dir()
    assert temp_dir.is_dir()
    assert backup_dir.is_dir()
    assert os.environ["PULLBOX_DB_URL"] == "sqlite+aiosqlite:////data/pullbox.db"


def test_install_startup_log_mirror_wraps_stdout_and_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pullbox.docker_entrypoint as entrypoint

    original_stdout = _OriginalStream()
    original_stderr = _OriginalStream()
    created: list[_Mirror] = []

    def _mirror_factory(
        path: Path,
        *,
        max_bytes: int = 1024 * 1024,
        backup_count: int = 5,
    ) -> _Mirror:
        mirror = _Mirror(path, max_bytes=max_bytes, backup_count=backup_count)
        created.append(mirror)
        return mirror

    monkeypatch.setenv("PULLBOX_LOG_SIZE_LIMIT_MB", "2")
    monkeypatch.setenv("PULLBOX_LOG_BACKUP_COUNT", "3")
    monkeypatch.setattr(entrypoint, "StartupLogMirror", _mirror_factory)
    monkeypatch.setattr(sys, "stdout", original_stdout)
    monkeypatch.setattr(sys, "stderr", original_stderr)

    mirror = entrypoint._install_startup_log_mirror(tmp_path / "startup.log")

    assert mirror is created[0]
    assert mirror.max_bytes == 2 * 1024 * 1024
    assert mirror.backup_count == 3
    assert isinstance(sys.stdout, entrypoint._TeeStream)
    assert isinstance(sys.stderr, entrypoint._TeeStream)


def test_run_migrations_exits_when_alembic_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import pullbox.docker_entrypoint as entrypoint

    monkeypatch.setattr(entrypoint, "_run_process", lambda _command: 4)

    with pytest.raises(SystemExit) as exc_info:
        entrypoint._run_migrations()

    assert exc_info.value.code == 4


def test_run_migrations_prints_completion_when_alembic_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import pullbox.docker_entrypoint as entrypoint

    commands: list[list[str]] = []
    monkeypatch.setattr(entrypoint, "_run_process", lambda command: commands.append(command) or 0)

    entrypoint._run_migrations()

    output = capsys.readouterr().out
    assert commands == [
        [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", "upgrade", "head"]
    ]
    assert "Running database migrations" in output
    assert "Database migrations complete" in output


class _FakeStdout:
    def __init__(self, on_iterate) -> None:
        self._on_iterate = on_iterate

    def __iter__(self):
        self._on_iterate()
        yield "child output\n"


class _FakeProcess:
    sent_signals: ClassVar[list[int]] = []

    def __init__(self, on_iterate=lambda: None, *, return_code: int = 0) -> None:
        self.stdout = _FakeStdout(on_iterate)
        self.return_code = return_code

    def poll(self) -> None:
        return None

    def send_signal(self, signum: int) -> None:
        self.__class__.sent_signals.append(signum)

    def wait(self) -> int:
        return self.return_code


def test_run_process_streams_output_forwards_signals_and_restores_handlers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import pullbox.docker_entrypoint as entrypoint

    captured_handlers: dict[signal.Signals, object] = {}
    signal_calls: list[tuple[signal.Signals, object]] = []

    def _fake_signal(signum: signal.Signals, handler: object) -> None:
        signal_calls.append((signum, handler))
        if callable(handler):
            captured_handlers[signum] = handler

    def _fake_getsignal(signum: signal.Signals) -> str:
        return f"old-{signum.name}"

    def _invoke_forwarded_signal() -> None:
        handler = captured_handlers[signal.Signals.SIGTERM]
        assert callable(handler)
        handler(int(signal.Signals.SIGTERM), None)

    process = _FakeProcess(_invoke_forwarded_signal, return_code=6)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(signal, "getsignal", _fake_getsignal)
    monkeypatch.setattr(signal, "signal", _fake_signal)

    assert entrypoint._run_process(["pullbox-test"]) == 6

    assert "child output" in capsys.readouterr().out
    assert _FakeProcess.sent_signals == [int(signal.Signals.SIGTERM)]
    assert signal_calls[-2:] == [
        (signal.Signals.SIGINT, "old-SIGINT"),
        (signal.Signals.SIGTERM, "old-SIGTERM"),
    ]


def test_run_process_returns_127_when_process_cannot_launch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import pullbox.docker_entrypoint as entrypoint

    def _raise_os_error(*_args: object, **_kwargs: object) -> None:
        raise OSError("missing binary")

    monkeypatch.setattr(subprocess, "Popen", _raise_os_error)

    assert entrypoint._run_process(["missing-binary"]) == 127
    assert "Failed to launch missing-binary" in capsys.readouterr().err


def test_main_runs_restart_loop_and_closes_mirror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pullbox.docker_entrypoint as entrypoint

    mirror = _Mirror(tmp_path / "startup.log")
    migrations: list[str] = []
    commands: list[list[str]] = []
    process_results = iter([RESTART_EXIT_CODE, 5])

    monkeypatch.setattr(entrypoint, "_configure_runtime_environment", lambda: mirror.path)
    monkeypatch.setattr(entrypoint, "_install_startup_log_mirror", lambda _path: mirror)
    monkeypatch.setattr(entrypoint, "_run_migrations", lambda: migrations.append("ran"))

    def _run_process(command: list[str]) -> int:
        commands.append(command)
        return next(process_results)

    monkeypatch.setattr(entrypoint, "_run_process", _run_process)
    monkeypatch.setattr(entrypoint, "build_startup_summary", lambda *, startup_log: startup_log)
    monkeypatch.setattr(entrypoint, "render_bootstrap_summary", lambda summary: f"boot {summary}")
    monkeypatch.setattr(entrypoint, "render_launching", lambda: "launching")
    monkeypatch.setattr(entrypoint, "render_restart_requested", lambda code: f"restart {code}")

    with pytest.raises(SystemExit) as exc_info:
        entrypoint.main([])

    assert exc_info.value.code == 5
    assert migrations == ["ran", "ran"]
    assert commands == [list(entrypoint.DEFAULT_COMMAND), list(entrypoint.DEFAULT_COMMAND)]
    assert mirror.closed is True


def test_module_entrypoint_runs_default_command_without_real_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pullbox.startup_log_tee as startup_log_tee
    import pullbox.startup_messages as startup_messages

    commands: list[list[str]] = []

    class _RunpyProcess:
        stdout = iter(())

        def __init__(self, command: list[str], *_args: object, **_kwargs: object) -> None:
            commands.append(command)

        def poll(self) -> None:
            return None

        def send_signal(self, _signum: int) -> None:
            return None

        def wait(self) -> int:
            return 0

    monkeypatch.delitem(sys.modules, "pullbox.docker_entrypoint", raising=False)
    monkeypatch.setattr(sys, "argv", ["pullbox.docker_entrypoint"])
    monkeypatch.setenv("PULLBOX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PULLBOX_TEMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("PULLBOX_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(startup_log_tee, "StartupLogMirror", _Mirror)
    monkeypatch.setattr(
        startup_messages,
        "get_build_metadata",
        lambda: SimpleNamespace(release_date=None, branch=None, commit=None),
    )
    monkeypatch.setattr(subprocess, "Popen", _RunpyProcess)

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("pullbox.docker_entrypoint", run_name="__main__")

    assert exc_info.value.code == 0
    assert commands == [
        [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini", "upgrade", "head"],
        ["python", "-m", "pullbox"],
    ]

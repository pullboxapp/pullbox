"""Exercise Docker smoke failure policy without Docker, network, or real sleeps."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STUB = """#!/bin/sh
command_name=${0##*/}
printf '%s %s\\n' "$command_name" "$*" >> "$SMOKE_TRACE"
case "$command_name" in
    docker)
        if [ "$1" = run ]; then exit "$SMOKE_RUN_STATUS"; fi
        ;;
    curl) exit "$SMOKE_HEALTH_STATUS" ;;
    pytest) exit "$SMOKE_TEST_STATUS" ;;
    seq) printf '1\\n30\\n' ;;
esac
"""


def _run_smoke(
    tmp_path: Path,
    *,
    keep_on_failure: str | None = None,
    health_status: int = 0,
    test_status: int = 0,
    run_status: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    make = shutil.which("make")
    assert make is not None, "Make is required to test the repository's local CI gate"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("docker", "curl", "pytest", "seq", "sleep"):
        stub = binaries / name
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
    trace = tmp_path / "commands.log"
    command = [
        make,
        "-s",
        "-f",
        str(ROOT / "Makefile"),
        "-o",
        "docker-build-check",
        "docker-smoke",
        f"VENV={tmp_path}",
    ]
    if keep_on_failure is not None:
        command.append(f"DOCKER_SMOKE_KEEP_ON_FAILURE={keep_on_failure}")
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env={
            "PATH": str(binaries),
            "SMOKE_TRACE": str(trace),
            "SMOKE_HEALTH_STATUS": str(health_status),
            "SMOKE_TEST_STATUS": str(test_status),
            "SMOKE_RUN_STATUS": str(run_status),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    commands = trace.read_text(encoding="utf-8").splitlines()
    run_index = next(i for i, item in enumerate(commands) if item.startswith("docker run "))
    return result, commands[run_index + 1 :]


@pytest.mark.parametrize("failure", ["health", "test"])
def test_opt_in_preserves_failed_container_without_reading_logs(
    tmp_path: Path, failure: str
) -> None:
    result, commands = _run_smoke(
        tmp_path,
        keep_on_failure="1",
        health_status=int(failure == "health"),
        test_status=int(failure == "test"),
    )

    assert result.returncode != 0
    assert not any(command.startswith("docker ") for command in commands)
    assert any(command.startswith("pytest ") for command in commands) == (failure == "test")
    assert "preserved" in result.stdout


@pytest.mark.parametrize("failure", ["health", "test"])
def test_default_failure_still_reports_logs_and_removes_container(
    tmp_path: Path, failure: str
) -> None:
    result, commands = _run_smoke(
        tmp_path,
        health_status=int(failure == "health"),
        test_status=int(failure == "test"),
    )

    assert result.returncode != 0
    assert commands[-2:] == ["docker logs pullbox-smoke", "docker rm -f pullbox-smoke"]


@pytest.mark.parametrize("keep_on_failure", [None, "0", "1"])
def test_success_always_tears_down_container(tmp_path: Path, keep_on_failure: str | None) -> None:
    result, commands = _run_smoke(tmp_path, keep_on_failure=keep_on_failure)

    assert result.returncode == 0, result.stderr
    assert commands[-1] == "docker rm -f pullbox-smoke"
    assert any(command.startswith("pytest ") for command in commands)
    assert "docker logs pullbox-smoke" not in commands


def test_run_failure_stops_before_health_and_cleanup(tmp_path: Path) -> None:
    result, commands = _run_smoke(tmp_path, keep_on_failure="1", run_status=1)

    assert result.returncode != 0
    assert commands == []

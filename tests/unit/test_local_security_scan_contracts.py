"""Exercise local scan gates without Docker, network access, or real findings."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE_SHA = "1" * 40
STUB = """#!/bin/sh
command_name=${0##*/}
printf '%s %s\\n' "$command_name" "$*" >> "$SCAN_TRACE"
case "$command_name" in
    git) printf '%s\\n' "$BASE_SHA"; exit "$BASE_STATUS" ;;
    gitleaks)
        if [ "$1" = dir ]; then exit "$DIR_STATUS"; fi
        exit "$HISTORY_STATUS"
        ;;
    docker) exit "$RUNTIME_STATUS" ;;
    grype) exit "$GRYPE_STATUS" ;;
esac
"""


def _environment(tmp_path: Path, **statuses: int) -> dict[str, str]:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("git", "gitleaks", "docker", "grype", "bash"):
        stub = binaries / name
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
    return {
        "PATH": str(binaries),
        "SCAN_TRACE": str(tmp_path / "commands.log"),
        "BASE_SHA": BASE_SHA,
        **dict.fromkeys(
            ("BASE_STATUS", "DIR_STATUS", "HISTORY_STATUS", "RUNTIME_STATUS", "GRYPE_STATUS"),
            "0",
        ),
        **{key: str(value) for key, value in statuses.items()},
    }


@pytest.mark.parametrize("base_ref", [None, "origin/main"])
def test_secret_scan_covers_worktree_and_resolved_commit_range(
    tmp_path: Path, base_ref: str | None
) -> None:
    env = _environment(tmp_path)
    bash = shutil.which("bash")
    assert bash is not None
    command = [bash, str(ROOT / "scripts/run_secret_scans.sh"), "gitleaks"]
    if base_ref:
        command.append(base_ref)
    result = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10, check=False
    )

    assert result.returncode == 0, result.stderr
    assert Path(env["SCAN_TRACE"]).read_text().splitlines() == [
        f"git rev-parse --verify --end-of-options {base_ref or 'origin/develop'}^{{commit}}",
        "gitleaks dir . --no-banner --redact --timeout=300",
        f"gitleaks git . --log-opts=--no-merges {BASE_SHA}..HEAD "
        "--no-banner --redact --timeout=300",
    ]


@pytest.mark.parametrize(
    ("failure", "expected_commands"),
    [("BASE_STATUS", 1), ("DIR_STATUS", 2), ("HISTORY_STATUS", 3)],
)
def test_secret_scan_fails_closed(tmp_path: Path, failure: str, expected_commands: int) -> None:
    env = _environment(tmp_path, **{failure: 7})
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(
        [bash, str(ROOT / "scripts/run_secret_scans.sh"), "gitleaks"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 7
    assert len(Path(env["SCAN_TRACE"]).read_text().splitlines()) == expected_commands


@pytest.mark.parametrize("failure", [None, "RUNTIME_STATUS", "GRYPE_STATUS"])
def test_docker_security_runs_before_smoke_and_stops_on_failure(
    tmp_path: Path, failure: str | None
) -> None:
    env = _environment(tmp_path, **({failure: 7} if failure else {}))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "verify_container_security_runtime.py").touch()
    # A harmless downstream recipe makes ordering observable without a smoke container.
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        f"include {ROOT / 'Makefile'}\n"
        "docker-smoke:\n\t@printf 'smoke-started\\n' >> \"$$SCAN_TRACE\"\n"
    )
    make = shutil.which("make")
    assert make is not None
    result = subprocess.run(
        [make, "-s", "-o", "docker-build-check", "docker-smoke", "GRYPE=grype"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    commands = Path(env["SCAN_TRACE"]).read_text().splitlines()
    runtime = "docker run --rm -i --entrypoint python pullbox:local -"
    scan = "grype docker:pullbox:local --config .grype.yaml --fail-on high --output table"
    assert runtime in commands
    if failure == "RUNTIME_STATUS":
        assert commands[-1] == runtime
        assert scan not in commands
    else:
        assert commands.index(runtime) < commands.index(scan)
    assert (result.returncode == 0) == (failure is None)
    assert (commands[-1] == "smoke-started") == (failure is None)


def test_local_and_remote_scanners_share_pins_and_blocking_policy() -> None:
    installer = (ROOT / "scripts/install_ci_tool.sh").read_text()
    assert 'version="0.110.0"' in installer
    for name in ("docker-validate.yml", "docker-release.yml"):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        scans = [
            step["with"]
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            if step.get("uses", "").startswith("anchore/scan-action@")
        ]
        assert scans
        for scan in scans:
            assert scan["grype-version"] == "v0.110.0"
            assert scan["fail-build"] is True
            assert scan["severity-cutoff"] == "high"
            assert scan["config"] == ".grype.yaml"
    makefile = (ROOT / "Makefile").read_text()
    assert 'bash scripts/run_secret_scans.sh "$(GITLEAKS)" "$(SECRET_SCAN_BASE)"' in makefile


def test_approved_glibc_exceptions_are_exact_version_and_package_scoped() -> None:
    config = yaml.safe_load((ROOT / ".grype.yaml").read_text())
    approved_cves = {"CVE-2026-5435", "CVE-2026-5450", "CVE-2026-5928"}
    entries = [entry for entry in config["ignore"] if entry["vulnerability"] in approved_cves]
    assert len(entries) == 18
    assert {
        (entry["vulnerability"], entry["package"]["name"], entry["package"]["version"])
        for entry in entries
    } == {
        (cve, package, version)
        for cve in approved_cves
        for package in ("libc6", "libc6-dev", "libc-dev-bin")
        for version in ("2.41-12+deb13u3+dhi1", "2.41-12+deb13u3+dhi2")
    }
    assert all(entry["package"]["type"] == "deb" for entry in entries)

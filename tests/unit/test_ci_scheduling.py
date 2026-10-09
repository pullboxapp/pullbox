"""Earlier Firefox scheduling must preserve all required CI evidence."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TEST_JOBS = ("test", "test-production", "e2e", "e2e-firefox")


def _jobs() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]


def test_firefox_starts_after_production_without_waiting_for_compatibility() -> None:
    jobs = _jobs()
    assert "test-production" in jobs, "Production Python needs an independent completion signal"
    assert "e2e-firefox" in jobs, "Firefox must be scheduled independently of Chromium"
    assert jobs["e2e-firefox"]["needs"] == [
        "release_sync_check",
        "full_ci_check",
        "test-production",
    ]
    assert jobs["e2e"]["needs"] == [
        "release_sync_check",
        "full_ci_check",
        "test",
        "test-production",
    ]
    assert jobs["test-production"]["needs"] == jobs["test"]["needs"]
    assert "test" not in jobs["test-production"]["needs"]
    assert jobs["e2e"]["strategy"]["matrix"]["include"] == [
        {"browser": "chromium", "pytest_extra_args": ""}
    ]
    assert jobs["e2e-firefox"]["strategy"]["matrix"]["include"] == [
        {"browser": "firefox", "pytest_extra_args": "--durations=25 --durations-min=1.0"}
    ]
    assert jobs["e2e-firefox"]["steps"] == jobs["e2e"]["steps"]
    assert jobs["e2e-firefox"]["env"] == jobs["e2e"]["env"]


@pytest.mark.parametrize("job_name", TEST_JOBS)
def test_split_jobs_keep_gates_routing_and_fail_closed_dependencies(job_name: str) -> None:
    jobs = _jobs()
    assert job_name in jobs
    job = jobs[job_name]
    assert job["if"] == (
        "needs.release_sync_check.outputs.is_sync != 'true' "
        "&& needs.full_ci_check.outputs.run_full == 'true'"
    )
    assert job["runs-on"] == jobs["test"]["runs-on"]
    assert job["permissions"] == {"contents": "read"}
    assert job["timeout-minutes"] == 30
    assert job["strategy"]["fail-fast"] is False
    assert not job.get("continue-on-error", False)
    assert all(not step.get("continue-on-error", False) for step in job["steps"])
    assert job["name"] == (
        "Test (Python ${{ matrix.python-version }})"
        if job_name.startswith("test")
        else "E2E Tests (${{ matrix.browser }})"
    )


def _run_gate(results: dict[str, str], *, sync: bool = False, full: bool = True) -> int:
    gate = _jobs()["ci-required"]
    assert gate["if"] == "always()"
    assert set(gate["needs"]) == {
        "release_sync_check",
        "full_ci_check",
        "quality-gate",
        "typecheck",
        "alembic-check",
        "accessibility",
        *TEST_JOBS,
    }
    step = next(step for step in gate["steps"] if step.get("name") == "Verify required CI jobs")
    assert step["env"]["NEEDS_CONTEXT"] == "${{ toJson(needs) }}"
    env = os.environ | {
        "NEEDS_CONTEXT": json.dumps({key: {"result": results[key]} for key in gate["needs"]}),
        "RELEASE_SYNC_PR": str(sync).lower(),
        "RUN_FULL": str(full).lower(),
        "LABEL_REQUIRED": "false",
    }
    return subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    ).returncode


def _success_results() -> dict[str, str]:
    return {name: "success" for name in _jobs() if name != "ci-required"}


@pytest.mark.parametrize("job_name", TEST_JOBS)
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_required_gate_rejects_every_unsuccessful_test_lane(job_name: str, result: str) -> None:
    results = _success_results()
    results[job_name] = result
    assert _run_gate(results) == 1


def test_required_gate_accepts_all_successful_lanes() -> None:
    assert _run_gate(_success_results()) == 0


def test_preflight_does_not_replace_full_ci() -> None:
    results = _success_results() | dict.fromkeys(TEST_JOBS, "skipped")
    assert _run_gate(results, full=False) == 1


def test_validated_release_sync_still_allows_intentional_skips() -> None:
    results = dict.fromkeys(_success_results(), "skipped")
    results.update(release_sync_check="success", full_ci_check="success")
    assert _run_gate(results, sync=True, full=False) == 0

"""Keep runner-specific worker defaults without weakening the Python CI gates."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("job_name", ["test", "test-production"])
def test_worker_default_uses_six_only_on_self_hosted_runners(job_name: str):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    events = workflow.get("on", workflow.get(True))
    workers = events["workflow_dispatch"]["inputs"].get("pytest_workers", {})
    assert workers.get("type") == "choice"
    assert workers.get("options") == ["auto", "5", "6"]
    assert workers.get("default") == "auto"
    assert workflow["env"]["PYTEST_WORKERS"] == "5"
    step = next(
        step
        for step in workflow["jobs"][job_name]["steps"]
        if step.get("name") == "Run tests with coverage"
    )
    assert step["env"]["PYTEST_WORKERS"] == (
        "${{ github.event_name == 'workflow_dispatch' "
        "&& inputs.pytest_workers != 'auto' && inputs.pytest_workers "
        "|| (runner.environment == 'self-hosted' && '6' || env.PYTEST_WORKERS) }}"
    )


def test_worker_benchmark_keeps_the_full_matrix_and_release_coverage_gate():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]
    assert "test-production" in jobs, "Production Python must complete independently"
    assert jobs["test"]["strategy"]["matrix"]["include"] == [
        {"python-version": "3.12", "coverage_fail_under": 0},
        {"python-version": "3.13", "coverage_fail_under": 0},
    ]
    assert jobs["test-production"]["strategy"]["matrix"]["include"] == [
        {"python-version": "3.14", "coverage_fail_under": 90},
    ]
    job = jobs["test-production"]
    assert job["steps"] == jobs["test"]["steps"]
    step = next(step for step in job["steps"] if step.get("name") == "Run tests with coverage")
    assert "pytest tests/" in step["run"]
    assert '--cov-fail-under="${COVERAGE_FAIL_UNDER}"' in step["run"]
    assert "--durations=25" in step["run"]
    assert "faulthandler_timeout=180" in step["run"]
    assert not step.get("continue-on-error", False)

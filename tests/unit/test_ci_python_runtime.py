"""Keep worker experiments opt-in without weakening the Python CI gates."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_worker_benchmark_is_bounded_and_defaults_to_five_workers():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    events = workflow.get("on", workflow.get(True))
    workers = events["workflow_dispatch"]["inputs"].get("pytest_workers", {})
    assert workers.get("type") == "choice"
    assert workers.get("options") == ["5", "6"]
    assert workers.get("default") == "5"
    assert workflow["env"]["PYTEST_WORKERS"] == "5"
    step = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("name") == "Run tests with coverage"
    )
    assert step["env"]["PYTEST_WORKERS"] == (
        "${{ github.event_name == 'workflow_dispatch' "
        "&& inputs.pytest_workers || env.PYTEST_WORKERS }}"
    )


def test_worker_benchmark_keeps_the_full_matrix_and_release_coverage_gate():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["test"]
    assert job["strategy"]["matrix"]["include"] == [
        {"python-version": "3.12", "coverage_fail_under": 0},
        {"python-version": "3.13", "coverage_fail_under": 0},
        {"python-version": "3.14", "coverage_fail_under": 90},
    ]
    step = next(step for step in job["steps"] if step.get("name") == "Run tests with coverage")
    assert "pytest tests/" in step["run"]
    assert '--cov-fail-under="${COVERAGE_FAIL_UNDER}"' in step["run"]
    assert "--durations=25" in step["run"]
    assert "faulthandler_timeout=180" in step["run"]
    assert not step.get("continue-on-error", False)

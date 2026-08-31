"""Require completed, same-commit development validation before publishing edge.

Only explicit runs of the four trusted workflows on develop are accepted. PR
aggregates (including preflight and release-sync fast paths) are not evidence
that the exact commit being packaged passed the full validation suite.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Any
from urllib.parse import urlencode

REQUIRED_JOBS: dict[str, dict[str, tuple[str, ...]]] = {
    "ci.yml": {
        "Quality Gate": ("Ruff lint", "Ruff format check", "Check for uncommitted CSS changes"),
        "Type Check": ("Mypy",),
        "Migration Check": (
            "Verify migrations apply from empty DB",
            "Verify app boots after migration",
        ),
        "Test (Python 3.12)": ("Run tests with coverage",),
        "Test (Python 3.13)": ("Run tests with coverage",),
        "Test (Python 3.14)": ("Run tests with coverage",),
        "Accessibility Checks": ("Run contrast audit", "Run accessibility browser tests"),
        "E2E Tests (chromium)": ("Run E2E tests",),
        "E2E Tests (firefox)": ("Run E2E tests",),
        "CI Required": (),
    },
    "security.yml": {
        "Gitleaks": ("Run gitleaks on current tree",),
        "pip-audit": ("Run pip-audit",),
        "Safety Check": ("Run safety check",),
        "Bandit": ("Run Bandit", "Upload Bandit report"),
        "Security Required": (),
    },
    "workflow-hygiene.yml": {
        "actionlint": ("Run actionlint",),
        "Workflow Hygiene Required": (),
    },
    "docker-validate.yml": {
        "Production Docker Validate (trusted)": (
            "Build production Docker image",
            "Verify container security runtime",
            "Run Grype scan",
            "Verify packaged static assets",
            "Wait for healthy",
        ),
        "Docker Validate Required": (),
    },
}


class ValidationError(ValueError):
    """The available evidence cannot authorize a development publication."""


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("GitHub returned an unexpected response shape.")
    return value


def _positive_id(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValidationError("GitHub returned an invalid run or check identifier.")
    return value


def latest_run(runs: list[dict[str, Any]]) -> dict[str, Any]:
    if not runs:
        raise ValidationError("No manual develop validation run exists for this commit.")
    return max(runs, key=lambda run: _positive_id(run.get("id")))


def validate_evidence(
    workflow: str,
    run: dict[str, Any],
    suite: dict[str, Any],
    jobs: list[dict[str, Any]],
    repository: str,
    sha: str,
) -> None:
    expected = {
        "path": f".github/workflows/{workflow}",
        "event": "workflow_dispatch",
        "head_sha": sha,
        "head_branch": "develop",
        "status": "completed",
        "conclusion": "success",
    }
    if any(run.get(key) != value for key, value in expected.items()):
        raise ValidationError(
            f"{workflow}: latest run is not a successful exact-commit develop run."
        )
    for field in ("repository", "head_repository"):
        if _object(run.get(field)).get("full_name") != repository:
            raise ValidationError(f"{workflow}: validation came from another repository.")
    app = _object(suite.get("app"))
    if (
        suite.get("id") != _positive_id(run.get("check_suite_id"))
        or suite.get("head_sha") != sha
        or app.get("slug") != "github-actions"
        or _object(app.get("owner")).get("login") != "github"
    ):
        raise ValidationError(f"{workflow}: missing trusted GitHub Actions check provenance.")

    run_id = _positive_id(run.get("id"))
    attempt = _positive_id(run.get("run_attempt"))
    for name, required_steps in REQUIRED_JOBS[workflow].items():
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) != 1:
            raise ValidationError(f"{workflow}: required job {name!r} is missing or duplicated.")
        job = matches[0]
        if any(
            job.get(key) != value
            for key, value in {
                "run_id": run_id,
                "run_attempt": attempt,
                "head_sha": sha,
                "status": "completed",
                "conclusion": "success",
            }.items()
        ):
            raise ValidationError(
                f"{workflow}: required job {name!r} did not succeed in this attempt."
            )
        steps = job.get("steps")
        if not isinstance(steps, list):
            raise ValidationError(f"{workflow}: job {name!r} has no step evidence.")
        for step_name in required_steps:
            selected = [_object(step) for step in steps if _object(step).get("name") == step_name]
            # Preserve the existing advisory Bandit policy, but require that it
            # actually ran and that its report job succeeded. Skipped is never OK.
            conclusions = {"success"}
            if (workflow, name, step_name) == ("security.yml", "Bandit", "Run Bandit"):
                conclusions.add("failure")
            if (
                len(selected) != 1
                or selected[0].get("status") != "completed"
                or selected[0].get("conclusion") not in conclusions
            ):
                raise ValidationError(
                    f"{workflow}: required step {step_name!r} did not run successfully."
                )


def get_json(endpoint: str) -> dict[str, Any]:
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "--method", "GET", endpoint],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        # Do not forward CLI errors or response bodies into release logs.
        raise ValidationError("Unable to read GitHub validation evidence; publication is blocked.")
    return _object(json.loads(result.stdout))


def list_all(endpoint: str, field: str) -> list[dict[str, Any]]:
    """Fail closed on incomplete pagination instead of accepting a partial job set."""
    records: list[dict[str, Any]] = []
    separator = "&" if "?" in endpoint else "?"
    for page in range(1, 11):
        payload = get_json(f"{endpoint}{separator}per_page=100&page={page}")
        values = payload.get(field)
        count = payload.get("total_count")
        if not isinstance(values, list) or type(count) is not int or not 0 <= count <= 1000:
            raise ValidationError(
                "GitHub validation evidence is malformed or exceeds the bounded query."
            )
        records.extend(_object(value) for value in values)
        if len(records) == count:
            return records
        if len(values) != 100 or len(records) > count:
            break
    raise ValidationError("GitHub returned incomplete validation evidence.")


def main() -> int:
    try:
        repository = os.environ["GITHUB_REPOSITORY"]
        sha = os.environ["GITHUB_SHA"]
        if (
            os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != "refs/heads/develop"
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
        ):
            raise ValidationError("Development publication requires a trusted develop dispatch.")
        query = urlencode({"head_sha": sha, "event": "workflow_dispatch", "branch": "develop"})
        for workflow in REQUIRED_JOBS:
            base = f"repos/{repository}"
            run = latest_run(
                list_all(f"{base}/actions/workflows/{workflow}/runs?{query}", "workflow_runs")
            )
            run_id = _positive_id(run.get("id"))
            attempt = _positive_id(run.get("run_attempt"))
            suite_id = _positive_id(run.get("check_suite_id"))
            suite = get_json(f"{base}/check-suites/{suite_id}")
            jobs = list_all(f"{base}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
            validate_evidence(workflow, run, suite, jobs, repository, sha)
            print(f"Validated {workflow}: run {run_id}, attempt {attempt}, commit {sha}")
    except (
        ValidationError,
        KeyError,
        OSError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        message = (
            str(exc) if isinstance(exc, ValidationError) else "Unable to load validation evidence."
        )
        print(f"::error::{message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

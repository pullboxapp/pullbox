"""Worker-level evidence for opt-in CI coverage engine comparisons."""

import json
import os
import platform
from pathlib import Path

import coverage
import pytest


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """Inspect the collector pytest-cov started, not a separate probe collector."""
    directory = os.environ.get("PULLBOX_CI_COVERAGE_REPORT_DIR")
    if not directory:
        return
    expected = os.environ.get("PULLBOX_CI_COVERAGE_EXPECTED_CORE", "auto")
    if expected not in {"auto", "ctrace", "sysmon"}:
        raise pytest.UsageError(f"Unsupported CI coverage core: {expected}")
    cov = coverage.Coverage.current()
    if cov is None:
        raise pytest.UsageError("Coverage is not active during the CI core measurement")
    core = dict(cov.sys_info())["core"]
    worker = getattr(session.config, "workerinput", {}).get("workerid", "controller")
    report = {
        "worker": worker,
        "expected_core": expected,
        "core": core,
        "python_version": platform.python_version(),
        "coverage_version": coverage.__version__,
        "branch": cov.get_option("run:branch"),
        "dynamic_context": cov.get_option("run:dynamic_context"),
        "concurrency": cov.get_option("run:concurrency"),
        "plugins": cov.get_option("run:plugins"),
    }
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / f"{worker}.json").write_text(json.dumps(report, indent=2) + "\n")
    if expected != "auto":
        expected_name = {"ctrace": "CTracer", "sysmon": "SysMonitor"}[expected]
        if core != expected_name:
            raise pytest.UsageError(f"Expected {expected_name}, got {core}")

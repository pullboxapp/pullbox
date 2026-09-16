"""Dependency setup contracts for independent browser CI jobs."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("job_name", ["accessibility", "e2e"])
def test_browser_jobs_install_locked_node_dependencies_before_pytest(job_name: str) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"][job_name]["steps"]
    node_steps = [
        index
        for index, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/setup-node@")
    ]
    install_steps = [index for index, step in enumerate(steps) if step.get("run") == "npm ci"]
    test_steps = [
        index for index, step in enumerate(steps) if "pytest tests/e2e/" in step.get("run", "")
    ]

    assert node_steps and install_steps and test_steps
    assert node_steps[0] < install_steps[0] < test_steps[0]
    assert not steps[install_steps[0]].get("continue-on-error", False)
    assert "if" not in steps[install_steps[0]]

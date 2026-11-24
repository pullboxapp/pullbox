"""The required lane must not turn pytest skips or deselection into green evidence."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("selection", "skip", "success"),
    [("", False, True), ("", True, False), ("-k one", False, False)],
)
def test_requires_each_selected_case_to_actually_pass(tmp_path, selection, skip, success):
    (tmp_path / "test_trial.py").write_text(
        "import pytest\ndef test_one(): pass\ndef test_two(): "
        + ("pytest.skip('no database')" if skip else "pass")
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from scripts.run_postgres_qualification import run_required; "
            "raise SystemExit(run_required(['test_trial.py::test_one','test_trial.py::test_two']))",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "PYTEST_ADDOPTS": selection,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) == success, (result.stdout, result.stderr)
    if not success:
        assert "Required PostgreSQL cases without a pass:\ntest_trial.py::test_two" in result.stderr

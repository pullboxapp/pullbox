"""The required lane must not turn pytest skips or deselection into green evidence."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_gate_requires_mixed_folder_rollback_and_stale_conflict_recovery_on_both_majors():
    from scripts.run_postgres_qualification import required_nodes

    nodes = set(required_nodes())
    module = "tests/integration/test_postgres_mylar.py::"
    for major in (16, 18):
        for provider in ("metron", "gcd"):
            assert f"{module}test_conflict_followup[{major}-{provider}]" in nodes
            for in_place in (False, True):
                for layout in ("only", "mixed", "unmatched"):
                    assert (
                        f"{module}test_first_series[{major}-{layout}-{in_place}-{provider}]"
                        in nodes
                    )
                assert f"{module}test_mixed_folder[{major}-True-{in_place}-{provider}]" in nodes
                assert f"{module}test_recheck_at_placement[{major}-{in_place}-{provider}]" in nodes
                for state in ("unknown", "foreign_stale"):
                    assert (
                        f"{module}test_recorded_match[{major}-{state}-{in_place}-{provider}]"
                        in nodes
                    )


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

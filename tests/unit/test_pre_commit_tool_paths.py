"""Contracts for worktree-local pre-commit tool resolution."""

from __future__ import annotations

from pathlib import Path


def test_system_hooks_resolve_tools_from_the_worktree_venv() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config = (repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    mypy_hook = (repo_root / "scripts" / "pre_commit_mypy.sh").read_text(encoding="utf-8")
    pytest_hook = (repo_root / "scripts" / "pre_commit_pytest.sh").read_text(encoding="utf-8")

    assert "entry: scripts/pre_commit_mypy.sh" in config
    assert "entry: scripts/pre_commit_pytest.sh" in config
    assert ".venv/bin/mypy" in mypy_hook
    assert ".venv/bin/pytest" in pytest_hook

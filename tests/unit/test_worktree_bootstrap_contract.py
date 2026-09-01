"""Contracts for isolated, repeatable worktree setup."""

from __future__ import annotations

from pathlib import Path


def test_worktree_bootstrap_is_idempotent_and_does_not_repoint_shared_hooks() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    bootstrap = makefile.split("bootstrap-worktree:", maxsplit=1)[1].split("\nsetup:", maxsplit=1)[
        0
    ]

    assert 'if [ ! -x "$(PYTHON)" ]' in bootstrap
    assert "$(PYTHON_BOOTSTRAP) -m venv $(VENV)" in bootstrap
    assert '$(PIP) install -e ".[dev,e2e]"' in bootstrap
    assert "npm ci" in bootstrap
    assert "$(PYTHON) -m playwright install chromium firefox" in bootstrap
    assert "cp .env.dev.example .env" in bootstrap
    assert "pre-commit install" not in bootstrap
    assert "setup: bootstrap-worktree" in makefile

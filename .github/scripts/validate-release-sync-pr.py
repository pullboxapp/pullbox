#!/usr/bin/env python3
"""Detect safe post-release sync PRs that can skip heavyweight checks.

This script is intentionally conservative. It only marks a PR as a release sync
when it carries main back to develop and the only additional change is the
standard next patch ``-dev`` version bump.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

VERSION_FILE = "src/pullbox/__init__.py"
ALLOWED_SYNC_CHANGES = {VERSION_FILE}
SYNC_BRANCH_PREFIX = "feature/sync-develop-"
VERSION_RE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
RELEASE_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class ValidationResult:
    is_sync: bool
    reason: str


def extract_version(text: str) -> str | None:
    """Extract the Pullbox package version from ``src/pullbox/__init__.py``."""
    match = VERSION_RE.search(text)
    return match.group(1) if match else None


def expected_next_dev_version(released_version: str) -> str | None:
    """Return the expected post-release dev version for a final release."""
    match = RELEASE_VERSION_RE.fullmatch(released_version)
    if not match:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}-dev"


def version_bump_is_release_sync(main_text: str, head_text: str) -> ValidationResult:
    main_version = extract_version(main_text)
    head_version = extract_version(head_text)
    if main_version is None or head_version is None:
        return ValidationResult(False, "could not read Pullbox version")

    expected = expected_next_dev_version(main_version)
    if expected is None:
        return ValidationResult(False, f"main version is not a final release: {main_version}")
    if head_version != expected:
        return ValidationResult(
            False,
            f"expected {VERSION_FILE} to be bumped from {main_version} to {expected}; "
            f"found {head_version}",
        )
    return ValidationResult(True, f"release sync with dev bump {main_version} -> {head_version}")


def _run_git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


def _git_succeeds(*args: str, cwd: Path) -> bool:
    return _run_git(*args, cwd=cwd, check=False).returncode == 0


def _git_stdout(*args: str, cwd: Path) -> str:
    return _run_git(*args, cwd=cwd).stdout.strip()


def _fetch_release_refs(cwd: Path) -> None:
    _run_git(
        "fetch",
        "--no-tags",
        "origin",
        "main:refs/remotes/origin/main",
        "develop:refs/remotes/origin/develop",
        cwd=cwd,
    )


def validate_release_sync_pr(
    env: dict[str, str], cwd: Path, *, fetch_refs: bool = True
) -> ValidationResult:
    if env.get("RELEASE_SYNC_EVENT_NAME") != "pull_request":
        return ValidationResult(False, "not a pull request")
    if env.get("RELEASE_SYNC_BASE_REF") != "develop":
        return ValidationResult(False, "base branch is not develop")

    head_ref = env.get("RELEASE_SYNC_HEAD_REF", "")
    if not head_ref.startswith(SYNC_BRANCH_PREFIX):
        return ValidationResult(False, f"head branch does not start with {SYNC_BRANCH_PREFIX}")

    repository = env.get("RELEASE_SYNC_REPOSITORY", "")
    head_repository = env.get("RELEASE_SYNC_HEAD_REPOSITORY", "")
    actor = env.get("RELEASE_SYNC_ACTOR", "")
    if head_repository != repository:
        return ValidationResult(False, "fork pull requests cannot use the sync fast path")
    if actor == "dependabot[bot]":
        return ValidationResult(False, "Dependabot pull requests cannot use the sync fast path")

    try:
        if fetch_refs:
            _fetch_release_refs(cwd)

        if not _git_succeeds(
            "merge-base", "--is-ancestor", "origin/develop", "origin/main", cwd=cwd
        ):
            return ValidationResult(False, "origin/main is not a descendant of origin/develop")
        if not _git_succeeds("merge-base", "--is-ancestor", "origin/main", "HEAD", cwd=cwd):
            return ValidationResult(False, "pull request head does not include origin/main")

        changed = set(
            filter(
                None,
                _git_stdout("diff", "--name-only", "origin/main..HEAD", cwd=cwd).splitlines(),
            )
        )
        if changed != ALLOWED_SYNC_CHANGES:
            return ValidationResult(
                False,
                "release sync fast path only allows "
                f"{sorted(ALLOWED_SYNC_CHANGES)} after origin/main; found {sorted(changed)}",
            )

        main_text = _git_stdout("show", f"origin/main:{VERSION_FILE}", cwd=cwd)
        head_text = (cwd / VERSION_FILE).read_text(encoding="utf-8")
        return version_bump_is_release_sync(main_text, head_text)
    except Exception as exc:
        return ValidationResult(False, f"could not validate release sync PR: {exc}")


def _write_output(result: ValidationResult) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"is_sync={str(result.is_sync).lower()}\n")
            output.write(f"reason={result.reason}\n")

    print(f"release_sync={str(result.is_sync).lower()}")
    print(f"reason={result.reason}")


def main() -> int:
    result = validate_release_sync_pr(os.environ, Path.cwd())
    _write_output(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

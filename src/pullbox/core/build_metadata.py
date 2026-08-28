"""Resolve release/build metadata for runtime status surfaces."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BuildMetadata:
    """Normalized runtime build metadata."""

    release_date: str | None
    branch: str | None
    commit: str | None


def _repo_root() -> Path:
    """Return the repository root when running from a checkout."""
    return Path(__file__).resolve().parents[3]


def _normalize_build_date(value: str | None) -> str | None:
    """Normalize build/release timestamps into ``YYYY-MM-DD`` when possible."""
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", trimmed):
        return trimmed
    try:
        parsed = datetime.fromisoformat(trimmed.replace("Z", "+00:00"))
    except ValueError:
        return trimmed[:10] if len(trimmed) >= 10 else trimmed
    return parsed.date().isoformat()


def _resolve_git_dir(repo_root: Path) -> Path | None:
    """Resolve the repository's git directory for normal and linked worktrees."""
    git_path = repo_root / ".git"
    if git_path.is_dir():
        return git_path
    if not git_path.is_file():
        return None
    try:
        content = git_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    git_dir = content.split("gitdir:", 1)[1].strip()
    resolved = (repo_root / git_dir).resolve() if not Path(git_dir).is_absolute() else Path(git_dir)
    return resolved if resolved.exists() else None


def _read_git_ref(git_dir: Path, ref: str) -> str | None:
    """Read a git ref from loose refs or packed-refs."""
    ref_dirs = [git_dir]
    commondir_path = git_dir / "commondir"
    if commondir_path.exists():
        try:
            common_value = commondir_path.read_text(encoding="utf-8").strip()
        except OSError:
            common_value = ""
        if common_value:
            common_dir = Path(common_value)
            if not common_dir.is_absolute():
                common_dir = (git_dir / common_dir).resolve()
            if common_dir != git_dir:
                ref_dirs.append(common_dir)

    for ref_dir in ref_dirs:
        ref_path = ref_dir / ref
        if ref_path.exists():
            try:
                value = ref_path.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            return value or None

        packed_refs = ref_dir / "packed-refs"
        if packed_refs.exists():
            try:
                for line in packed_refs.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("^"):
                        continue
                    parts = line.split(" ", 1)
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0]
            except OSError:
                return None
    return None


def _get_env_build_metadata() -> BuildMetadata:
    """Resolve build metadata from explicit runtime environment variables."""
    release_date = _normalize_build_date(
        os.environ.get("PULLBOX_BUILD_DATE") or os.environ.get("BUILD_DATE")
    )
    branch = (
        os.environ.get("PULLBOX_GIT_BRANCH")
        or os.environ.get("GIT_BRANCH")
        or os.environ.get("PULLBOX_BUILD_BRANCH")
    )
    commit = os.environ.get("PULLBOX_GIT_SHA") or os.environ.get("GIT_SHA")
    if commit:
        commit = commit[:7]
    return BuildMetadata(
        release_date=release_date,
        branch=(branch.strip() or None) if branch else None,
        commit=commit,
    )


def _git_output(*args: str) -> str | None:
    """Run a small git command and return trimmed stdout when available."""
    try:
        result = subprocess.run(
            ["git", "-C", str(_repo_root()), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    output = result.stdout.strip()
    return output or None


def _get_checkout_metadata() -> BuildMetadata:
    """Resolve git metadata directly from checkout files when git is unavailable."""
    repo_root = _repo_root()
    git_dir = _resolve_git_dir(repo_root)
    if git_dir is None:
        return BuildMetadata(release_date=None, branch=None, commit=None)

    try:
        head_value = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return BuildMetadata(release_date=None, branch=None, commit=None)

    branch: str | None = None
    commit: str | None = None

    if head_value.startswith("ref:"):
        ref = head_value.split("ref:", 1)[1].strip()
        branch = ref.removeprefix("refs/heads/") or None
        commit = _read_git_ref(git_dir, ref)
    elif head_value:
        commit = head_value

    release_date: str | None = None
    reflog_ref = git_dir / "logs" / "HEAD"
    if branch:
        branch_log = git_dir / "logs" / "refs" / "heads" / branch
        if branch_log.exists():
            reflog_ref = branch_log
    if reflog_ref.exists():
        try:
            lines = [line for line in reflog_ref.read_text(encoding="utf-8").splitlines() if line]
        except OSError:
            lines = []
        if lines:
            tail = lines[-1]
            match = re.search(r">\s+(\d+)\s+[+-]\d{4}\t", tail)
            if match:
                release_date = datetime.fromtimestamp(int(match.group(1)), UTC).date().isoformat()

    return BuildMetadata(
        release_date=release_date,
        branch=branch,
        commit=commit[:7] if commit else None,
    )


def get_build_metadata() -> BuildMetadata:
    """Resolve release date, branch, and commit for runtime surfaces."""
    metadata = _get_env_build_metadata()
    if metadata.release_date or metadata.branch or metadata.commit:
        return metadata

    branch = _git_output("rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        remote_head = _git_output("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if remote_head and remote_head.startswith("origin/"):
            branch = remote_head.removeprefix("origin/")
        else:
            branch = None

    release_date = _git_output("log", "-1", "--format=%cs")
    commit = _git_output("rev-parse", "--short=7", "HEAD")
    if release_date or branch or commit:
        return BuildMetadata(
            release_date=release_date,
            branch=branch,
            commit=commit,
        )

    return _get_checkout_metadata()


__all__ = [
    "BuildMetadata",
    "get_build_metadata",
]

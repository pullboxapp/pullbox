"""Tests for build metadata resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from pullbox.core import build_metadata


def _clear_build_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for name in (
        "PULLBOX_BUILD_DATE",
        "BUILD_DATE",
        "PULLBOX_GIT_BRANCH",
        "GIT_BRANCH",
        "PULLBOX_BUILD_BRANCH",
        "PULLBOX_GIT_SHA",
        "GIT_SHA",
    ):
        monkeypatch.delenv(name, raising=False)


def test_normalize_build_date_handles_common_inputs() -> None:
    assert build_metadata._normalize_build_date(None) is None
    assert build_metadata._normalize_build_date("   ") is None
    assert build_metadata._normalize_build_date("2026-06-17") == "2026-06-17"
    assert build_metadata._normalize_build_date("2026-06-17T12:34:56Z") == "2026-06-17"
    assert build_metadata._normalize_build_date("not-a-date-value") == "not-a-date"


def test_build_metadata_prefers_explicit_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_build_env(monkeypatch)
    monkeypatch.setenv("PULLBOX_BUILD_DATE", "2026-05-12T13:04:05Z")
    monkeypatch.setenv("PULLBOX_GIT_BRANCH", "feature/ui-followups")
    monkeypatch.setenv("PULLBOX_GIT_SHA", "abcdef1234567890")

    metadata = build_metadata.get_build_metadata()

    assert metadata.release_date == "2026-05-12"
    assert metadata.branch == "feature/ui-followups"
    assert metadata.commit == "abcdef1"


def test_env_metadata_uses_legacy_names_and_trims_blank_branch(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_build_env(monkeypatch)
    monkeypatch.setenv("BUILD_DATE", "2026-06-17")
    monkeypatch.setenv("GIT_BRANCH", "   ")
    monkeypatch.setenv("GIT_SHA", "1234567890abcdef")

    metadata = build_metadata._get_env_build_metadata()

    assert metadata.release_date == "2026-06-17"
    assert metadata.branch is None
    assert metadata.commit == "1234567"


def test_git_output_returns_trimmed_stdout(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(build_metadata, "_repo_root", lambda: Path("/repo"))

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        assert args[0] == ["git", "-C", "/repo", "rev-parse", "--short=7", "HEAD"]
        assert kwargs["check"] is True
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 1.0
        return SimpleNamespace(stdout=" abcdef1 \n")

    monkeypatch.setattr(build_metadata.subprocess, "run", fake_run)

    assert build_metadata._git_output("rev-parse", "--short=7", "HEAD") == "abcdef1"


def test_git_output_returns_none_on_failure_or_empty_output(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        build_metadata.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=" \n"),
    )

    assert build_metadata._git_output("log", "-1", "--format=%cs") is None

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise subprocess.SubprocessError

    monkeypatch.setattr(build_metadata.subprocess, "run", fail_run)

    assert build_metadata._git_output("log", "-1", "--format=%cs") is None


def test_build_metadata_uses_git_command_fallback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_build_env(monkeypatch)
    values = {
        ("rev-parse", "--abbrev-ref", "HEAD"): "feature/testing-coverage",
        ("log", "-1", "--format=%cs"): "2026-06-17",
        ("rev-parse", "--short=7", "HEAD"): "abcdef1",
    }

    monkeypatch.setattr(build_metadata, "_git_output", lambda *args: values[args])

    metadata = build_metadata.get_build_metadata()

    assert metadata == build_metadata.BuildMetadata(
        release_date="2026-06-17",
        branch="feature/testing-coverage",
        commit="abcdef1",
    )


def test_build_metadata_resolves_detached_head_from_origin(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_build_env(monkeypatch)
    values = {
        ("rev-parse", "--abbrev-ref", "HEAD"): "HEAD",
        ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"): "origin/main",
        ("log", "-1", "--format=%cs"): "2026-06-17",
        ("rev-parse", "--short=7", "HEAD"): "abcdef1",
    }

    monkeypatch.setattr(build_metadata, "_git_output", lambda *args: values[args])

    metadata = build_metadata.get_build_metadata()

    assert metadata.branch == "main"
    assert metadata.release_date == "2026-06-17"
    assert metadata.commit == "abcdef1"


def test_build_metadata_allows_detached_head_without_origin(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_build_env(monkeypatch)
    values = {
        ("rev-parse", "--abbrev-ref", "HEAD"): "HEAD",
        ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"): None,
        ("log", "-1", "--format=%cs"): None,
        ("rev-parse", "--short=7", "HEAD"): "abcdef1",
    }

    monkeypatch.setattr(build_metadata, "_git_output", lambda *args: values[args])

    metadata = build_metadata.get_build_metadata()

    assert metadata == build_metadata.BuildMetadata(
        release_date=None,
        branch=None,
        commit="abcdef1",
    )


def test_resolve_git_dir_supports_linked_worktrees(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    linked_git_dir = tmp_path / "actual.git"
    repo_root.mkdir()
    linked_git_dir.mkdir()
    (repo_root / ".git").write_text("gitdir: ../actual.git\n", encoding="utf-8")

    assert build_metadata._resolve_git_dir(repo_root) == linked_git_dir


def test_checkout_metadata_reads_shared_ref_for_linked_worktree(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    common_git_dir = tmp_path / "common.git"
    linked_git_dir = common_git_dir / "worktrees" / "repo"
    linked_git_dir.mkdir(parents=True)
    repo_root.mkdir()

    full_sha = "1234567890abcdef1234567890abcdef12345678"
    (repo_root / ".git").write_text(
        f"gitdir: {linked_git_dir}\n",
        encoding="utf-8",
    )
    (linked_git_dir / "HEAD").write_text("ref: refs/heads/develop\n", encoding="utf-8")
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    shared_ref = common_git_dir / "refs" / "heads" / "develop"
    shared_ref.parent.mkdir(parents=True)
    shared_ref.write_text(f"{full_sha}\n", encoding="utf-8")
    monkeypatch.setattr(build_metadata, "_repo_root", lambda: repo_root)

    metadata = build_metadata._get_checkout_metadata()

    assert metadata.branch == "develop"
    assert metadata.commit == "1234567"


def test_resolve_git_dir_returns_none_for_invalid_or_unreadable_git_file(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    git_file = repo_root / ".git"
    git_file.write_text("not-a-gitdir-pointer\n", encoding="utf-8")

    assert build_metadata._resolve_git_dir(repo_root) is None

    original_read_text = Path.read_text

    def fail_selected_path(self: Path, *args: object, **kwargs: object) -> str:
        if self == git_file:
            raise OSError
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_selected_path)

    assert build_metadata._resolve_git_dir(repo_root) is None


def test_read_git_ref_returns_none_for_unreadable_refs(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / ".git"
    loose_ref = git_dir / "refs" / "heads" / "main"
    packed_refs = git_dir / "packed-refs"
    loose_ref.parent.mkdir(parents=True)
    loose_ref.write_text("abcdef1234567890\n", encoding="utf-8")
    packed_refs.write_text("abcdef1234567890 refs/heads/other\n", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_loose_ref(self: Path, *args: object, **kwargs: object) -> str:
        if self == loose_ref:
            raise OSError
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_loose_ref)

    assert build_metadata._read_git_ref(git_dir, "refs/heads/main") is None

    loose_ref.unlink()

    def fail_packed_refs(self: Path, *args: object, **kwargs: object) -> str:
        if self == packed_refs:
            raise OSError
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_packed_refs)

    assert build_metadata._read_git_ref(git_dir, "refs/heads/main") is None


def test_read_git_ref_returns_none_for_missing_ref(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    assert build_metadata._read_git_ref(git_dir, "refs/heads/main") is None


def test_checkout_metadata_returns_empty_without_git_dir(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setattr(build_metadata, "_repo_root", lambda: repo_root)

    metadata = build_metadata._get_checkout_metadata()

    assert metadata == build_metadata.BuildMetadata(release_date=None, branch=None, commit=None)


def test_checkout_metadata_returns_empty_when_head_is_unreadable(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    git_dir = repo_root / ".git"
    git_dir.mkdir(parents=True)
    head_path = git_dir / "HEAD"
    head_path.write_text("ref: refs/heads/main\n", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_head(self: Path, *args: object, **kwargs: object) -> str:
        if self == head_path:
            raise OSError
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(build_metadata, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(Path, "read_text", fail_head)

    metadata = build_metadata._get_checkout_metadata()

    assert metadata == build_metadata.BuildMetadata(release_date=None, branch=None, commit=None)


def test_checkout_metadata_reads_detached_head_and_head_log(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    repo_root = tmp_path / "repo"
    git_dir = repo_root / ".git"
    git_dir.mkdir(parents=True)
    full_sha = "1234567890abcdef1234567890abcdef12345678"
    (git_dir / "HEAD").write_text(f"{full_sha}\n", encoding="utf-8")
    (git_dir / "logs").mkdir()
    (git_dir / "logs" / "HEAD").write_text(
        (
            "0000000000000000000000000000000000000000 "
            f"{full_sha} Test User <test@example.com> 1715558400 -0700\tcheckout: test\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(build_metadata, "_repo_root", lambda: repo_root)

    metadata = build_metadata._get_checkout_metadata()

    assert metadata.release_date == "2024-05-13"
    assert metadata.branch is None
    assert metadata.commit == "1234567"


def test_checkout_metadata_ignores_unreadable_reflog(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    repo_root = tmp_path / "repo"
    git_dir = repo_root / ".git"
    branch_log = git_dir / "logs" / "refs" / "heads" / "main"
    ref_path = git_dir / "refs" / "heads" / "main"
    branch_log.parent.mkdir(parents=True)
    ref_path.parent.mkdir(parents=True)
    full_sha = "1234567890abcdef1234567890abcdef12345678"
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    ref_path.write_text(f"{full_sha}\n", encoding="utf-8")
    branch_log.write_text("unreadable\n", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_reflog(self: Path, *args: object, **kwargs: object) -> str:
        if self == branch_log:
            raise OSError
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(build_metadata, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(Path, "read_text", fail_reflog)

    metadata = build_metadata._get_checkout_metadata()

    assert metadata.release_date is None
    assert metadata.branch == "main"
    assert metadata.commit == "1234567"


def test_checkout_metadata_reads_packed_refs(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    repo_root = tmp_path / "repo"
    git_dir = repo_root / ".git"
    git_dir.mkdir(parents=True)
    full_sha = "abcdef1234567890abcdef1234567890abcdef12"
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted\n^{full_sha}\n{full_sha} refs/heads/main\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build_metadata, "_repo_root", lambda: repo_root)

    metadata = build_metadata._get_checkout_metadata()

    assert metadata.release_date is None
    assert metadata.branch == "main"
    assert metadata.commit == "abcdef1"


def test_build_metadata_falls_back_to_checkout_files(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _clear_build_env(monkeypatch)
    repo_root = tmp_path / "repo"
    git_dir = repo_root / ".git"
    branch_name = "feature/ui-followups"
    ref_path = git_dir / "refs" / "heads" / "feature" / "ui-followups"
    log_path = git_dir / "logs" / "refs" / "heads" / "feature" / "ui-followups"
    repo_root.mkdir()
    ref_path.parent.mkdir(parents=True)
    log_path.parent.mkdir(parents=True)

    full_sha = "1234567890abcdef1234567890abcdef12345678"
    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch_name}\n", encoding="utf-8")
    ref_path.write_text(f"{full_sha}\n", encoding="utf-8")
    log_path.write_text(
        (
            "0000000000000000000000000000000000000000 "
            f"{full_sha} Test User <test@example.com> 1715558400 -0700\tcommit: test\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("PULLBOX_BUILD_DATE", raising=False)
    monkeypatch.delenv("PULLBOX_GIT_BRANCH", raising=False)
    monkeypatch.delenv("PULLBOX_GIT_SHA", raising=False)
    monkeypatch.setattr(build_metadata, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(build_metadata, "_git_output", lambda *_args: None)

    metadata = build_metadata.get_build_metadata()

    assert metadata.release_date == "2024-05-13"
    assert metadata.branch == branch_name
    assert metadata.commit == "1234567"

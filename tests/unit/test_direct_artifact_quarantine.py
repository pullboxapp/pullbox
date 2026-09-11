"""Quarantine lifecycle and safety tests for direct artifacts."""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from pullbox.core.file_safety import FileSafetyError
from pullbox.models.direct_acquisition import DirectArtifactFailureClass
from pullbox.services.direct_artifact_quarantine import (
    DirectArtifactQuarantine,
    DirectArtifactValidationError,
    DirectQuarantineWorkspace,
    _detect_comic_suffix,
    _ensure_private_directory,
    validate_direct_artifact,
)
from pullbox.utilities.executors.integrity_checker import IntegrityResult

if TYPE_CHECKING:
    from pathlib import Path


def _write_cbz(path: Path, *, entry: str = "001.jpg") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(entry, b"synthetic image fixture")


def _write_nested_pack(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Alien 005.cbz", b"issue five")
        archive.writestr("Alien 006.cbz", b"issue six")


def test_quarantine_uses_private_deterministic_attempt_paths(tmp_path: Path) -> None:
    quarantine = DirectArtifactQuarantine(tmp_path / "direct")

    workspace = quarantine.prepare(acquisition_id=12, artifact_id=34)

    assert workspace.root == (tmp_path / "direct").resolve()
    assert workspace.directory == workspace.root / "attempt-12" / "artifact-34"
    assert workspace.partial_path == workspace.directory / "payload.part"
    assert workspace.directory.stat().st_mode & 0o777 == 0o700


def test_quarantine_finalizes_from_content_not_misleading_filename(tmp_path: Path) -> None:
    quarantine = DirectArtifactQuarantine(tmp_path / "direct")
    workspace = quarantine.prepare(acquisition_id=1, artifact_id=2)
    _write_cbz(workspace.partial_path)

    final_path = quarantine.finalize(
        workspace,
        filename_hint="mislabelled-release.cbr",
    )

    assert final_path == workspace.directory / "artifact-2.cbz"
    assert final_path.is_file()
    assert not workspace.partial_path.exists()


@pytest.mark.asyncio
async def test_direct_artifact_reuses_existing_safety_and_integrity_checks(
    tmp_path: Path,
) -> None:
    quarantine = DirectArtifactQuarantine(tmp_path / "direct")
    workspace = quarantine.prepare(acquisition_id=1, artifact_id=2)
    _write_cbz(workspace.partial_path)
    final_path = quarantine.finalize(workspace, filename_hint="issue.cbz")
    session = AsyncMock()
    session.get.return_value = None

    result = await validate_direct_artifact(session, final_path)

    assert result.page_count == 1
    assert result.file_size == final_path.stat().st_size
    assert result.file_hash


@pytest.mark.asyncio
async def test_direct_artifact_accepts_a_valid_separable_issue_pack(
    tmp_path: Path,
) -> None:
    quarantine = DirectArtifactQuarantine(tmp_path / "direct")
    workspace = quarantine.prepare(acquisition_id=1, artifact_id=2)
    _write_nested_pack(workspace.partial_path)
    final_path = quarantine.finalize(workspace, filename_hint="pack.cbz")
    session = AsyncMock()
    session.get.return_value = None

    result = await validate_direct_artifact(session, final_path)

    assert result.page_count is None
    assert result.file_size == final_path.stat().st_size


@pytest.mark.asyncio
async def test_direct_artifact_rejects_archive_traversal_and_stays_quarantined(
    tmp_path: Path,
) -> None:
    quarantine = DirectArtifactQuarantine(tmp_path / "direct")
    workspace = quarantine.prepare(acquisition_id=1, artifact_id=2)
    _write_cbz(workspace.partial_path, entry="../escape.jpg")
    final_path = quarantine.finalize(workspace, filename_hint="issue.cbz")
    session = AsyncMock()
    session.get.return_value = None

    with pytest.raises(DirectArtifactValidationError) as caught:
        await validate_direct_artifact(session, final_path)

    assert caught.value.failure_class is DirectArtifactFailureClass.SAFETY
    assert caught.value.intervention is False
    assert final_path.exists()
    assert not (tmp_path / "escape.jpg").exists()


@pytest.mark.asyncio
async def test_direct_artifact_marks_resource_limit_as_overrideable_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quarantine = DirectArtifactQuarantine(tmp_path / "direct")
    workspace = quarantine.prepare(acquisition_id=1, artifact_id=2)
    _write_cbz(workspace.partial_path)
    final_path = quarantine.finalize(workspace, filename_hint="issue.cbz")
    session = AsyncMock()
    session.get.return_value = None

    def reject_oversized_archive(*_args: object, **_kwargs: object) -> None:
        raise FileSafetyError(
            "Archive decompressed size (4,248,234,210 bytes) exceeds limit (2,097,152,000 bytes)"
        )

    monkeypatch.setattr(
        "pullbox.services.direct_artifact_quarantine.run_safety_checks",
        reject_oversized_archive,
    )

    with pytest.raises(DirectArtifactValidationError) as caught:
        await validate_direct_artifact(session, final_path)

    assert caught.value.code == "artifact_resource_safety_review"
    assert caught.value.intervention is True
    assert caught.value.overrideable is True
    assert caught.value.safety_block == {
        "kind": "archive_decompressed_size",
        "reason": (
            "Archive decompressed size (4,248,234,210 bytes) exceeds limit (2,097,152,000 bytes)"
        ),
        "details": [],
        "source": "file_safety",
        "overrideable": True,
    }


def test_quarantine_rejects_unknown_payload_and_cleans_owned_workspace(
    tmp_path: Path,
) -> None:
    quarantine = DirectArtifactQuarantine(tmp_path / "direct")
    workspace = quarantine.prepare(acquisition_id=1, artifact_id=2)
    workspace.partial_path.write_bytes(b"not a comic")

    with pytest.raises(DirectArtifactValidationError) as caught:
        quarantine.finalize(workspace, filename_hint="payload.bin")

    assert caught.value.code == "artifact_file_type_unsupported"
    quarantine.cleanup(workspace)
    assert not workspace.directory.exists()


def test_quarantine_rejects_symlinked_attempt_directory(tmp_path: Path) -> None:
    root = tmp_path / "direct"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "attempt-1").symlink_to(outside, target_is_directory=True)
    quarantine = DirectArtifactQuarantine(root)

    with pytest.raises(DirectArtifactValidationError) as caught:
        quarantine.prepare(acquisition_id=1, artifact_id=2)

    assert caught.value.code == "unsafe_quarantine_destination"


@pytest.mark.parametrize(("acquisition_id", "artifact_id"), [(0, 1), (1, 0), (-1, 2)])
def test_quarantine_rejects_non_positive_identifiers(
    tmp_path: Path,
    acquisition_id: int,
    artifact_id: int,
) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        DirectArtifactQuarantine(tmp_path / "direct").prepare(
            acquisition_id=acquisition_id,
            artifact_id=artifact_id,
        )


def test_validation_error_exposes_safe_diagnostics() -> None:
    safety_block = {"overrideable": True, "reason": "too large"}
    error = DirectArtifactValidationError(
        code="too_large",
        message="Review required",
        retryable=True,
        safety_block=safety_block,
    )
    safety_block["reason"] = "mutated"

    assert error.overrideable is True
    assert error.safety_block == {"overrideable": True, "reason": "too large"}
    assert "code='too_large'" in repr(error)


def test_finalize_rejects_missing_empty_and_existing_payloads(tmp_path: Path) -> None:
    quarantine = DirectArtifactQuarantine(tmp_path / "direct")
    missing = quarantine.prepare(acquisition_id=1, artifact_id=1)
    with pytest.raises(DirectArtifactValidationError, match="safe quarantine"):
        quarantine.finalize(missing, filename_hint="issue.cbz")

    empty = quarantine.prepare(acquisition_id=2, artifact_id=2)
    empty.partial_path.touch()
    with pytest.raises(DirectArtifactValidationError, match="safe quarantine"):
        quarantine.finalize(empty, filename_hint="issue.cbz")

    occupied = quarantine.prepare(acquisition_id=3, artifact_id=3)
    _write_cbz(occupied.partial_path)
    (occupied.directory / "artifact-3.cbz").write_bytes(b"occupied")
    with pytest.raises(DirectArtifactValidationError, match="safe quarantine"):
        quarantine.finalize(occupied, filename_hint="issue.cbz")


def test_finalize_reports_atomic_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quarantine = DirectArtifactQuarantine(tmp_path / "direct")
    workspace = quarantine.prepare(acquisition_id=1, artifact_id=2)
    _write_cbz(workspace.partial_path)

    def fail_replace(*_args: object) -> None:
        raise OSError("read only")

    monkeypatch.setattr("pullbox.services.direct_artifact_quarantine.os.replace", fail_replace)

    with pytest.raises(DirectArtifactValidationError) as caught:
        quarantine.finalize(workspace, filename_hint="issue.cbz")

    assert caught.value.code == "artifact_quarantine_unwritable"


@pytest.mark.parametrize(
    ("header", "hint", "suffix"),
    [
        (b"PK\x03\x04payload", "book.epub", ".epub"),
        (b"Rar!\x1a\x07payload", "book.bin", ".cbr"),
        (b"7z\xbc\xaf\x27\x1cpayload", None, ".cb7"),
        (b"%PDF payload", None, ".pdf"),
        (b"\x00" * 257 + b"ustar", None, ".cbt"),
    ],
)
def test_detect_comic_suffix_uses_content_signatures(
    tmp_path: Path,
    header: bytes,
    hint: str | None,
    suffix: str,
) -> None:
    payload = tmp_path / "payload.part"
    payload.write_bytes(header)

    assert _detect_comic_suffix(payload, filename_hint=hint) == suffix


def test_detect_comic_suffix_reports_unreadable_payload(tmp_path: Path) -> None:
    with pytest.raises(DirectArtifactValidationError) as caught:
        _detect_comic_suffix(tmp_path / "missing.part", filename_hint=None)

    assert caught.value.code == "artifact_quarantine_unreadable"
    assert caught.value.retryable is True
    assert caught.value.intervention is False


def test_private_directory_must_remain_beneath_parent(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    outside = tmp_path / "outside"
    parent.mkdir()

    with pytest.raises(DirectArtifactValidationError, match="safe quarantine"):
        _ensure_private_directory(outside, parent=parent)


def test_cleanup_ignores_invalid_workspace(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = DirectQuarantineWorkspace(
        root=root,
        directory=outside,
        partial_path=outside / "payload.part",
        artifact_id=1,
    )

    DirectArtifactQuarantine(root).cleanup(workspace)

    assert outside.exists()


@pytest.mark.asyncio
async def test_direct_artifact_rejects_disallowed_extension(tmp_path: Path) -> None:
    path = tmp_path / "payload.exe"
    path.write_bytes(b"binary")
    session = AsyncMock()
    session.get.return_value = None

    with pytest.raises(DirectArtifactValidationError) as caught:
        await validate_direct_artifact(session, path)

    assert caught.value.code == "artifact_file_type_unsupported"


@pytest.mark.asyncio
async def test_direct_artifact_rejects_non_resource_safety_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "payload.cbz"
    _write_cbz(path)
    session = AsyncMock()
    session.get.return_value = None

    def reject_archive(*_args: object, **_kwargs: object) -> None:
        raise FileSafetyError("unsafe archive member")

    monkeypatch.setattr(
        "pullbox.services.direct_artifact_quarantine.run_safety_checks",
        reject_archive,
    )

    with pytest.raises(DirectArtifactValidationError) as caught:
        await validate_direct_artifact(session, path)

    assert caught.value.code == "artifact_safety_rejected"
    assert caught.value.intervention is False


@pytest.mark.asyncio
async def test_direct_artifact_rejects_corrupt_non_pack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "payload.cbz"
    _write_cbz(path)
    session = AsyncMock()
    session.get.return_value = None
    monkeypatch.setattr(
        "pullbox.services.direct_artifact_quarantine.check_file_integrity",
        AsyncMock(return_value=IntegrityResult(status="corrupt")),
    )
    monkeypatch.setattr(
        "pullbox.services.direct_artifact_quarantine.is_separable_issue_pack",
        lambda _path: False,
    )

    with pytest.raises(DirectArtifactValidationError) as caught:
        await validate_direct_artifact(session, path)

    assert caught.value.code == "artifact_integrity_failed"


@pytest.mark.asyncio
async def test_direct_artifact_reports_unreadable_final_stat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnreadablePath:
        suffix = ".cbz"

        def stat(self) -> object:
            raise OSError("gone")

    session = AsyncMock()
    monkeypatch.setattr(
        "pullbox.services.direct_artifact_quarantine.get_allowed_extensions",
        AsyncMock(return_value={".cbz"}),
    )
    monkeypatch.setattr(
        "pullbox.services.direct_artifact_quarantine.is_dangerous_file_blocking_enabled",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "pullbox.services.direct_artifact_quarantine.get_archive_size_limit_bytes",
        AsyncMock(return_value=1024),
    )
    monkeypatch.setattr(
        "pullbox.services.direct_artifact_quarantine.run_safety_checks",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "pullbox.services.direct_artifact_quarantine.check_file_integrity",
        AsyncMock(return_value=IntegrityResult(status="healthy", page_count=1)),
    )

    with pytest.raises(DirectArtifactValidationError) as caught:
        await validate_direct_artifact(session, UnreadablePath())  # type: ignore[arg-type]

    assert caught.value.code == "artifact_quarantine_unreadable"
    assert caught.value.retryable is True

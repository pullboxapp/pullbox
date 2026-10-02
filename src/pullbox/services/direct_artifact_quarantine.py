"""App-owned quarantine lifecycle and validation for direct artifacts."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pullbox.core.file_safety import (
    DEFAULT_ALLOWED_EXTENSIONS,
    FileSafetyError,
    classify_resource_safety_exception,
    get_allowed_extensions,
    get_archive_size_limit_bytes,
    is_dangerous_file_blocking_enabled,
    run_safety_checks,
)
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.models.direct_acquisition import DirectArtifactFailureClass
from pullbox.services.direct_artifact_pack import is_separable_issue_pack
from pullbox.utilities.executors.integrity_checker import check_file_integrity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class DirectQuarantineWorkspace:
    """Deterministic paths owned by one direct artifact attempt."""

    root: Path
    directory: Path
    partial_path: Path
    artifact_id: int


@dataclass(frozen=True, slots=True)
class DirectArtifactValidationResult:
    """Safe metadata retained after existing integrity checks pass."""

    path: Path
    file_size: int
    page_count: int | None
    file_hash: str | None


class DirectArtifactValidationError(RuntimeError):
    """Stable safety failure that does not expose source URLs or credentials."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool = False,
        intervention: bool = True,
        safety_block: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.failure_class = DirectArtifactFailureClass.SAFETY
        self.retryable = retryable
        self.intervention = intervention
        self.safety_block = dict(safety_block) if safety_block is not None else None
        self.overrideable = bool(self.safety_block and self.safety_block.get("overrideable"))

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, "
            f"retryable={self.retryable!r}, intervention={self.intervention!r})"
        )


class DirectArtifactQuarantine:
    """Create and clean bounded workspaces beneath one app-owned root."""

    def __init__(self, root: Path) -> None:
        self._configured_root = root.expanduser()

    def prepare(self, *, acquisition_id: int, artifact_id: int) -> DirectQuarantineWorkspace:
        if acquisition_id < 1 or artifact_id < 1:
            raise ValueError("Direct acquisition and artifact IDs must be positive.")
        root = _ensure_private_directory(self._configured_root, parent=None)
        attempt_dir = _ensure_private_directory(
            root / f"attempt-{acquisition_id}",
            parent=root,
        )
        artifact_dir = _ensure_private_directory(
            attempt_dir / f"artifact-{artifact_id}",
            parent=attempt_dir,
        )
        return DirectQuarantineWorkspace(
            root=root,
            directory=artifact_dir,
            partial_path=artifact_dir / "payload.part",
            artifact_id=artifact_id,
        )

    def finalize(
        self,
        workspace: DirectQuarantineWorkspace,
        *,
        filename_hint: str | None,
    ) -> Path:
        """Atomically materialize a supported comic extension from file content."""
        _validate_workspace(workspace)
        partial = workspace.partial_path
        try:
            if partial.is_symlink() or not partial.is_file() or partial.stat().st_size <= 0:
                raise _unsafe_quarantine_error()
        except OSError as exc:
            raise _unsafe_quarantine_error() from exc

        suffix = _detect_comic_suffix(partial, filename_hint=filename_hint)
        if suffix not in DEFAULT_ALLOWED_EXTENSIONS:
            raise _unsupported_type_error()
        final_path = workspace.directory / f"artifact-{workspace.artifact_id}{suffix}"
        if final_path.exists() or final_path.is_symlink():
            raise _unsafe_quarantine_error()
        try:
            os.replace(partial, final_path)
            final_path.chmod(0o600)
        except OSError as exc:
            raise DirectArtifactValidationError(
                code="artifact_quarantine_unwritable",
                message="Pullbox could not finalize the quarantined artifact.",
            ) from exc
        return final_path

    def cleanup(self, workspace: DirectQuarantineWorkspace) -> None:
        """Remove only the owned artifact workspace and empty attempt parent."""
        try:
            _validate_workspace(workspace)
            shutil.rmtree(workspace.directory)
            workspace.directory.parent.rmdir()
        except OSError:
            return
        except DirectArtifactValidationError:
            return


async def validate_direct_artifact(
    session: AsyncSession,
    path: Path,
) -> DirectArtifactValidationResult:
    """Run Pullbox's existing safety and integrity engines before handoff."""
    allowed_extensions = await get_allowed_extensions(session)
    if path.suffix.lower() not in allowed_extensions:
        raise _unsupported_type_error()
    block_dangerous = await is_dangerous_file_blocking_enabled(session)
    max_archive_size = await get_archive_size_limit_bytes(session)
    try:
        source_signature = await asyncio.to_thread(build_file_identity_signature, path)
    except OSError as exc:
        raise DirectArtifactValidationError(
            code="artifact_quarantine_unreadable",
            message="Pullbox could not read the quarantined artifact.",
            retryable=True,
            intervention=False,
        ) from exc
    try:
        await asyncio.to_thread(
            run_safety_checks,
            path,
            block_dangerous=block_dangerous,
            max_archive_size=max_archive_size,
        )
    except FileSafetyError as exc:
        safety_block = classify_resource_safety_exception(exc)
        if safety_block is not None:
            if await asyncio.to_thread(build_file_identity_signature, path) != source_signature:
                raise DirectArtifactValidationError(
                    code="artifact_changed_during_inspection",
                    message=(
                        "The downloaded artifact changed during inspection. Retry the download."
                    ),
                ) from exc
            raise DirectArtifactValidationError(
                code="artifact_resource_safety_review",
                message=(
                    "The downloaded artifact exceeds configured resource safety limits "
                    "and requires an explicit allow-once review."
                ),
                safety_block={
                    **safety_block.to_diagnostics(),
                    "source_signature": source_signature,
                },
            ) from exc
        raise DirectArtifactValidationError(
            code="artifact_safety_rejected",
            message="The downloaded artifact failed Pullbox file safety checks.",
            intervention=False,
        ) from exc

    integrity = await check_file_integrity(path, deep=False)
    is_pack = integrity.status == "corrupt" and await asyncio.to_thread(
        is_separable_issue_pack,
        path,
    )
    if integrity.status == "corrupt" and not is_pack:
        raise DirectArtifactValidationError(
            code="artifact_integrity_failed",
            message="The downloaded artifact failed Pullbox integrity checks.",
        )
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise DirectArtifactValidationError(
            code="artifact_quarantine_unreadable",
            message="Pullbox could not read the quarantined artifact.",
            retryable=True,
            intervention=False,
        ) from exc
    return DirectArtifactValidationResult(
        path=path,
        file_size=file_size,
        page_count=None if is_pack else integrity.page_count,
        file_hash=None if is_pack else integrity.file_hash,
    )


def _ensure_private_directory(path: Path, *, parent: Path | None) -> Path:
    if path.is_symlink():
        raise _unsafe_quarantine_error()
    try:
        path.mkdir(mode=0o700, parents=parent is None, exist_ok=True)
        resolved = path.resolve(strict=True)
        if parent is not None:
            resolved.relative_to(parent)
        if not resolved.is_dir() or resolved.is_symlink():
            raise _unsafe_quarantine_error()
        resolved.chmod(0o700)
        return resolved
    except DirectArtifactValidationError:
        raise
    except (OSError, ValueError) as exc:
        raise _unsafe_quarantine_error() from exc


def _validate_workspace(workspace: DirectQuarantineWorkspace) -> None:
    try:
        root = workspace.root.resolve(strict=True)
        directory = workspace.directory.resolve(strict=True)
        directory.relative_to(root)
    except (OSError, ValueError) as exc:
        raise _unsafe_quarantine_error() from exc
    if root.is_symlink() or directory.is_symlink() or not directory.is_dir():
        raise _unsafe_quarantine_error()
    if workspace.partial_path.parent != directory:
        raise _unsafe_quarantine_error()


def _detect_comic_suffix(path: Path, *, filename_hint: str | None) -> str:
    try:
        with path.open("rb") as handle:
            header = handle.read(512)
    except OSError as exc:
        raise DirectArtifactValidationError(
            code="artifact_quarantine_unreadable",
            message="Pullbox could not inspect the quarantined artifact.",
            retryable=True,
            intervention=False,
        ) from exc

    hint_suffix = Path(filename_hint or "").suffix.lower()
    if header.startswith(b"PK\x03\x04"):
        return ".epub" if hint_suffix == ".epub" else ".cbz"
    if header.startswith(b"Rar!\x1a\x07"):
        return ".cbr"
    if header.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".cb7"
    if header.startswith(b"%PDF"):
        return ".pdf"
    if len(header) >= 262 and header[257:262] == b"ustar":
        return ".cbt"
    raise _unsupported_type_error()


def _unsupported_type_error() -> DirectArtifactValidationError:
    return DirectArtifactValidationError(
        code="artifact_file_type_unsupported",
        message="The downloaded artifact is not a supported comic file.",
    )


def _unsafe_quarantine_error() -> DirectArtifactValidationError:
    return DirectArtifactValidationError(
        code="unsafe_quarantine_destination",
        message="The direct artifact path is outside its safe quarantine.",
    )

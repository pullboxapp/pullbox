"""Content-backed remount checks scoped to retained direct metadata recovery."""

from __future__ import annotations

import hmac
import re
from typing import TYPE_CHECKING

from pullbox.core.exceptions import ConfigurationError
from pullbox.core.library_file_ownership import (
    build_file_identity_signature,
    build_managed_placement_signature,
)

if TYPE_CHECKING:
    from pathlib import Path

_IDENTITY_KEYS = ("schema_version", "resolved_path", "size", "mtime_ns", "device", "inode")


def proof_identity(proof: dict[str, int | str]) -> dict[str, int | str]:
    return {key: proof[key] for key in _IDENTITY_KEYS}


def _identity(path: Path) -> dict[str, int | str]:
    if path.is_symlink():
        raise ValueError("A retained metadata file must not be a symlink.")
    try:
        return {**build_file_identity_signature(path), "ctime_ns": path.stat().st_ctime_ns}
    except ConfigurationError as exc:
        raise ValueError("The retained metadata file is no longer a regular file.") from exc


def capture_file_proof(path: Path) -> dict[str, int | str]:
    before = _identity(path)
    try:
        hashed = build_managed_placement_signature(path)
    except ConfigurationError as exc:
        raise ValueError("The retained metadata file changed while hashing.") from exc
    if _identity(path) != before or proof_identity(hashed) != proof_identity(before):
        raise ValueError("The retained metadata file changed while hashing.")
    return {**before, "sha256": hashed["content_digest"]}


def verify_file_proof(
    path: Path,
    saved: object,
    digest: object,
    verified: dict[str, int | str] | None = None,
) -> dict[str, int | str] | None:
    current = _identity(path)
    signature = proof_identity(current)
    if digest is None:
        # Legacy records cannot infer original bytes from today's file after a remount.
        if signature != saved:
            raise ValueError("No saved content proof for the changed storage identity.")
        return None
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(saved, dict)
        or type(saved.get("device")) is not int
        or {**signature, "device": saved["device"]} != saved
    ):
        raise ValueError("The retained metadata file identity changed.")
    if verified is not None:
        # Repeated writer control checks use this preview's verified stat/ctime snapshot.
        if verified == {**current, "sha256": digest}:
            return verified
        if {
            **current,
            "device": verified.get("device"),
            "ctime_ns": verified.get("ctime_ns"),
            "sha256": digest,
        } != verified:
            raise ValueError("The retained metadata file changed after preview.")
    proof = capture_file_proof(path)
    if proof != {**current, "sha256": proof["sha256"]} or not hmac.compare_digest(
        str(proof["sha256"]), digest
    ):
        raise ValueError("The retained metadata file content changed.")
    return proof

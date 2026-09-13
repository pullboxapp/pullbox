"""Verify the publisher before accepting any download coordinates."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024**3
VERSION = re.compile(r"\d{8}T\d{6}Z")
SHA256 = re.compile(r"[0-9a-f]{64}")

# Public verification keys only. Ship a new key before the publisher rotates to it.
TRUSTED_KEYS = {
    "catalog-2026-09": Ed25519PublicKey.from_public_bytes(
        base64.b64decode("SwsNUSLDGU8PvkvImuLSRopRgKMdQM2Odr2JA5bA+tg=")
    ),
}


class CatalogError(ValueError):
    """A safe, actionable catalog failure."""


@dataclass(frozen=True)
class Artifact:
    version: str
    download_path: str
    sha256: str
    size_bytes: int
    base_version: str | None = None


@dataclass(frozen=True)
class Publication:
    latest_version: str
    full_snapshot: Artifact
    snapshots: tuple[Artifact, ...]
    patches: tuple[Artifact, ...]

    def latest_patch(self) -> Artifact | None:
        return next(
            (
                patch
                for patch in self.patches
                if patch.version == self.latest_version
                and patch.base_version == self.full_snapshot.version
            ),
            None,
        )


def valid_version(value: object) -> str:
    if not isinstance(value, str) or VERSION.fullmatch(value) is None:
        raise CatalogError("Catalog version is not supported. Update Pullbox and try again.")
    try:
        datetime.strptime(value, "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise CatalogError("Catalog version is invalid.") from exc
    return value


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogError("Catalog manifest is invalid.")
    return value


def _artifact(value: object, *, patch: bool = False) -> Artifact:
    item = _object(value)
    version = valid_version(item.get("target_version" if patch else "version"))
    base = valid_version(item.get("base_version")) if patch else None
    path = (
        f"/api/v2/catalog/patches/{base}/{version}"
        if patch
        else f"/api/v2/catalog/snapshots/{version}"
    )
    checksum, size = item.get("sha256"), item.get("size_bytes")
    if (
        item.get("download_path") != path
        or not isinstance(checksum, str)
        or SHA256.fullmatch(checksum) is None
        or type(size) is not int
        or not 0 < size <= MAX_ARTIFACT_BYTES
        or (base is not None and base >= version)
    ):
        raise CatalogError("Catalog artifact information is invalid.")
    return Artifact(version, path, checksum, size, base)


def verify_manifest(
    raw: bytes,
    keys: Mapping[str, Ed25519PublicKey] = TRUSTED_KEYS,
) -> Publication:
    """Parse only bounded JSON and verify its canonical signed payload first."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise CatalogError("Catalog manifest is too large.")
    try:
        document = _object(json.loads(raw))
        payload, signature = _object(document.get("payload")), _object(document.get("signature"))
        key_id = signature.get("key_id")
        if not isinstance(key_id, str) or key_id not in keys:
            raise CatalogError("Unknown catalog signing key. Update Pullbox and try again.")
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        if (
            signature.get("algorithm") != "Ed25519"
            or signature.get("payload_sha256") != hashlib.sha256(canonical).hexdigest()
        ):
            raise CatalogError("Catalog signature verification failed.")
        encoded = signature.get("value")
        if not isinstance(encoded, str):
            raise CatalogError("Catalog signature verification failed.")
        keys[key_id].verify(
            base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True),
            canonical,
        )
        if (
            payload.get("format_id") != "pullbox-catalog-v2-publication"
            or payload.get("schema_version") != "1"
        ):
            raise CatalogError("Catalog format is not supported. Update Pullbox and try again.")
        full = _artifact(payload.get("full_snapshot"))
        snapshots_raw, patches_raw = payload.get("snapshots", []), payload.get("patches", [])
        if not isinstance(snapshots_raw, list) or not isinstance(patches_raw, list):
            raise CatalogError("Catalog manifest is invalid.")
        snapshots = tuple(_artifact(item) for item in snapshots_raw)
        patches = tuple(_artifact(item, patch=True) for item in patches_raw)
        bases = {full.version, *(entry.version for entry in snapshots)}
        coordinates = [item.download_path for item in (full, *snapshots, *patches)]
        latest = valid_version(payload.get("latest_version"))
        if (
            len(coordinates) != len(set(coordinates))
            or any(item.version >= full.version for item in snapshots)
            or any(item.base_version not in bases for item in patches)
            or any(
                item.base_version != full.version and item.version >= full.version
                for item in patches
            )
            or latest != max([full.version, *(item.version for item in patches)])
        ):
            raise CatalogError("Catalog update lineage is invalid.")
        result = Publication(latest, full, snapshots, patches)
        if latest != full.version and result.latest_patch() is None:
            raise CatalogError("Catalog latest update is unavailable.")
        return result
    except (InvalidSignature, binascii.Error) as exc:
        raise CatalogError("Catalog signature verification failed.") from exc
    except CatalogError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise CatalogError("Catalog manifest is invalid.") from exc

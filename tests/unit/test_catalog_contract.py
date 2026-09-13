"""The publication signature is the trust boundary for all download coordinates."""

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pullbox.services.catalog.contract import CatalogError, verify_manifest


def signed_publication(payload, key):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return json.dumps(
        {
            "payload": payload,
            "signature": {
                "algorithm": "Ed25519",
                "key_id": "test",
                "payload_sha256": hashlib.sha256(canonical).hexdigest(),
                "value": base64.urlsafe_b64encode(key.sign(canonical)).decode().rstrip("="),
            },
        }
    ).encode()


def publication_payload():
    return {
        "format_id": "pullbox-catalog-v2-publication",
        "schema_version": "1",
        "published_at": "2026-09-13T05:00:00+00:00",
        "latest_version": "20260913T050000Z",
        "full_snapshot": {
            "version": "20260913T050000Z",
            "size_bytes": 123,
            "sha256": "a" * 64,
            "download_path": "/api/v2/catalog/snapshots/20260913T050000Z",
        },
        "snapshots": [],
        "patches": [],
        "retention": {"snapshot_count": 6, "patch_days": 35},
    }


def test_verifies_the_signed_artifact_coordinates():
    key = Ed25519PrivateKey.generate()
    result = verify_manifest(
        signed_publication(publication_payload(), key), {"test": key.public_key()}
    )
    assert result is not None
    assert result.latest_version == "20260913T050000Z"
    assert result.full_snapshot.size_bytes == 123


def test_rejects_tampering_before_accepting_a_download():
    key = Ed25519PrivateKey.generate()
    document = json.loads(signed_publication(publication_payload(), key))
    document["payload"]["full_snapshot"]["size_bytes"] = 1
    with pytest.raises(CatalogError, match="signature"):
        verify_manifest(json.dumps(document).encode(), {"test": key.public_key()})


def test_rejects_unknown_signing_key():
    key = Ed25519PrivateKey.generate()
    with pytest.raises(CatalogError, match="signing key"):
        verify_manifest(signed_publication(publication_payload(), key), {})


@pytest.mark.parametrize("invalid_number", [float("nan"), float("inf")])
def test_non_json_numbers_report_a_safe_manifest_error(invalid_number):
    key = Ed25519PrivateKey.generate()
    payload = publication_payload()
    payload["full_snapshot"]["size_bytes"] = invalid_number
    with pytest.raises(CatalogError, match="manifest is invalid"):
        verify_manifest(signed_publication(payload, key), {"test": key.public_key()})


@pytest.mark.parametrize(
    "change",
    [
        {"download_path": "https://elsewhere.example/private"},
        {"download_path": "/api/v2/catalog/snapshots/../../secret"},
        {"size_bytes": -1},
        {"size_bytes": True},
        {"sha256": "bad"},
        {"version": "garbage"},
    ],
)
def test_rejects_invalid_signed_artifacts(change):
    key = Ed25519PrivateKey.generate()
    payload = publication_payload()
    payload["full_snapshot"].update(change)
    with pytest.raises(CatalogError):
        verify_manifest(signed_publication(payload, key), {"test": key.public_key()})

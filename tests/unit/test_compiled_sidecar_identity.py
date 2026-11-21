"""Read the writer's compiled identity without trusting copied provenance."""

import json

import pytest

from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.source_metadata import SourceMetadataExtractor
from tests.integration.test_compiled_sidecar_import import _sidecar


@pytest.mark.parametrize("namespace", [IdentityNamespace.METRON, IdentityNamespace.GCD])
def test_writer_sidecar_keeps_only_series_identity_evidence(tmp_path, namespace):
    data = json.loads(_sidecar(tmp_path, namespace))
    other = "gcd" if namespace == IdentityNamespace.METRON else "metron"
    data["pullbox"]["snapshot"]["observed_identities"] = [
        {"namespace": other, "entity_kind": "series", "external_id": "999"}
    ]
    data["pullbox"]["links"] = {other: {"id": "999", "verification_state": "verified"}}
    (tmp_path / "series.json").write_text(json.dumps(data))
    metadata = SourceMetadataExtractor().from_path(
        tmp_path / "untagged.cbz", include_archive_comicinfo=False
    )
    evidence = metadata.diagnostics["series_sidecar"]
    assert evidence["identities"] == [{"namespace": namespace.value, "kind": "series", "id": "21"}]
    assert evidence["source"] == "series.json"
    assert not evidence["problems"]
    assert "snapshot" not in evidence and "links" not in evidence
    assert metadata.comicvine_series_id is None


@pytest.mark.parametrize(
    "defect", ["version", "boolean_version", "duplicate", "kind", "path", "oversized", "malformed"]
)
def test_bad_compiled_sidecar_is_bounded_diagnostic_evidence(tmp_path, defect):
    original = _sidecar(tmp_path, IdentityNamespace.METRON)
    data = json.loads(original)
    if defect == "version":
        data["pullbox"]["schema_version"] = 2
    elif defect == "boolean_version":
        data["pullbox"]["schema_version"] = True
    elif defect == "kind":
        data["pullbox"]["snapshot"]["entity_kind"] = "issue"
    elif defect == "path":
        data["private_copy"] = "/do/not/store/this/path"
    payload = json.dumps(data).encode()
    if defect == "duplicate":
        payload = payload.replace(
            b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1', 1
        )
    elif defect == "oversized":
        payload = b" " * (1024 * 1024 + 1) + payload
    elif defect == "malformed":
        payload = payload[:-1]
    (tmp_path / "series.json").write_bytes(payload)
    metadata = SourceMetadataExtractor().from_path(
        tmp_path / "untagged.cbz", include_archive_comicinfo=False
    )
    evidence = metadata.diagnostics["series_sidecar"]
    assert evidence["problems"] == ["invalid_series_sidecar"]
    assert evidence["identities"] == []
    assert "/do/not/store" not in json.dumps(metadata.diagnostics)
    assert (tmp_path / "series.json").read_bytes() == payload

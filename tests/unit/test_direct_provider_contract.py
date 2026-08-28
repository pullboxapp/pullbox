from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from pullbox.providers.direct.contract import (
    DIRECT_PROVIDER_PROTOCOL_V1,
    DirectCandidate,
    DirectManifestResponse,
    DirectMirror,
    DirectResolveResponse,
    DirectSearchRequest,
    DirectSearchResponse,
    negotiate_direct_provider_protocol,
)


def _candidate(**overrides: object) -> DirectCandidate:
    values: dict[str, object] = {
        "provider_candidate_id": "provider:item-1",
        "source_reference": "https://source.example/item/1",
        "display_title": "Example #1",
        "raw_title": "Example 001 (2026).cbz",
        "parsed": {"series_title": "Example", "issue_numbers": ["1"]},
        "provider_confidence": 0.95,
    }
    values.update(overrides)
    return DirectCandidate.model_validate(values)


def _manifest(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "protocol_version": DIRECT_PROVIDER_PROTOCOL_V1,
        "provider_id": "pullbox.synthetic",
        "display_name": "Synthetic Provider",
        "description": "Deterministic test provider.",
        "provider_version": "1.0.0",
        "supported_protocol_versions": [DIRECT_PROVIDER_PROTOCOL_V1],
        "publisher": "Pullbox",
        "license": "GPL-3.0-or-later",
        "source_domains": ["provider.test"],
        "artifact_host_patterns": [],
        "capabilities": {
            "search": True,
            "resolve": True,
            "browser_challenge": False,
            "health": True,
            "quota": False,
            "configuration_schema": True,
        },
        "configuration_schema": {
            "type": "object",
            "properties": {
                "member_token": {
                    "type": "string",
                    "title": "Member token",
                    "x-pullbox-secret": True,
                }
            },
            "additionalProperties": False,
        },
        "build": {"revision": "test"},
    }
    payload.update(overrides)
    return payload


def test_manifest_accepts_additive_fields_and_normalizes_native_controls() -> None:
    manifest = DirectManifestResponse.model_validate(
        _manifest(future_optional_field={"supported": True})
    )

    assert manifest.provider_id == "pullbox.synthetic"
    assert manifest.configuration_controls[0].name == "member_token"
    assert manifest.configuration_controls[0].secret is True


def test_manifest_normalizes_allowlisted_uri_controls() -> None:
    manifest = DirectManifestResponse.model_validate(
        _manifest(
            configuration_schema={
                "type": "object",
                "properties": {
                    "source_url": {
                        "type": "string",
                        "title": "Official URL",
                        "format": "uri",
                        "enum": [
                            "https://annas-archive.gl",
                            "https://annas-archive.pk",
                            "https://annas-archive.gd",
                        ],
                        "default": "https://annas-archive.gd",
                    }
                },
                "additionalProperties": False,
            }
        )
    )

    control = manifest.configuration_controls[0]
    assert control.input_format == "uri"
    assert control.choices == (
        "https://annas-archive.gl",
        "https://annas-archive.pk",
        "https://annas-archive.gd",
    )


def test_manifest_normalizes_open_uri_suggestions_and_source_origin() -> None:
    manifest = DirectManifestResponse.model_validate(
        _manifest(
            configuration_schema={
                "type": "object",
                "properties": {
                    "source_url": {
                        "type": "string",
                        "format": "uri",
                        "default": "https://custom.example",
                        "x-pullbox-suggestions": [
                            "https://source-one.example",
                            "https://source-two.example",
                        ],
                        "x-pullbox-source-origin": True,
                    }
                },
                "additionalProperties": False,
            }
        )
    )

    control = manifest.configuration_controls[0]
    assert control.choices == ()
    assert control.suggestions == (
        "https://source-one.example",
        "https://source-two.example",
    )
    assert control.source_origin is True


def test_manifest_keeps_suggestions_and_source_origin_independent() -> None:
    manifest = DirectManifestResponse.model_validate(
        _manifest(
            configuration_schema={
                "type": "object",
                "properties": {
                    "suggested_url": {
                        "type": "string",
                        "format": "uri",
                        "x-pullbox-suggestions": ["https://known.example"],
                    },
                    "custom_origin": {
                        "type": "string",
                        "format": "uri",
                        "x-pullbox-source-origin": True,
                    },
                },
                "additionalProperties": False,
            }
        )
    )

    suggested, custom = manifest.configuration_controls
    assert suggested.suggestions == ("https://known.example",)
    assert suggested.source_origin is False
    assert custom.suggestions == ()
    assert custom.source_origin is True


def test_candidate_content_fingerprint_is_optional_and_validated() -> None:
    assert _candidate().content_fingerprint is None
    fingerprinted = _candidate(content_fingerprint="md5:0123456789abcdef0123456789abcdef")
    assert fingerprinted.content_fingerprint == "md5:0123456789abcdef0123456789abcdef"
    assert fingerprinted.content_fingerprint not in repr(fingerprinted)
    with pytest.raises(ValidationError):
        _candidate(content_fingerprint="md5:not-a-hash")


def test_manifest_rejects_executable_or_nested_configuration_controls() -> None:
    with pytest.raises(ValidationError, match="configuration control is unsupported"):
        DirectManifestResponse.model_validate(
            _manifest(
                configuration_schema={
                    "type": "object",
                    "properties": {"unsafe": {"type": "object", "html": "<script>"}},
                    "additionalProperties": False,
                }
            )
        )


@pytest.mark.parametrize(
    "field",
    [
        {"type": "integer", "default": True},
        {"type": "boolean", "enum": ["yes", "no"]},
        {"type": "integer", "minimum": 10, "maximum": 1},
        {"type": "string", "minLength": 10, "maxLength": 1},
        {"type": "integer", "minLength": 1},
        {"type": "string", "minimum": 1},
        {"type": "string", "format": "html"},
        {"type": "boolean", "format": "uri"},
        {"type": "string", "format": "uri", "x-pullbox-secret": True},
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://127.0.0.1"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://localhost"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://2130706433"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://0x7f000001"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://source.local"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://source.onion"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://source.internal"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://source.home.arpa"],
        },
    ],
)
def test_manifest_rejects_internally_inconsistent_configuration_controls(
    field: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        DirectManifestResponse.model_validate(
            _manifest(
                configuration_schema={
                    "type": "object",
                    "properties": {"unsafe": field},
                    "additionalProperties": False,
                }
            )
        )


@pytest.mark.parametrize(
    "default",
    [
        "http://source.example",
        "https://127.0.0.1",
        "https://2130706433",
        "https://source.local",
        "https://source.onion",
        "https://source.internal",
        "https://source.home.arpa",
    ],
)
def test_manifest_rejects_unsafe_source_origin_default(default: str) -> None:
    with pytest.raises(ValidationError):
        DirectManifestResponse.model_validate(
            _manifest(
                configuration_schema={
                    "type": "object",
                    "properties": {
                        "source_url": {
                            "type": "string",
                            "format": "uri",
                            "default": default,
                            "x-pullbox-source-origin": True,
                        }
                    },
                    "additionalProperties": False,
                }
            )
        )


def test_protocol_negotiation_requires_exact_supported_intersection() -> None:
    assert (
        negotiate_direct_provider_protocol([DIRECT_PROVIDER_PROTOCOL_V1])
        == DIRECT_PROVIDER_PROTOCOL_V1
    )
    with pytest.raises(ValueError, match="compatible"):
        negotiate_direct_provider_protocol(["direct-download-provider/v2"])


def test_search_request_requires_aware_future_deadline() -> None:
    with pytest.raises(ValidationError):
        DirectSearchRequest(
            protocol_version=DIRECT_PROVIDER_PROTOCOL_V1,
            request_id=UUID("11111111-1111-4111-8111-111111111111"),
            deadline=datetime.now() + timedelta(minutes=1),
            intent={
                "series_title": "Synthetic Adventures",
                "normalized_title": "synthetic adventures",
            },
        )


def test_search_and_resolve_responses_are_bounded() -> None:
    with pytest.raises(ValidationError):
        DirectSearchResponse.model_validate(
            {
                "protocol_version": DIRECT_PROVIDER_PROTOCOL_V1,
                "request_id": "11111111-1111-4111-8111-111111111111",
                "candidates": [{}] * 101,
                "truncated": True,
            }
        )
    with pytest.raises(ValidationError):
        DirectResolveResponse.model_validate(
            {
                "protocol_version": DIRECT_PROVIDER_PROTOCOL_V1,
                "request_id": "22222222-2222-4222-8222-222222222222",
                "artifacts": [{}] * 101,
            }
        )


def test_resolve_response_accepts_bounded_quota_without_download_history() -> None:
    response = DirectResolveResponse.model_validate(
        {
            "protocol_version": DIRECT_PROVIDER_PROTOCOL_V1,
            "request_id": "22222222-2222-4222-8222-222222222222",
            "artifacts": [],
            "quota": {
                "remaining": 22,
                "limit": 25,
                "window_seconds": 64_800,
                "recently_downloaded_md5s": ["must-not-cross-the-boundary"],
            },
        }
    )

    assert response.quota is not None
    assert response.quota.remaining == 22
    assert response.quota.limit == 25
    assert response.quota.window_seconds == 64_800
    assert "recently_downloaded" not in response.quota.model_dump()


def test_source_credentials_are_hidden_from_request_repr() -> None:
    request = DirectSearchRequest(
        protocol_version=DIRECT_PROVIDER_PROTOCOL_V1,
        request_id=UUID("11111111-1111-4111-8111-111111111111"),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        intent={
            "series_title": "Synthetic Adventures",
            "normalized_title": "synthetic adventures",
        },
        source_credentials={"member_token": "unique-secret-value"},
    )

    assert "unique-secret-value" not in repr(request)


def test_signed_artifact_locations_are_hidden_from_repr() -> None:
    mirror = DirectMirror(
        mirror_id="mirror-1",
        host_kind="generic_https",
        final_url="https://files.example/book.cbz?token=unique-signed-value",
    )

    assert "unique-signed-value" not in repr(mirror)

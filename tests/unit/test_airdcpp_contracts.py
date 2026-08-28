"""AirDC++ wire-contract parser tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pullbox.providers.airdcpp.contracts import (
    AirDcppAuthenticationInfo,
    AirDcppConnectivityInfo,
    AirDcppHub,
    AirDcppQueueBundle,
    AirDcppSearchInstance,
    AirDcppSearchResult,
    AirDcppSearchResultEvent,
    AirDcppSearchSentEvent,
    AirDcppSession,
    AirDcppSystemInfo,
)


def _system_info(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "api_version": 1,
        "api_feature_level": 10,
        "client_version": "AirDC++w 2.14.0 x86_64",
        "platform": "linux",
        "path_separator": "/",
    }
    value.update(overrides)
    return value


def _user(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "username": "pullbox",
        "permissions": [
            "search",
            "download",
            "queue_view",
            "queue_edit",
            "hubs_view",
            "settings_view",
        ],
    }
    value.update(overrides)
    return value


def test_authentication_contract_accepts_additive_fields_and_masks_token() -> None:
    auth = AirDcppAuthenticationInfo.model_validate(
        {
            "session_id": 123,
            "auth_token": "memory-only-token",
            "token_type": "Bearer",
            "system_info": {**_system_info(), "future_field": "accepted"},
            "user": {**_user(), "active_sessions": 1},
            "wizard_pending": False,
            "future_envelope_field": True,
        }
    )

    assert auth.session_id == 123
    assert auth.auth_token.get_secret_value() == "memory-only-token"
    assert "memory-only-token" not in repr(auth)
    assert auth.system_info.api_feature_level == 10


@pytest.mark.parametrize(
    "payload",
    [
        {},
        _system_info(api_version="1"),
        _system_info(api_feature_level=True),
        _system_info(path_separator=1),
    ],
)
def test_system_info_rejects_missing_or_wrong_required_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AirDcppSystemInfo.model_validate(payload)


def test_session_contract_requires_current_user() -> None:
    with pytest.raises(ValidationError):
        AirDcppSession.model_validate({"id": 123})


def test_hub_contract_keeps_sensitive_url_masked() -> None:
    hub = AirDcppHub.model_validate(
        {
            "id": 5,
            "hub_url": "adcs://private.example.test:1511",
            "connect_state": {"id": "connected", "str": "Connected"},
            "identity": {"name": "Private hub", "description": "", "supports": []},
            "share_profile": {},
            "favorite_hub": 0,
            "message_counts": {},
            "settings": {},
        }
    )

    assert hub.connected is True
    assert "private.example.test" not in repr(hub)


def test_connectivity_contract_requires_valid_ports_and_statuses() -> None:
    connectivity = AirDcppConnectivityInfo.model_validate(
        {
            "status_v4": {
                "auto_detect": True,
                "enabled": True,
                "text": "Active mode",
                "bind_address": "0.0.0.0",
                "external_ip": "203.0.113.10",
            },
            "status_v6": {
                "auto_detect": False,
                "enabled": False,
                "text": "Disabled",
                "bind_address": "::",
                "external_ip": "::",
            },
            "tcp_port": 21248,
            "tls_port": 21249,
            "udp_port": 21248,
        }
    )

    assert connectivity.status_v4.enabled is True
    assert connectivity.tcp_port == 21248
    assert "203.0.113.10" not in repr(connectivity)

    with pytest.raises(ValidationError):
        AirDcppConnectivityInfo.model_validate(
            {
                **connectivity.model_dump(),
                "tcp_port": 70000,
            }
        )


def test_connectivity_contract_normalizes_airdcpp_string_ports() -> None:
    connectivity = AirDcppConnectivityInfo.model_validate(
        {
            "status_v4": {
                "auto_detect": False,
                "enabled": True,
                "text": "Active mode",
                "bind_address": "0.0.0.0",
                "external_ip": "203.0.113.10",
            },
            "status_v6": {
                "auto_detect": False,
                "enabled": False,
                "text": "Disabled",
                "bind_address": "::",
                "external_ip": "::",
            },
            "tcp_port": "21248",
            "tls_port": "21249",
            "udp_port": "21248",
        }
    )

    assert connectivity.tcp_port == 21248
    assert connectivity.tls_port == 21249
    assert connectivity.udp_port == 21248

    with pytest.raises(ValidationError):
        AirDcppConnectivityInfo.model_validate(
            {
                **connectivity.model_dump(),
                "tcp_port": "70000",
            }
        )


def test_queue_bundle_contract_requires_authoritative_progress_fields() -> None:
    bundle = AirDcppQueueBundle.model_validate(
        {
            "id": 83425443,
            "name": "Example Comic 001.cbz",
            "target": "/Downloads/Example Comic 001.cbz",
            "type": {"id": "file", "str": "cbz", "content_type": {"id": "other"}},
            "size": 1000,
            "downloaded_bytes": 250,
            "priority": {"id": 4, "str": "Normal", "auto": False},
            "time_added": 1,
            "time_finished": 0,
            "speed": 10,
            "seconds_left": 75,
            "sources": {"online": 1, "total": 2, "str": "1/2 online"},
            "status": {
                "id": "queued",
                "failed": False,
                "downloaded": False,
                "completed": False,
                "str": "Running (25%)",
            },
            "future_field": "accepted",
        }
    )

    assert bundle.id == 83425443
    assert bundle.status.completed is False

    with pytest.raises(ValidationError):
        AirDcppQueueBundle.model_validate(
            {
                "id": 1,
                "name": "Incomplete",
            }
        )


def test_queue_bundle_contract_normalizes_live_whole_number_floats() -> None:
    payload: dict[str, object] = {
        "id": 83425443,
        "name": "Example Comic 001.cbz",
        "target": "/Downloads/Example Comic 001.cbz",
        "type": {"id": "file"},
        "size": 1000.0,
        "downloaded_bytes": 250.0,
        "priority": {"id": 4, "str": "Normal", "auto": False},
        "time_added": 1.0,
        "time_finished": 0.0,
        "speed": 10.0,
        "seconds_left": 75.0,
        "sources": {"online": 1, "total": 2, "str": "1/2 online"},
        "status": {
            "id": "queued",
            "failed": False,
            "downloaded": False,
            "completed": False,
            "str": "Running (25%)",
        },
    }

    bundle = AirDcppQueueBundle.model_validate(payload)

    assert bundle.size == 1000
    assert bundle.downloaded_bytes == 250
    assert bundle.time_added == 1
    assert bundle.time_finished == 0
    assert bundle.speed == 10
    assert bundle.seconds_left == 75
    with pytest.raises(ValidationError):
        AirDcppQueueBundle.model_validate({**payload, "speed": 10.5})


def test_search_instance_and_grouped_file_result_contracts_are_strict_and_additive() -> None:
    instance = AirDcppSearchInstance.model_validate(
        {
            "id": 44,
            "expires_in": 60_000,
            "current_search_id": 0,
            "owner": "session:123:pullbox",
            "queue_time": 0,
            "queued_count": 0,
            "result_count": 1,
            "searches_sent_ago": 0,
            "future_field": True,
        }
    )
    result = AirDcppSearchResult.model_validate(
        {
            "id": "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
            "name": "Example Comic 001 (2026).cbz",
            "relevance": 1.34,
            "hits": 4,
            "users": {"count": 4, "user": {"cid": "must-not-be-retained"}},
            "type": {"id": "file", "str": "File"},
            "path": "/private/peer/path/Example Comic 001.cbz",
            "tth": "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
            "dupe": None,
            "time": 1,
            "slots": {"free": 2, "total": 11, "str": "2/11"},
            "connection": 42_500_000,
            "size": 62_523_525_626,
            "future_field": "accepted",
        }
    )

    assert instance.id == 44
    assert result.file_result is True
    assert result.slots.free == 2
    assert result.users.count == 4
    assert "private/peer/path" not in repr(result)
    assert "must-not-be-retained" not in repr(result)


def test_search_instance_accepts_live_string_search_ids_and_empty_sentinel() -> None:
    payload = {
        "id": 44,
        "expires_in": 60_000,
        "current_search_id": "",
        "owner": "session:123:pullbox",
        "queue_time": 0,
        "queued_count": 0,
        "result_count": 0,
        "searches_sent_ago": 0,
    }

    instance = AirDcppSearchInstance.model_validate(payload)
    active = AirDcppSearchInstance.model_validate(
        {**payload, "current_search_id": "active-search-id"}
    )

    assert instance.current_search_id == 0
    assert active.current_search_id == "active-search-id"
    with pytest.raises(ValidationError):
        AirDcppSearchInstance.model_validate({**payload, "current_search_id": "x" * 1001})


def test_search_events_accept_live_string_ids_and_whole_number_floats() -> None:
    result_payload: dict[str, object] = {
        "id": "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
        "name": "Example Comic 001.cbz",
        "relevance": 1.0,
        "hits": 4.0,
        "users": {"count": 4},
        "type": {"id": "file"},
        "path": "/private/peer/path",
        "tth": "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
        "time": 1.0,
        "slots": {"free": 2, "total": 11, "str": "2/11"},
        "connection": 42_500_000.0,
        "size": 62_523_525_626.0,
    }

    sent = AirDcppSearchSentEvent.model_validate(
        {"sent": 3, "search_id": "active-search-id", "query": {}}
    )
    result = AirDcppSearchResultEvent.model_validate(
        {"result": result_payload, "search_id": "active-search-id"}
    )

    assert sent.sent == 3
    assert result.result.hits == 4
    assert result.result.time == 1
    assert result.result.connection == 42_500_000
    assert result.result.size == 62_523_525_626


@pytest.mark.parametrize("invalid", [1.5, float("inf"), float("nan"), True])
def test_search_result_rejects_non_integral_or_non_finite_counters(invalid: object) -> None:
    payload: dict[str, object] = {
        "id": "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
        "name": "Example Comic 001.cbz",
        "relevance": 1.0,
        "hits": invalid,
        "users": {"count": 1},
        "type": {"id": "file"},
        "path": "/private/peer/path",
        "tth": "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
        "time": 0.0,
        "slots": {"free": 1, "total": 1, "str": "1/1"},
        "connection": 1.0,
        "size": 1.0,
    }

    with pytest.raises(ValidationError):
        AirDcppSearchResult.model_validate(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tth": "not-a-tth"},
        {"size": -1},
        {"slots": {"free": 12, "total": 11, "str": "12/11"}},
        {"users": {"count": -1}},
        {"type": {"id": 1}},
    ],
)
def test_grouped_search_result_rejects_incompatible_fields(overrides: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "id": "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
        "name": "Example Comic 001.cbz",
        "relevance": 1.0,
        "hits": 1,
        "users": {"count": 1},
        "type": {"id": "file"},
        "path": "/private/peer/path",
        "tth": "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
        "time": 0,
        "slots": {"free": 1, "total": 1, "str": "1/1"},
        "connection": 1,
        "size": 1,
    }
    payload.update(overrides)
    with pytest.raises(ValidationError):
        AirDcppSearchResult.model_validate(payload)

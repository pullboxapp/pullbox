"""Additive public-feed context must not make old or malformed releases unusable."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from pullbox.schemas.whats_new import (
    WhatsNewCacheMetadata,
    WhatsNewCurrentWeekResponse,
    WhatsNewIssueSummary,
    WhatsNewUpcomingResponse,
)
from pullbox.services.whats_new_data_client import WhatsNewDataClient

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_PROVIDERS = ("gcd", "metron", "comicvine")


def _payload(window: str) -> dict[str, Any]:
    name = "pullbox_data_current_week.json" if window == "current" else "pullbox_data_upcoming.json"
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _issues(payload: dict[str, Any], window: str) -> list[dict[str, Any]]:
    return payload["issues"] if window == "current" else payload["weeks"][0]["issues"]


def _parse(payload: dict[str, Any], window: str) -> WhatsNewIssueSummary:
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    cache = WhatsNewCacheMetadata(
        status="fresh", fetched_at=now, last_successful_refresh_at=now, stale=False
    )
    if window == "current":
        return WhatsNewCurrentWeekResponse(**payload, cache=cache).issues[0]
    return WhatsNewUpcomingResponse(**payload, cache=cache).weeks[0].issues[0]


@pytest.mark.parametrize("window", ["current", "upcoming"])
def test_old_feed_retains_rows_with_unknown_optional_context(window: str) -> None:
    payload = _payload(window)
    row = _parse(payload, window).model_dump(mode="json")

    assert row["locg_issue_id"] == _issues(payload, window)[0]["locg_issue_id"]
    for provider in _PROVIDERS:
        assert row.get(f"{provider}_issue_id", "absent") is None
        assert row["series"].get(f"{provider}_series_id", "absent") is None
    assert row["series"].get("publication_state") == "unknown"
    assert row["series"].get("publication_as_of", "absent") is None


@pytest.mark.parametrize("window", ["current", "upcoming"])
@pytest.mark.parametrize("state", ["published", "prepublication", "unknown"])
def test_new_feed_preserves_exact_context_and_json_dates(window: str, state: str) -> None:
    payload = _payload(window)
    issue = _issues(payload, window)[0]
    for number, provider in enumerate(_PROVIDERS, start=1):
        issue[f"{provider}_issue_id"] = number * 100
        issue["series"][f"{provider}_series_id"] = number * 10
    issue["series"].update(publication_state=state, publication_as_of="2026-10-03")

    parsed = _parse(payload, window)
    row = parsed.model_dump(mode="json")

    for number, provider in enumerate(_PROVIDERS, start=1):
        assert row.get(f"{provider}_issue_id") == number * 100
        assert row["series"].get(f"{provider}_series_id") == number * 10
    assert row["series"].get("publication_state") == state
    assert row["series"].get("publication_as_of") == "2026-10-03"
    assert getattr(parsed.series, "publication_as_of", None) == date(2026, 10, 3)


@pytest.mark.parametrize("scope", ["issue", "series"])
@pytest.mark.parametrize("provider", _PROVIDERS)
@pytest.mark.parametrize("invalid", [True, 0, -1, 1.5, "123", "secret-not-an-id", 2**63, {}])
def test_invalid_optional_ids_are_ignored_not_coerced(
    scope: str, provider: str, invalid: object
) -> None:
    payload = _payload("current")
    issue = payload["issues"][0]
    target = issue if scope == "issue" else issue["series"]
    field = f"{provider}_{scope}_id"
    target[field] = invalid

    with capture_logs() as logs:
        row = _parse(payload, "current").model_dump(mode="json")

    parsed_target = row if scope == "issue" else row["series"]
    assert parsed_target.get(field, "absent") is None
    assert any(log.get("field") == field for log in logs)
    assert "secret-not-an-id" not in json.dumps(logs)
    assert row["locg_issue_id"] == issue["locg_issue_id"]


@pytest.mark.parametrize("invalid", [None, "future-state", 1, True, {}])
def test_unknown_publication_enum_is_safe_and_diagnostic(invalid: object) -> None:
    payload = _payload("current")
    payload["issues"][0]["series"].update(publication_state=invalid, publication_as_of="2026-10-03")
    with capture_logs() as logs:
        series = _parse(payload, "current").model_dump(mode="json")["series"]
    assert series.get("publication_state") == "unknown"
    if invalid is not None:
        assert any(log.get("field") == "publication_state" for log in logs)


@pytest.mark.parametrize("invalid", [None, "2026-02-30", "20261003", "2026-10-03T00:00:00Z", True])
def test_missing_or_invalid_publication_date_cannot_certify_publication(invalid: object) -> None:
    payload = _payload("current")
    payload["issues"][0]["series"].update(publication_state="published", publication_as_of=invalid)
    with capture_logs() as logs:
        series = _parse(payload, "current").model_dump(mode="json")["series"]
    assert series.get("publication_state") == "unknown"
    assert series.get("publication_as_of", "absent") is None
    assert logs


@pytest.mark.parametrize("window", ["current", "upcoming"])
async def test_optional_context_uses_only_existing_public_feed_call(window: str) -> None:
    payload = _payload(window)
    issue = _issues(payload, window)[0]
    issue["comicvine_issue_id"] = 999
    issue["series"].update(
        comicvine_series_id=777, publication_state="published", publication_as_of="2026-10-03"
    )
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    client = WhatsNewDataClient(
        base_url="https://feed.example.test", transport=httpx.MockTransport(handle)
    )
    fetched = (
        await client.get_current_week() if window == "current" else await client.get_upcoming()
    )

    assert fetched == payload
    assert _parse(fetched, window).model_dump()["comicvine_issue_id"] == 999
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/releases"
    assert "authorization" not in requests[0].headers


def test_variant_without_exact_mapping_does_not_inherit_canonical_issue_id() -> None:
    payload = _payload("current")
    first, second = payload["issues"][:2]
    first["comicvine_issue_id"] = 999
    second["comicvine_issue_id"] = None
    second["series"] = dict(first["series"], comicvine_series_id=777)
    first_parsed = WhatsNewIssueSummary(**first).model_dump()
    second_parsed = WhatsNewIssueSummary(**second).model_dump()

    assert first_parsed.get("comicvine_issue_id") == 999
    assert second_parsed.get("comicvine_issue_id", "absent") is None
    assert first_parsed["locg_issue_id"] != second_parsed["locg_issue_id"]

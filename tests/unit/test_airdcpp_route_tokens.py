"""Opaque, user-bound Direct Connect route grant contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models.issue import IssueType
from pullbox.providers.base import ReleaseResult
from pullbox.services.airdcpp_route_tokens import AirDcppRouteTokenStore, can_override_dc_rejection
from pullbox.services.airdcpp_search_types import DcMetrics, DcRoute, DcValidatedCandidate
from pullbox.services.release_validator import ReleaseValidator

_TTH = "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y"


def _candidate() -> DcValidatedCandidate:
    release = ReleaseResult(
        title="Example Comic 001 (2026).cbz",
        indexer_name="Dedicated Air",
        download_url=f"airdcpp://client/7/tth/{_TTH}",
        size_bytes=100_000_000,
        age_days=None,
        seeders=None,
        leechers=None,
        grabs=None,
        is_torrent=False,
        category=None,
        published_at=None,
        protocol=AcquisitionProtocol.DC,
    )
    validation = ReleaseValidator().validate_all_results(
        [release],
        wanted_series="Example Comic",
        wanted_issue=1,
        wanted_year=2026,
        wanted_issue_type=IssueType.ISSUE,
    )[0][0]
    return DcValidatedCandidate(
        release=release,
        validation=validation,
        route=DcRoute(
            client_config_id=7,
            client_identity="airdcpp:7",
            search_instance_id=44,
            grouped_result_id="opaque-result",
            result_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            tth=_TTH,
            size_bytes=100_000_000,
        ),
        metrics=DcMetrics(2, 1, 2, 1_000_000),
    )


def test_route_token_is_opaque_user_bound_and_replay_stable() -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    store = AirDcppRouteTokenStore(now=lambda: now)

    token = store.issue(_candidate(), issue_id=3, user_id=5, search_log_id=9)

    assert _TTH not in token
    assert "airdcpp" not in token
    first = store.resolve(token, issue_id=3, user_id=5)
    second = store.resolve(token, issue_id=3, user_id=5)
    assert second.request_key == first.request_key
    assert second.candidate.route.client_config_id == 7
    assert second.search_log_id == 9
    with pytest.raises(ValueError, match="unavailable"):
        store.resolve(token, issue_id=4, user_id=5)
    with pytest.raises(ValueError, match="unavailable"):
        store.resolve(token, issue_id=3, user_id=6)


def test_route_token_expires_and_store_evicts_to_its_bound() -> None:
    current = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    store = AirDcppRouteTokenStore(max_entries=2, ttl_seconds=30, now=lambda: current)
    first = store.issue(_candidate(), issue_id=1, user_id=1, search_log_id=None)
    store.issue(_candidate(), issue_id=1, user_id=1, search_log_id=None)
    store.issue(_candidate(), issue_id=1, user_id=1, search_log_id=None)

    assert store.entry_count == 2
    with pytest.raises(ValueError, match="unavailable"):
        store.resolve(first, issue_id=1, user_id=1)

    current += timedelta(seconds=31)
    assert store.entry_count == 0


@pytest.mark.parametrize(
    "title,wanted_type,allowed",
    [
        ("Different Series 001 (2026).cbz", IssueType.ISSUE, True),
        ("Example Comic 002 (2026).cbz", IssueType.ISSUE, True),
        ("Example Comic 011.cbz", IssueType.TPB, True),
        ("Example Comic 001-006 (2026).cbz", IssueType.ISSUE, False),
        ("Example Comic 001 (B Story).cbz", IssueType.ISSUE, False),
        ("Example Comic 001 covers only.cbz", IssueType.ISSUE, False),
        ("Example Comic 001 (Audiobook).cbz", IssueType.ISSUE, False),
        ("Different Series 001.exe", IssueType.ISSUE, False),
    ],
)
def test_dc_override_classifies_real_validator_rejections(title, wanted_type, allowed):
    candidate = _candidate()
    release = replace(candidate.release, title=title)
    matched, rejected = ReleaseValidator().validate_all_results(
        [release],
        wanted_series="Example Comic",
        wanted_issue=1,
        wanted_year=2026,
        wanted_issue_type=wanted_type,
        wanted_series_issue_count=1,
    )
    candidate = replace(candidate, release=release, validation=(matched + rejected)[0])
    assert can_override_dc_rejection(candidate) is allowed


def test_dc_override_unknown_reason_and_identity_rejections_fail_closed():
    candidate = _candidate()
    for code in (None, "future_reason", "comicvine_issue_id_mismatch", "partial_issue"):
        rejected = replace(
            candidate,
            validation=replace(
                candidate.validation,
                is_match=False,
                rejection_code=code,
                rejection_reason="Series mismatch: misleading human-readable text",
            ),
        )
        assert not can_override_dc_rejection(rejected)

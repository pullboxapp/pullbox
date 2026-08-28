"""Automatic AirDC++ evaluation stays opt-in, bounded, and mutation-free."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models.issue import IssueType
from pullbox.providers.base import ReleaseResult
from pullbox.services.airdcpp_automatic_search import attach_automatic_airdcpp_search
from pullbox.services.airdcpp_search_types import (
    DcMetrics,
    DcRoute,
    DcSearchOutcome,
    DcValidatedCandidate,
)
from pullbox.services.release_validator import ReleaseValidator
from pullbox.services.search_acquisition_router import route_search_acquisition
from pullbox.services.search_targets import IssueSearchOutcome, IssueSearchTarget


def _outcome() -> IssueSearchOutcome:
    target = IssueSearchTarget(
        issue_id=1,
        series_id=2,
        series_title="Example Comic",
        issue_number=1,
        issue_type=IssueType.ISSUE,
        series_year=2026,
    )
    return IssueSearchOutcome(
        target=target,
        mode="fast",
        query_count=1,
        raw_results=[],
        filtered_results=[],
        matched=[],
        rejected=[],
        best_release=None,
        best_validation=None,
        search_details={},
        elapsed_ms=1,
    )


@pytest.mark.asyncio
async def test_automatic_search_attaches_low_priority_evaluation_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dc_outcome = DcSearchOutcome((), (), (), 0, 0, 0, 3, False)
    coordinator = AsyncMock()
    coordinator.search.return_value = dc_outcome
    session = AsyncMock()
    operation = SimpleNamespace(config_id=7)
    monkeypatch.setattr(
        "pullbox.services.airdcpp_automatic_search.get_airdcpp_supervisor_registry",
        lambda: object(),
    )
    monkeypatch.setattr(
        "pullbox.services.airdcpp_automatic_search.get_airdcpp_search_coordinator",
        lambda: coordinator,
    )
    load_clients = AsyncMock(return_value=(operation,))
    monkeypatch.setattr(
        "pullbox.services.airdcpp_automatic_search.load_airdcpp_search_clients",
        load_clients,
    )

    attached = await attach_automatic_airdcpp_search(
        session,
        _outcome(),
        validator_kwargs={"min_series_similarity": 0.8},
    )

    session.commit.assert_awaited_once()
    load_clients.assert_awaited_once_with(session, ANY, automatic=True)
    coordinator.search.assert_awaited_once_with(
        (operation,),
        attached.target,
        manual=False,
        validator_kwargs={"min_series_similarity": 0.8},
    )
    assert attached.dc_outcome is dc_outcome
    assert attached.search_details["dc_results_count"] == 0
    assert attached.search_details["dc_elapsed_ms"] == 3


@pytest.mark.asyncio
async def test_automatic_search_is_noop_without_runtime_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _outcome()
    session = AsyncMock()
    monkeypatch.setattr(
        "pullbox.services.airdcpp_automatic_search.get_airdcpp_supervisor_registry",
        lambda: None,
    )

    attached = await attach_automatic_airdcpp_search(session, original)

    assert attached is original
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_automatic_dc_winner_is_evaluation_only_before_r6_queue_gate() -> None:
    release = ReleaseResult(
        title="Example Comic 001 (2026).cbz",
        indexer_name="Dedicated Air",
        download_url="airdcpp://client/7/opaque",
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
    target = _outcome().target
    validation = ReleaseValidator().validate_all_results(
        [release],
        wanted_series=target.series_title,
        wanted_issue=target.issue_number,
        wanted_year=target.series_year,
    )[0][0]
    candidate = DcValidatedCandidate(
        release=release,
        validation=validation,
        route=DcRoute(
            client_config_id=7,
            client_identity="airdcpp:7",
            search_instance_id=44,
            grouped_result_id="opaque-result",
            result_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            tth="CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
            size_bytes=100_000_000,
        ),
        metrics=DcMetrics(2, 1, 2, 1_000_000),
    )
    outcome = replace(
        _outcome(),
        dc_outcome=DcSearchOutcome((candidate,), (), (), 1, 1, 0, 1, False),
    )
    download = AsyncMock()
    intervention = AsyncMock()

    routed = await route_search_acquisition(
        AsyncMock(),
        outcome=outcome,
        search_log_id=1,
        eval_kwargs={},
        type_thresholds={"issue": "high"},
        download_service=download,
        intervention_service=intervention,
        runner=None,
        source_priority=["dc", "usenet", "torrent", "direct"],
    )

    assert routed.source_kind == "dc"
    assert routed.action_status == "dc_evaluation_only"
    assert routed.grabbed == routed.queued == 0
    download.send_to_client.assert_not_awaited()
    intervention.create_pending_match.assert_not_awaited()

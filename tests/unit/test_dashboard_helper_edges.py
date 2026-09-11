"""Boundary coverage for dashboard scoring and user-facing labels."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import DatabaseError

from pullbox.services import dashboard_helpers as helpers
from pullbox.services.dashboard_intelligence_service import (
    DashboardIntelligenceService,
    _download_client_label,
    _hour_bucket_start,
)


def _storage(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "state": "healthy",
        "runway_to_degraded_days": 90.0,
        "daily_growth_bytes": 10.0,
        "previous_daily_growth_bytes": 8.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("current", "previous", "expected"),
    [(None, 90.0, 0), (90.0, None, 0), (90.0, 85.0, 0), (60.0, 90.0, 90)],
)
def test_rate_drop_score_handles_missing_rising_and_falling_rates(
    current: float | None,
    previous: float | None,
    expected: int,
) -> None:
    assert helpers.rate_drop_score(current, previous) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "info"), (95.0, "healthy"), (80.0, "watch"), (50.0, "critical")],
)
def test_rate_state_boundaries(value: float | None, expected: str) -> None:
    assert helpers.rate_state_from_percent(value) == expected


def test_dashboard_interpretations_cover_each_actionable_state() -> None:
    assert "cleanly" in helpers.flow_through_interpretation(95.0)
    assert "mostly" in helpers.flow_through_interpretation(80.0)
    assert "stalling" in helpers.flow_through_interpretation(50.0)

    debt = SimpleNamespace(total=2, oldest_at=None)
    assert "baseline" in helpers.review_debt_interpretation(debt)

    risk = SimpleNamespace(next_72h_count=1, nearest_release_date=None)
    assert "quick check" in helpers.release_risk_interpretation(risk)

    reliability = SimpleNamespace(rate=95.0, worst_client_label=None)
    assert "enough history" in helpers.client_reliability_interpretation(reliability)
    reliability.worst_client_label = "SABnzbd"
    assert "nothing looks alarming" in helpers.client_reliability_interpretation(reliability)


@pytest.mark.parametrize(
    ("storage", "expected"),
    [
        (_storage(state="unhealthy"), "Storage is already past the safe line."),
        (_storage(runway_to_degraded_days=None), "Waiting on enough snapshots to project runway."),
        (_storage(runway_to_degraded_days=90.0), "You still have room"),
        (_storage(runway_to_degraded_days=30.0), "runway is shrinking"),
        (_storage(runway_to_degraded_days=7.0), "close enough"),
    ],
)
def test_storage_interpretation_tracks_runway(storage: SimpleNamespace, expected: str) -> None:
    assert expected in helpers.storage_interpretation(storage)


def test_storage_growth_projection_and_acceleration_boundaries() -> None:
    snapshots = [
        SimpleNamespace(snapshot_date=date(2026, 1, 1), used_bytes=100),
        SimpleNamespace(snapshot_date=date(2026, 1, 3), used_bytes=180),
        SimpleNamespace(snapshot_date=date(2026, 1, 5), used_bytes=260),
        SimpleNamespace(snapshot_date=date(2026, 1, 7), used_bytes=400),
    ]
    assert helpers.storage_growth_rates(snapshots) == (50.0, 40.0)
    assert (
        helpers.project_days_remaining(threshold_bytes=100, used_bytes=100, daily_growth_bytes=10.0)
        == 0.0
    )
    assert helpers.storage_is_accelerating(
        _storage(daily_growth_bytes=1.0, previous_daily_growth_bytes=0.0)
    )
    assert helpers.storage_is_accelerating(
        _storage(daily_growth_bytes=11.0, previous_daily_growth_bytes=8.0)
    )
    assert helpers.storage_trend_score(_storage(daily_growth_bytes=None)) == 10
    assert (
        helpers.storage_trend_score(
            _storage(daily_growth_bytes=4.0, previous_daily_growth_bytes=None)
        )
        == 30
    )
    assert (
        helpers.storage_trend_score(
            _storage(daily_growth_bytes=16.0, previous_daily_growth_bytes=8.0)
        )
        == 100
    )


@pytest.mark.parametrize(
    ("storage", "severity", "imminence"),
    [
        (_storage(state="unhealthy"), 100, 20),
        (_storage(state="degraded"), 80, 20),
        (_storage(runway_to_degraded_days=None), 0, 20),
        (_storage(runway_to_degraded_days=5.0), 85, 95),
        (_storage(runway_to_degraded_days=15.0), 65, 70),
        (_storage(runway_to_degraded_days=30.0), 45, 45),
        (_storage(runway_to_degraded_days=60.0), 0, 20),
    ],
)
def test_storage_priority_boundaries(
    storage: SimpleNamespace,
    severity: int,
    imminence: int,
) -> None:
    assert helpers.storage_severity(storage) == severity
    assert helpers.storage_imminence(storage) == imminence


def test_dashboard_date_labels_handle_missing_and_boundary_values() -> None:
    now = datetime(2026, 1, 10, tzinfo=UTC)
    assert helpers.age_in_days(None, now) == 0
    assert helpers.oldest_age_label(None, now) == "Fresh queue"
    assert helpers.oldest_age_label(now - timedelta(days=1), now) == "Oldest item is 1 day old"
    assert helpers.days_until(None, date(2026, 1, 10)) == 999
    assert helpers.release_time_label(None, date(2026, 1, 10)) == "Soon"
    assert helpers.release_time_label(date(2026, 1, 13), date(2026, 1, 10)) == "Due in 3 days"


def test_storage_labels_and_search_drift_cover_sparse_baselines() -> None:
    assert helpers.storage_runway_label(_storage(state="unhealthy")) == (
        "Past the unhealthy threshold"
    )
    assert helpers.storage_runway_label(_storage(runway_to_degraded_days=0.0)) == (
        "At the degraded threshold"
    )
    assert helpers.trend_label(5, 5.0, noun="item") == "Flat vs last week"
    assert not helpers.search_yield_is_drifting(
        SimpleNamespace(rate=50.0, previous_rate=80.0, searches=2, previous_searches=10)
    )


@pytest.mark.asyncio
async def test_dashboard_metric_facades_forward_to_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    service = DashboardIntelligenceService(AsyncMock())
    loader = MagicMock()
    async_methods = [
        "load_snapshot",
        "load_reference_metrics",
        "latest_rollup_timestamp",
        "has_terminal_download_history",
        "load_download_summary",
        "load_client_reliability",
        "load_client_reliability_window",
        "load_review_debt",
        "load_release_risk",
        "load_search_yield",
        "load_import_failures",
        "load_health_summary",
        "load_storage_summary",
        "load_failure_clusters",
        "load_unmatched_clusters",
        "ensure_daily_storage_snapshot",
        "should_refresh_rollups",
        "persist_rollups",
    ]
    for name in async_methods:
        setattr(loader, name, AsyncMock(return_value=name))
    monkeypatch.setattr(service, "_metric_loader", lambda: loader)
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    before = now - timedelta(days=1)
    snapshot = object()
    storage = object()

    assert await service._load_snapshot(now) == "load_snapshot"
    assert await service._load_reference_metrics(now) == "load_reference_metrics"
    assert await service._latest_rollup_timestamp() == "latest_rollup_timestamp"
    assert await service._has_terminal_download_history() == "has_terminal_download_history"
    assert await service._load_download_summary(before, before, now) == "load_download_summary"
    assert await service._load_client_reliability(before, before, now) == "load_client_reliability"
    assert (
        await service._load_client_reliability_window(before, now)
        == "load_client_reliability_window"
    )
    assert await service._load_review_debt(None) == "load_review_debt"
    assert await service._load_release_risk(now.date(), None) == "load_release_risk"
    assert await service._load_search_yield(before, before, now) == "load_search_yield"
    assert await service._load_import_failures(before, before, now) == "load_import_failures"
    assert await service._load_health_summary(None) == "load_health_summary"
    assert await service._load_storage_summary(now) == "load_storage_summary"
    assert await service._load_failure_clusters(before, now) == "load_failure_clusters"
    assert await service._load_unmatched_clusters() == "load_unmatched_clusters"
    await service._ensure_daily_storage_snapshot(storage, now)  # type: ignore[arg-type]
    assert await service._should_refresh_rollups(snapshot, now) == "should_refresh_rollups"  # type: ignore[arg-type]
    await service._persist_rollups(snapshot, now)  # type: ignore[arg-type]


def test_dashboard_presentation_and_priority_facades_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = DashboardIntelligenceService(AsyncMock())
    snapshot = object()
    priorities = [object()]
    presentation = MagicMock()
    priority = MagicMock()
    for name in (
        "build_briefing",
        "build_scorecards",
        "build_watch_items",
        "build_exceptions",
        "build_live_pulse",
    ):
        getattr(presentation, name).return_value = name
    for name in (
        "build_priorities",
        "build_health_priority",
        "build_storage_priority",
        "build_client_failure_priority",
        "build_review_debt_priority",
        "build_release_risk_priority",
        "build_search_yield_priority",
        "build_import_failure_priority",
        "build_unmatched_growth_priority",
    ):
        getattr(priority, name).return_value = name
    monkeypatch.setattr(service, "_presentation_builder", lambda: presentation)
    monkeypatch.setattr(service, "_priority_builder", lambda: priority)

    assert service._build_briefing(snapshot, priorities) == "build_briefing"  # type: ignore[arg-type]
    assert service._build_scorecards(snapshot) == "build_scorecards"  # type: ignore[arg-type]
    assert service._build_watch_items(snapshot) == "build_watch_items"  # type: ignore[arg-type]
    assert service._build_exceptions(snapshot) == "build_exceptions"  # type: ignore[arg-type]
    assert service._build_live_pulse(snapshot) == "build_live_pulse"  # type: ignore[arg-type]
    assert service._build_priorities(snapshot) == "build_priorities"  # type: ignore[arg-type]
    assert service._build_health_priority(snapshot) == "build_health_priority"  # type: ignore[arg-type]
    assert service._build_storage_priority(snapshot) == "build_storage_priority"  # type: ignore[arg-type]
    assert (
        service._build_client_failure_priority(snapshot) == "build_client_failure_priority"  # type: ignore[arg-type]
    )
    assert service._build_review_debt_priority(snapshot) == "build_review_debt_priority"  # type: ignore[arg-type]
    assert service._build_release_risk_priority(snapshot) == "build_release_risk_priority"  # type: ignore[arg-type]
    assert service._build_search_yield_priority(snapshot) == "build_search_yield_priority"  # type: ignore[arg-type]
    assert service._build_import_failure_priority(snapshot) == "build_import_failure_priority"  # type: ignore[arg-type]
    assert (
        service._build_unmatched_growth_priority(snapshot) == "build_unmatched_growth_priority"  # type: ignore[arg-type]
    )


def test_dashboard_client_labels_and_hour_bucket_cover_fallbacks() -> None:
    assert _download_client_label("sabnzbd") == "SABnzbd"
    assert _download_client_label("custom_client") == "Custom Client"
    assert _hour_bucket_start(datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC)) == datetime(
        2026,
        1,
        2,
        3,
        tzinfo=UTC,
    )


@pytest.mark.asyncio
async def test_dashboard_cache_rollback_failure_is_non_fatal() -> None:
    session = AsyncMock()
    session.rollback.side_effect = RuntimeError("closed")

    await DashboardIntelligenceService(session)._rollback_after_cache_error()

    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_rollups_loads_and_persists_one_snapshot() -> None:
    service = DashboardIntelligenceService(AsyncMock())
    now = datetime(2026, 1, 2, 3, tzinfo=UTC)
    snapshot = SimpleNamespace(storage=object())
    service._load_snapshot = AsyncMock(return_value=snapshot)  # type: ignore[method-assign]
    service._ensure_daily_storage_snapshot = AsyncMock()  # type: ignore[method-assign]
    service._persist_rollups = AsyncMock()  # type: ignore[method-assign]

    await service.capture_rollups(now=now)

    service._ensure_daily_storage_snapshot.assert_awaited_once_with(snapshot.storage, now)  # type: ignore[attr-defined]
    service._persist_rollups.assert_awaited_once_with(snapshot, now)  # type: ignore[attr-defined]


class _CacheSession:
    def __init__(self, commit_error: Exception | None = None) -> None:
        self.commit_error = commit_error
        self.execute = AsyncMock()
        self.commit = AsyncMock(side_effect=commit_error)
        self.rollback = AsyncMock()

    async def __aenter__(self) -> _CacheSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_best_effort_cache_write_commits_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_session = _CacheSession()
    monkeypatch.setattr(
        "pullbox.database.get_session_factory",
        lambda: lambda: cache_session,
    )
    writer = AsyncMock()

    await DashboardIntelligenceService(AsyncMock())._run_best_effort_cache_write(
        cache_key="metrics",
        writer=writer,
    )

    writer.assert_awaited_once_with(cache_session)
    cache_session.commit.assert_awaited_once()
    cache_session.rollback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commit_error",
    [
        DatabaseError("commit", {}, Exception("database failed")),
        RuntimeError("unexpected"),
    ],
)
async def test_best_effort_cache_write_contains_non_lock_failures(
    monkeypatch: pytest.MonkeyPatch,
    commit_error: Exception,
) -> None:
    cache_session = _CacheSession(commit_error)
    monkeypatch.setattr(
        "pullbox.database.get_session_factory",
        lambda: lambda: cache_session,
    )
    service = DashboardIntelligenceService(AsyncMock())
    service._log_cache_error = MagicMock()  # type: ignore[method-assign]

    await service._run_best_effort_cache_write(
        cache_key="metrics",
        writer=AsyncMock(),
    )

    cache_session.rollback.assert_awaited_once()
    if isinstance(commit_error, DatabaseError):
        service._log_cache_error.assert_called_once_with(  # type: ignore[attr-defined]
            cache_key="metrics",
            exc=commit_error,
        )
    else:
        service._log_cache_error.assert_not_called()  # type: ignore[attr-defined]

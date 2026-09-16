"""Boundary tests for the ImportService Step 3 facade."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pullbox.core.exceptions import NotFoundError
from pullbox.models.import_job import ImportSeriesStatus
from pullbox.services.import_service_review import ImportServiceReviewMixin


def _service() -> ImportServiceReviewMixin:
    service = ImportServiceReviewMixin()
    service._recompute_file_counters = AsyncMock()  # type: ignore[attr-defined]
    service._recompute_series_counters = AsyncMock()  # type: ignore[attr-defined]
    return service


@pytest.mark.asyncio
async def test_resolve_conflicts_drops_internal_file_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = AsyncMock(return_value=([1], [2], {1: [3]}))
    monkeypatch.setattr("pullbox.services.import_service_review.resolve_import_conflicts", resolver)
    service = _service()

    assert await service.resolve_conflicts(AsyncMock(), 10, [(1, 3)]) == ([1], [2])


@pytest.mark.asyncio
async def test_allow_safety_file_requires_its_imported_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pullbox.services.import_service_review.allow_import_safety_blocked_file_once",
        AsyncMock(return_value=SimpleNamespace(import_series_id=7)),
    )
    session = AsyncMock()
    session.get.return_value = None

    with pytest.raises(NotFoundError, match="ImportedSeries"):
        await _service().allow_safety_blocked_file_once(session, 1, 2)


@pytest.mark.asyncio
async def test_allow_safety_file_for_retry_returns_job_and_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pullbox.services.import_service_review.allow_import_safety_blocked_file_once",
        AsyncMock(return_value=SimpleNamespace(import_series_id=7)),
    )
    job = SimpleNamespace(id=1)
    series = SimpleNamespace(id=7)
    session = AsyncMock()
    session.get.side_effect = [job, series]

    result = await _service().allow_safety_blocked_file_once_for_retry(session, 1, 2)

    assert result == (job, series)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[None], [SimpleNamespace(id=1), None]])
async def test_allow_safety_file_for_retry_rejects_missing_parent_rows(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[object | None],
) -> None:
    monkeypatch.setattr(
        "pullbox.services.import_service_review.allow_import_safety_blocked_file_once",
        AsyncMock(return_value=SimpleNamespace(import_series_id=7)),
    )
    session = AsyncMock()
    session.get.side_effect = rows

    with pytest.raises(NotFoundError):
        await _service().allow_safety_blocked_file_once_for_retry(session, 1, 2)


@pytest.mark.asyncio
async def test_skip_safety_file_requires_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pullbox.services.import_service_review.skip_import_safety_blocked_file",
        AsyncMock(return_value=SimpleNamespace(import_series_id=7)),
    )
    session = AsyncMock()
    session.get.return_value = None

    with pytest.raises(NotFoundError, match="ImportedSeries"):
        await _service().skip_safety_blocked_file(session, 1, 2)


@pytest.mark.asyncio
async def test_skip_last_safety_file_closes_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pullbox.services.import_service_review.skip_import_safety_blocked_file",
        AsyncMock(return_value=SimpleNamespace(import_series_id=7)),
    )
    series = SimpleNamespace(
        files_matched=0,
        files_conflict=0,
        files_no_match=0,
        diagnostics={},
        status=ImportSeriesStatus.NO_MATCH,
        selected_for_import=True,
    )
    job = SimpleNamespace(id=1)
    session = AsyncMock()
    session.get.side_effect = [series, job]
    service = _service()

    result = await service.skip_safety_blocked_file(session, 1, 2)

    assert result is series
    assert series.status is ImportSeriesStatus.SKIPPED
    assert series.selected_for_import is False
    service._recompute_series_counters.assert_awaited_once_with(session, job)  # type: ignore[attr-defined]
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_selection_requires_job() -> None:
    session = AsyncMock()
    session.get.return_value = None

    with pytest.raises(NotFoundError, match="ImportJob"):
        await _service().get_review_selection_state(session, 404)

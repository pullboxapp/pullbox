"""Direct branch coverage for series API route orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fastapi import Response
from sqlalchemy import select

from pullbox.api.v1 import series as series_api
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.publisher import Publisher
from pullbox.models.search_log import SearchLog
from pullbox.models.series import Series, SeriesStatus, SeriesStatusOverride, SeriesType
from pullbox.schemas.series import (
    SeriesBulkDelete,
    SeriesCreate,
    SeriesDeleteContextRequest,
    SeriesResponse,
    SeriesUpdate,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _user() -> SimpleNamespace:
    return SimpleNamespace(username="admin")


def _series_response(series_id: int = 1, *, monitored: bool = True) -> SeriesResponse:
    now = datetime.now(UTC)
    return SeriesResponse(
        id=series_id,
        comicvine_id=9001,
        title="Absolute Superman",
        sort_title="absolute superman",
        year_start=2025,
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
        issue_count=10,
        monitored=monitored,
        created_at=now,
        updated_at=now,
    )


async def _seed_series(
    session: AsyncSession,
    *,
    title: str = "Absolute Superman",
    publisher: Publisher | None = None,
    monitored: bool = True,
    comicvine_id: int | None = None,
) -> Series:
    series = Series(
        comicvine_id=comicvine_id or sum(ord(char) for char in title) + 10_000,
        title=title,
        sort_title=title.lower(),
        year_start=2025,
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
        monitored=monitored,
        issue_count=2,
        publisher=publisher,
    )
    session.add(series)
    await session.flush()
    return series


@pytest.mark.asyncio
async def test_service_builder_seams_delegate(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_service = object()
    series_service = object()
    monkeypatch.setattr(
        "pullbox.composition.services.build_metadata_service",
        AsyncMock(return_value=metadata_service),
    )
    monkeypatch.setattr(
        "pullbox.composition.services.build_series_service",
        lambda metadata: series_service if metadata is metadata_service else None,
    )

    assert await series_api._build_metadata_service(db_session) is metadata_service
    assert await series_api._build_series_service(db_session) is series_service


@pytest.mark.asyncio
async def test_load_and_list_series_branches(db_session: AsyncSession) -> None:
    publisher = Publisher(name="DC Comics")
    preferred_root = LibraryRoot(name="Future", path="/future", enabled=True)
    db_session.add_all([publisher, preferred_root])
    await db_session.flush()
    series = await _seed_series(db_session, publisher=publisher, monitored=False)
    series.preferred_library_root_id = preferred_root.id
    db_session.add_all(
        [
            Issue(series_id=series.id, issue_number=1, status=IssueStatus.OWNED),
            Issue(series_id=series.id, issue_number=2, status=IssueStatus.WANTED),
        ]
    )
    await db_session.flush()

    loaded = await series_api._load_series_response(db_session, series.id)
    assert loaded.id == series.id
    assert loaded.publisher_name == "DC Comics"
    assert loaded.owned_count == 1
    assert loaded.wanted_count == 1
    assert loaded.preferred_library_root_id == preferred_root.id

    with pytest.raises(NotFoundError):
        await series_api._load_series_response(db_session, 99999)

    listed = await series_api.list_series(
        _user(),
        db_session,
        limit=1,
        offset=0,
        publisher_id=publisher.id,
        status=SeriesStatus.CONTINUING,
        monitored=False,
        year=2025,
        sort="year",
        order="desc",
    )

    assert listed.total == 1
    assert listed.items[0].publisher_name == "DC Comics"
    assert listed.items[0].owned_count == 1
    assert listed.items[0].wanted_count == 1
    assert listed.items[0].preferred_library_root_id == preferred_root.id
    assert listed.has_more is False


def test_enrich_series_fallback_counts() -> None:
    series = Series(
        id=99,
        title="Fallback Series",
        sort_title="fallback series",
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
        monitored=True,
        issue_count=2,
        library_root_id=10,
        preferred_library_root_id=20,
    )
    series.issues = [
        Issue(series_id=99, issue_number=1, status=IssueStatus.OWNED),
        Issue(series_id=99, issue_number=2, status=IssueStatus.WANTED),
    ]

    detail = series_api._enrich_series(series)
    listed = series_api._enrich_series_list(series)

    assert detail["owned_count"] == 1
    assert detail["wanted_count"] == 1
    assert listed["owned_count"] == 1
    assert listed["wanted_count"] == 1
    assert detail["preferred_library_root_id"] == 20
    assert listed["preferred_library_root_id"] == 20


@pytest.mark.asyncio
async def test_get_series_wrapper_and_bulk_update(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_response = AsyncMock(return_value=_series_response(55))
    monkeypatch.setattr(series_api, "_load_series_response", load_response)

    response = await series_api.get_series(55, _user(), db_session)
    assert response.id == 55
    load_response.assert_awaited_once_with(db_session, 55)

    first = await _seed_series(db_session, title="First", monitored=False, comicvine_id=9101)
    second = await _seed_series(db_session, title="Second", monitored=False, comicvine_id=9102)
    result = await series_api.bulk_update_series(
        series_api.SeriesBulkUpdate(series_ids=[first.id, second.id, 999], monitored=True),
        _user(),
        db_session,
    )
    assert result == {"updated": 2, "skipped": 1}


@pytest.mark.asyncio
async def test_bulk_delete_and_delete_context_branches(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_ids: list[int] = []

    async def fake_delete(
        _session: AsyncSession,
        series_id: int,
        *,
        delete_files: bool = False,
        delete_folder: bool = False,
    ) -> None:
        assert delete_files is True
        assert delete_folder is True
        if series_id == 2:
            raise NotFoundError("Series", series_id)
        deleted_ids.append(series_id)

    async def fake_build_delete_context(
        _session: AsyncSession,
        series_ids: list[int],
    ) -> SimpleNamespace:
        return SimpleNamespace(
            series_count=len(series_ids),
            linked_file_count=4,
            managed_file_count=3,
            referenced_file_count=1,
        )

    monkeypatch.setattr(series_api.SeriesService, "delete", fake_delete)
    monkeypatch.setattr(
        series_api.SeriesService,
        "build_delete_context",
        fake_build_delete_context,
    )

    result = await series_api.bulk_delete_series(
        SeriesBulkDelete(series_ids=[1, 2, 3], delete_files=True, delete_folder=True),
        _user(),
        db_session,
    )
    assert result == {"deleted": 2, "skipped": 1}
    assert deleted_ids == [1, 3]

    context = await series_api.build_series_delete_context(
        SeriesDeleteContextRequest(series_ids=[1, 3]),
        _user(),
        db_session,
    )
    assert context.series_count == 2
    assert context.linked_file_count == 4
    assert context.managed_file_count == 3
    assert context.referenced_file_count == 1


@pytest.mark.asyncio
async def test_add_update_refresh_and_folder_routes_delegate(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(
        add_from_comicvine=AsyncMock(return_value=SimpleNamespace(id=10)),
        set_status_override=AsyncMock(),
        toggle_monitoring=AsyncMock(),
        rename_all_series_folders=AsyncMock(return_value={"renamed": 2, "skipped": 1}),
        rename_series_folder=AsyncMock(return_value="/comics/Absolute Superman (2025)"),
    )
    metadata_service = SimpleNamespace(refresh_series=AsyncMock())
    load_response = AsyncMock(side_effect=lambda _session, series_id: _series_response(series_id))
    monkeypatch.setattr(series_api, "_build_series_service", AsyncMock(return_value=service))
    monkeypatch.setattr(
        series_api,
        "_build_metadata_service",
        AsyncMock(return_value=metadata_service),
    )
    monkeypatch.setattr(series_api, "_load_series_response", load_response)
    monkeypatch.setattr(series_api, "load_search_on_add_default", AsyncMock(return_value=True))

    with pytest.raises(ValidationError, match="global import policy"):
        await series_api.add_series(
            SeriesCreate(comicvine_id=1234, search_on_add=False),
            _user(),
            db_session,
        )

    added = await series_api.add_series(SeriesCreate(comicvine_id=1234), _user(), db_session)
    assert added.id == 10
    service.add_from_comicvine.assert_awaited_once_with(
        db_session,
        1234,
        None,
        search_on_add=True,
    )

    updated = await series_api.update_series(
        10,
        SeriesUpdate(monitored=False),
        _user(),
        db_session,
    )
    assert updated.id == 10
    service.toggle_monitoring.assert_awaited_once_with(db_session, 10, False)

    await series_api.update_series(
        10,
        SeriesUpdate(status_override=SeriesStatusOverride.ENDED),
        _user(),
        db_session,
    )
    service.set_status_override.assert_awaited_once_with(
        db_session,
        10,
        SeriesStatusOverride.ENDED,
    )

    await series_api.update_series(
        10,
        SeriesUpdate(status_override=None),
        _user(),
        db_session,
    )
    service.set_status_override.assert_awaited_with(db_session, 10, None)

    no_toggle = await series_api.update_series(11, SeriesUpdate(), _user(), db_session)
    assert no_toggle.id == 11
    assert service.toggle_monitoring.await_count == 1
    assert service.set_status_override.await_count == 2

    assert await series_api.rename_all_folders(_user(), db_session) == {
        "renamed": 2,
        "skipped": 1,
    }
    assert await series_api.rename_series_folder(10, _user(), db_session) == {
        "new_path": "/comics/Absolute Superman (2025)"
    }

    refreshed = await series_api.refresh_series(10, _user(), db_session)
    assert refreshed.id == 10
    metadata_service.refresh_series.assert_awaited_once_with(db_session, 10, force=True)


@pytest.mark.asyncio
async def test_delete_series_delegates(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delete = AsyncMock()
    monkeypatch.setattr(series_api.SeriesService, "delete", delete)

    await series_api.delete_series(42, _user(), db_session)

    delete.assert_awaited_once_with(db_session, 42)


@pytest.mark.asyncio
async def test_list_series_issues_success_and_missing(db_session: AsyncSession) -> None:
    series = await _seed_series(db_session)
    issue = Issue(
        series_id=series.id,
        issue_number=1,
        title="Issue #1",
        status=IssueStatus.OWNED,
        issue_type=IssueType.ISSUE,
    )
    root = LibraryRoot(name="Root", path="/comics", enabled=True)
    db_session.add_all([issue, root])
    await db_session.flush()
    db_session.add(
        LibraryFile(
            issue_id=issue.id,
            library_root_id=root.id,
            file_path="/comics/Absolute Superman 001.cbz",
            file_name="Absolute Superman 001.cbz",
            file_size=123,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
        )
    )
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await series_api.list_series_issues(99999, _user(), db_session)

    response = await series_api.list_series_issues(
        series.id,
        _user(),
        db_session,
        limit=1,
        offset=0,
    )
    assert response.total == 1
    assert response.items[0].has_file is True


@pytest.mark.asyncio
async def test_search_series_no_wanted_and_started_branches(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series = await _seed_series(db_session)
    hx_request = SimpleNamespace(headers={"HX-Request": "true"})
    response = Response()

    monkeypatch.setattr(
        series_api,
        "load_series_wanted_search_targets",
        AsyncMock(return_value=[]),
    )
    no_wanted = await series_api.search_series(
        series.id,
        hx_request,
        response,
        _user(),
        db_session,
    )
    assert no_wanted["status"] == "no_wanted"
    assert "HX-Trigger" in response.headers

    with pytest.raises(NotFoundError):
        await series_api.search_series(99999, hx_request, Response(), _user(), db_session)

    target = SimpleNamespace(
        issue_id=88,
        series_title=series.title,
        issue_number=1.0,
    )
    db_session.add(Issue(id=88, series_id=series.id, issue_number=1, status=IssueStatus.WANTED))
    await db_session.flush()

    async def fake_search_series_issues(
        _series_id: int,
        *,
        pending_log_ids_by_issue: dict[int, int],
    ) -> None:
        assert pending_log_ids_by_issue[88] > 0

    monkeypatch.setattr(
        series_api,
        "load_series_wanted_search_targets",
        AsyncMock(return_value=[target]),
    )
    monkeypatch.setattr(series_api, "search_series_issues", fake_search_series_issues)
    monkeypatch.setattr(series_api.time, "time", lambda: 12345)

    started_response = Response()
    started = await series_api.search_series(
        series.id,
        hx_request,
        started_response,
        _user(),
        db_session,
    )
    assert started["status"] == "started"
    assert started["task_id"] == f"search_series_{series.id}_12345"
    assert "HX-Trigger" in started_response.headers

    logs = list((await db_session.execute(select(SearchLog))).scalars().all())
    assert len(logs) == 1
    assert logs[0].issue_id == 88

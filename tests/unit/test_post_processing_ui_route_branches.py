"""Focused branch coverage for post-processing UI route helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.ui import post_processing_routes


class RecordingTemplates:
    """Tiny templates stand-in that records render calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], dict[str, object]]] = []

    def TemplateResponse(  # noqa: N802 - mirrors Starlette's template API.
        self,
        _request: object,
        template_name: str,
        context: dict[str, object],
        **kwargs: object,
    ) -> SimpleNamespace:
        response = SimpleNamespace(
            template_name=template_name,
            context=context,
            headers=dict(kwargs.get("headers") or {}),
            status_code=200,
        )
        self.calls.append((template_name, context, kwargs))
        return response


@pytest.fixture
def configured_post_processing_routes(monkeypatch: pytest.MonkeyPatch) -> RecordingTemplates:
    templates = RecordingTemplates()

    async def _live_status_map(
        _session: object,
        items: list[DownloadHistory],
    ) -> dict[int, dict[str, object]]:
        return {item.id: {"phase_label": "Importing"} for item in items if item.id is not None}

    monkeypatch.setattr(post_processing_routes, "_get_templates", lambda: templates)
    monkeypatch.setattr(
        post_processing_routes,
        "_build_context",
        lambda request, user=None, **kwargs: {"request": request, "user": user, **kwargs},
    )
    monkeypatch.setattr(
        post_processing_routes,
        "_download_client_label",
        lambda value: f"Client {value}",
    )
    monkeypatch.setattr(post_processing_routes, "_get_recent_completion_ids", lambda: set())
    monkeypatch.setattr(post_processing_routes, "_load_live_status_map", _live_status_map)
    monkeypatch.setattr(
        post_processing_routes,
        "_sidebar_badge_no_store_headers",
        {"Cache-Control": "no-store"},
    )
    return templates


@pytest.fixture
def route_request() -> SimpleNamespace:
    return SimpleNamespace(headers={}, cookies={}, state=SimpleNamespace())


@pytest.mark.parametrize(
    ("attribute", "callable_name", "error"),
    [
        ("_get_templates", "_templates", "templates"),
        ("_build_context", "_ctx", "context builder"),
        ("_download_client_label", "_client_label", "client labeler"),
        ("_get_recent_completion_ids", "_recent_completion_ids", "completion tracking"),
        ("_load_live_status_map", "_live_status_map", "live status loading"),
    ],
)
@pytest.mark.asyncio
async def test_post_processing_runtime_dependency_guards(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    callable_name: str,
    error: str,
) -> None:
    monkeypatch.setattr(post_processing_routes, attribute, None)
    callable_obj = getattr(post_processing_routes, callable_name)

    with pytest.raises(RuntimeError, match=error):
        if callable_name == "_ctx":
            callable_obj(SimpleNamespace())
        elif callable_name == "_client_label":
            callable_obj("sabnzbd")
        elif callable_name == "_live_status_map":
            await callable_obj(SimpleNamespace(), [])
        else:
            callable_obj()


def test_configure_post_processing_routes_sets_runtime_dependencies() -> None:
    templates = RecordingTemplates()
    originals = {
        "_get_templates": post_processing_routes._get_templates,
        "_build_context": post_processing_routes._build_context,
        "_download_client_label": post_processing_routes._download_client_label,
        "_get_recent_completion_ids": post_processing_routes._get_recent_completion_ids,
        "_load_live_status_map": post_processing_routes._load_live_status_map,
        "_sidebar_badge_no_store_headers": post_processing_routes._sidebar_badge_no_store_headers,
    }

    async def _live_status_map(
        _session: object,
        _items: list[DownloadHistory],
    ) -> dict[int, dict[str, object]]:
        return {}

    try:
        post_processing_routes.configure_post_processing_routes(
            get_templates=lambda: templates,
            build_context=lambda request, user=None, **kwargs: {
                "request": request,
                "user": user,
                **kwargs,
            },
            download_client_label=lambda value: value.upper(),
            get_recent_completion_ids=lambda: {1, 2},
            load_live_status_map=_live_status_map,
            sidebar_badge_no_store_headers={"Cache-Control": "no-store"},
        )

        assert post_processing_routes._templates() is templates
        assert post_processing_routes._client_label("sabnzbd") == "SABNZBD"
        assert post_processing_routes._recent_completion_ids() == {1, 2}
    finally:
        for name, value in originals.items():
            setattr(post_processing_routes, name, value)


def test_post_processing_normalizers_cover_default_and_legacy_values() -> None:
    assert post_processing_routes.normalize_post_processing_result_filter("failed") == "failed"
    assert post_processing_routes.normalize_post_processing_result_filter("weird") == "all"
    assert post_processing_routes.normalize_post_processing_tab("history") == "history"
    assert post_processing_routes.normalize_post_processing_tab("weird") == "queue"
    assert post_processing_routes.normalize_post_processing_filter_alias("active") == "all"
    assert post_processing_routes.normalize_post_processing_filter_alias("imported") == "imported"
    assert post_processing_routes.normalize_post_processing_filter_alias("bad") == "all"
    assert (
        post_processing_routes.resolve_post_processing_result_filter("failed", "active") == "failed"
    )
    assert post_processing_routes.resolve_post_processing_result_filter(None, "active") == "all"
    assert post_processing_routes.normalize_post_processing_sort(None) == "-completed_at"
    assert post_processing_routes.normalize_post_processing_sort("client") == "client"
    assert post_processing_routes.normalize_post_processing_sort("bogus") == "-completed_at"
    assert len(post_processing_routes.get_post_processing_history_order_by("-size")) == 2


@pytest.mark.asyncio
async def test_load_post_processing_client_options_dedupes_and_preserves_current(
    configured_post_processing_routes: RecordingTemplates,
    db_session,
) -> None:
    series = Series(title="Batman", sort_title="batman")
    db_session.add(series)
    await db_session.flush()
    issue = Issue(series_id=series.id, issue_number=1)
    db_session.add(issue)
    await db_session.flush()
    db_session.add_all(
        [
            DownloadHistory(
                issue_id=issue.id,
                title="Imported",
                download_url="https://example.test/imported",
                download_client=DownloadClientType.SABNZBD,
                state=DownloadState.COMPLETED,
                downloaded_path="/downloads/imported.cbz",
                completed_at=datetime(2026, 1, 1, tzinfo=UTC),
                imported_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            DownloadHistory(
                issue_id=issue.id,
                title="Imported Again",
                download_url="https://example.test/imported-again",
                download_client=DownloadClientType.SABNZBD,
                state=DownloadState.COMPLETED,
                downloaded_path="/downloads/imported-again.cbz",
                completed_at=datetime(2026, 1, 2, tzinfo=UTC),
                imported_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        ]
    )
    await db_session.commit()

    options = await post_processing_routes.load_post_processing_client_options(
        db_session,
        current_client="deluge",
    )

    assert options == [
        ("", "All Clients"),
        ("sabnzbd", "Client sabnzbd"),
        ("deluge", "Client deluge"),
    ]


@pytest.mark.asyncio
async def test_load_post_processing_status_context_separates_active_recent_and_history(
    configured_post_processing_routes: RecordingTemplates,
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    series = Series(title="Batman", sort_title="batman")
    db_session.add(series)
    await db_session.flush()
    issue = Issue(series_id=series.id, issue_number=1)
    db_session.add(issue)
    await db_session.flush()
    active = DownloadHistory(
        issue_id=issue.id,
        title="Active",
        download_url="https://example.test/active",
        download_client=DownloadClientType.SABNZBD,
        state=DownloadState.POST_PROCESSING,
        downloaded_path="/downloads/active.cbz",
        updated_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    recent_import = DownloadHistory(
        issue_id=issue.id,
        title="Recent Imported",
        download_url="https://example.test/recent",
        download_client=DownloadClientType.SABNZBD,
        state=DownloadState.COMPLETED,
        downloaded_path="/downloads/recent.cbz",
        completed_at=datetime(2026, 1, 3, tzinfo=UTC),
        imported_at=datetime(2026, 1, 3, tzinfo=UTC),
        updated_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    normal_import = DownloadHistory(
        issue_id=issue.id,
        title="Normal Imported",
        download_url="https://example.test/normal",
        download_client=DownloadClientType.SABNZBD,
        state=DownloadState.COMPLETED,
        downloaded_path="/downloads/normal.cbz",
        completed_at=datetime(2026, 1, 2, tzinfo=UTC),
        imported_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    failed = DownloadHistory(
        issue_id=issue.id,
        title="Failed Import",
        download_url="https://example.test/failed",
        download_client=DownloadClientType.QBITTORRENT,
        state=DownloadState.FAILED,
        downloaded_path="/downloads/failed.cbz",
        error_message="Source disappeared",
    )
    db_session.add_all([active, recent_import, normal_import, failed])
    await db_session.commit()
    for item in (active, recent_import, normal_import, failed):
        await db_session.refresh(item)
    monkeypatch.setattr(
        post_processing_routes,
        "_get_recent_completion_ids",
        lambda: {active.id, recent_import.id},
    )

    context = await post_processing_routes.load_post_processing_status_context(db_session)

    assert [item.id for item in context["active_items"]] == [active.id]
    assert [item.id for item in context["recent_imported_items"]] == [recent_import.id]
    assert context["live_status_map"] == {active.id: {"phase_label": "Importing"}}
    assert context["active_count"] == 1
    assert context["recent_imported_count"] == 1
    assert context["imported_count"] == 1
    assert context["failed_count"] == 1
    assert context["total_count"] == 4


@pytest.mark.asyncio
async def test_load_post_processing_history_context_filters_counts_and_paginates(
    configured_post_processing_routes: RecordingTemplates,
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    series = Series(title="Batman", sort_title="batman")
    db_session.add(series)
    await db_session.flush()
    issue = Issue(series_id=series.id, issue_number=1)
    db_session.add(issue)
    await db_session.flush()
    imported = DownloadHistory(
        issue_id=issue.id,
        title="Batman Imported",
        download_url="https://example.test/imported",
        download_client=DownloadClientType.SABNZBD,
        state=DownloadState.COMPLETED,
        downloaded_path="/downloads/imported.cbz",
        completed_at=datetime(2026, 1, 2, tzinfo=UTC),
        imported_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    recent = DownloadHistory(
        issue_id=issue.id,
        title="Batman Recent",
        download_url="https://example.test/recent",
        download_client=DownloadClientType.SABNZBD,
        state=DownloadState.COMPLETED,
        downloaded_path="/downloads/recent.cbz",
        completed_at=datetime(2026, 1, 3, tzinfo=UTC),
        imported_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    failed = DownloadHistory(
        issue_id=issue.id,
        title="Batman Failed",
        download_url="https://example.test/failed",
        download_client=DownloadClientType.SABNZBD,
        state=DownloadState.FAILED,
        downloaded_path="/downloads/failed.cbz",
        error_message="Archive failed",
        completed_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    other_client = DownloadHistory(
        issue_id=issue.id,
        title="Batman Deluge Failed",
        download_url="https://example.test/deluge",
        download_client=DownloadClientType.DELUGE,
        state=DownloadState.FAILED,
        downloaded_path="/downloads/deluge.cbz",
        error_message="Archive failed",
    )
    db_session.add_all([imported, recent, failed, other_client])
    await db_session.commit()
    await db_session.refresh(recent)
    monkeypatch.setattr(post_processing_routes, "_get_recent_completion_ids", lambda: {recent.id})

    context = await post_processing_routes.load_post_processing_history_context(
        db_session,
        result_value="all",
        client_value=" sabnzbd ",
        search_query=" Batman ",
        page=99,
        sort="result",
    )

    assert [item.title for item in context["history_items"]] == ["Batman Imported", "Batman Failed"]
    assert context["history_total"] == 2
    assert context["history_imported_count"] == 1
    assert context["history_failed_count"] == 1
    assert context["page"] == 1
    assert context["total_pages"] == 1
    assert context["result_filter"] == "all"
    assert context["client_filter"] == "sabnzbd"
    assert context["search_query"] == "Batman"
    assert context["sort"] == "result"
    assert ("deluge", "Client deluge") in context["client_options"]
    assert ("sabnzbd", "Client sabnzbd") in context["client_options"]


@pytest.mark.asyncio
async def test_post_processing_routes_select_expected_templates(
    configured_post_processing_routes: RecordingTemplates,
    monkeypatch: pytest.MonkeyPatch,
    db_session,
    route_request: SimpleNamespace,
) -> None:
    status_ctx = {
        "active_items": [],
        "recent_imported_items": [],
        "live_status_map": {},
        "active_count": 1,
        "recent_imported_count": 2,
        "imported_count": 3,
        "failed_count": 4,
        "total_count": 10,
    }
    history_ctx = {
        "history_items": [],
        "history_total": 7,
        "history_imported_count": 3,
        "history_failed_count": 4,
        "page": 1,
        "total_pages": 1,
        "result_filter": "failed",
        "client_filter": "",
        "client_options": [("", "All Clients")],
        "search_query": "batman",
        "sort": "title",
    }
    captured_history_args: list[dict[str, object]] = []

    async def _status(_session: object) -> dict[str, object]:
        return status_ctx

    async def _history(_session: object, **kwargs: object) -> dict[str, object]:
        captured_history_args.append(kwargs)
        return history_ctx

    monkeypatch.setattr(post_processing_routes, "load_post_processing_status_context", _status)
    monkeypatch.setattr(post_processing_routes, "load_post_processing_history_context", _history)

    full = await post_processing_routes.post_processing(
        route_request,
        user=SimpleNamespace(username="admin"),
        session=db_session,
        tab="weird",
        result=None,
        filter="active",
        client=None,
        search="batman",
        sort="title",
        page=1,
    )
    hx_bundle = await post_processing_routes.post_processing(
        SimpleNamespace(headers={"HX-Request": "true"}, cookies={}, state=SimpleNamespace()),
        user=SimpleNamespace(username="admin"),
        session=db_session,
        tab="history",
        result="failed",
        filter=None,
        client="sabnzbd",
        search="batman",
        sort="title",
        page=2,
    )
    hx_results = await post_processing_routes.post_processing(
        SimpleNamespace(
            headers={"HX-Request": "true", "HX-Target": "pp-history-results"},
            cookies={},
            state=SimpleNamespace(),
        ),
        user=SimpleNamespace(username="admin"),
        session=db_session,
        tab="history",
        result="failed",
        filter=None,
        client="sabnzbd",
        search="batman",
        sort="title",
        page=3,
    )

    assert full.template_name == "pages/post_processing.html"
    assert full.context["tab"] == "queue"
    assert hx_bundle.template_name == "partials/pp_content_bundle.html"
    assert hx_results.template_name == "partials/pp_history_results_bundle.html"
    assert captured_history_args[0]["result_value"] == "all"
    assert captured_history_args[1]["client_value"] == "sabnzbd"
    assert captured_history_args[2]["page"] == 3


@pytest.mark.asyncio
async def test_post_processing_htmx_queue_history_and_alias_routes(
    configured_post_processing_routes: RecordingTemplates,
    monkeypatch: pytest.MonkeyPatch,
    db_session,
    route_request: SimpleNamespace,
) -> None:
    status_ctx = {
        "active_items": [],
        "recent_imported_items": [],
        "live_status_map": {},
        "active_count": 0,
        "recent_imported_count": 0,
        "imported_count": 5,
        "failed_count": 6,
        "total_count": 11,
    }
    history_ctx = {
        "history_items": [],
        "history_total": 11,
        "history_imported_count": 5,
        "history_failed_count": 6,
        "page": 1,
        "total_pages": 1,
        "result_filter": "imported",
        "client_filter": "sabnzbd",
        "client_options": [("", "All Clients")],
        "search_query": "",
        "sort": "-completed_at",
    }

    async def _status(_session: object) -> dict[str, object]:
        return status_ctx

    async def _history(_session: object, **_kwargs: object) -> dict[str, object]:
        return history_ctx

    monkeypatch.setattr(post_processing_routes, "load_post_processing_status_context", _status)
    monkeypatch.setattr(post_processing_routes, "load_post_processing_history_context", _history)

    queue = await post_processing_routes.htmx_pp_queue(
        route_request,
        user=SimpleNamespace(username="admin"),
        session=db_session,
    )
    alias = await post_processing_routes.htmx_pp_status(
        route_request,
        user=SimpleNamespace(username="admin"),
        session=db_session,
    )
    history = await post_processing_routes.htmx_pp_history(
        route_request,
        user=SimpleNamespace(username="admin"),
        session=db_session,
        result="imported",
        filter=None,
        client="sabnzbd",
        search="",
        sort="-completed_at",
        page=1,
    )

    assert queue.template_name == "partials/pp_queue_bundle.html"
    assert queue.context["history_total"] == 11
    assert queue.headers == {"Cache-Control": "no-store"}
    assert alias.template_name == "partials/pp_queue_bundle.html"
    assert history.template_name == "partials/pp_history_results_bundle.html"
    assert history.context["tab"] == "history"


@pytest.mark.asyncio
async def test_post_processing_queue_detail_success_and_missing(
    configured_post_processing_routes: RecordingTemplates,
    db_session,
    route_request: SimpleNamespace,
) -> None:
    series = Series(title="Batman", sort_title="batman")
    db_session.add(series)
    await db_session.flush()
    issue = Issue(series_id=series.id, issue_number=1)
    db_session.add(issue)
    await db_session.flush()
    download = DownloadHistory(
        issue_id=issue.id,
        title="Batman 001",
        download_url="https://example.test/batman",
        download_client=DownloadClientType.QBITTORRENT,
        state=DownloadState.POST_PROCESSING,
        downloaded_path="/downloads/batman.cbz",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(download)
    await db_session.commit()
    await db_session.refresh(download)

    response = await post_processing_routes.htmx_pp_queue_detail(
        route_request,
        download_id=download.id,
        user=SimpleNamespace(username="admin"),
        session=db_session,
    )

    assert response.template_name == "partials/pp_queue_detail.html"
    assert response.context["dl"].id == download.id
    assert response.context["live"] == {"phase_label": "Importing"}

    with pytest.raises(HTTPException) as exc:
        await post_processing_routes.htmx_pp_queue_detail(
            route_request,
            download_id=999_999,
            user=SimpleNamespace(username="admin"),
            session=db_session,
        )
    assert exc.value.status_code == 404

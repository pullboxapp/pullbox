"""Focused branch coverage for downloads UI route helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.ui import downloads_routes


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
def configured_downloads_routes(monkeypatch: pytest.MonkeyPatch) -> RecordingTemplates:
    templates = RecordingTemplates()

    async def _queue_names(
        _session: object,
        downloads: list[DownloadHistory],
    ) -> dict[int, str]:
        return {item.id: f"Renamed {item.title}" for item in downloads if item.id is not None}

    async def _progress_map(
        _session: object,
        queue_items: list[DownloadHistory],
        *,
        fallback_progress: dict[int, object],
    ) -> dict[int, object]:
        return {
            **fallback_progress,
            **{
                item.id: SimpleNamespace(progress=0.5, eta_seconds=60, speed_bytes=100)
                for item in queue_items
                if item.id is not None
            },
        }

    monkeypatch.setattr(downloads_routes, "_get_templates", lambda: templates)
    monkeypatch.setattr(
        downloads_routes,
        "_build_context",
        lambda request, user=None, **kwargs: {"request": request, "user": user, **kwargs},
    )
    monkeypatch.setattr(downloads_routes, "_format_eta", lambda value: f"{value}s")
    monkeypatch.setattr(downloads_routes, "_build_queue_names", _queue_names)
    monkeypatch.setattr(downloads_routes, "_load_download_progress_map", _progress_map)
    monkeypatch.setattr(
        downloads_routes,
        "_sidebar_badge_no_store_headers",
        {"Cache-Control": "no-store"},
    )
    return templates


@pytest.fixture
def route_request() -> SimpleNamespace:
    return SimpleNamespace(headers={}, cookies={}, state=SimpleNamespace())


async def _seed_download_base(db_session) -> Issue:
    series = Series(title="Batman", sort_title="batman")
    db_session.add(series)
    await db_session.flush()
    issue = Issue(series_id=series.id, issue_number=1)
    db_session.add(issue)
    await db_session.flush()
    return issue


@pytest.mark.parametrize(
    ("attribute", "callable_name", "error"),
    [
        ("_get_templates", "_templates", "templates"),
        ("_build_context", "_ctx", "context builder"),
        ("_format_eta", "_eta", "ETA formatter"),
        ("_build_queue_names", "_queue_names", "queue-name builder"),
        ("_load_download_progress_map", "_progress_map", "progress-map loader"),
    ],
)
@pytest.mark.asyncio
async def test_downloads_runtime_dependency_guards(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
    attribute: str,
    callable_name: str,
    error: str,
) -> None:
    monkeypatch.setattr(downloads_routes, attribute, None)
    callable_obj = getattr(downloads_routes, callable_name)

    with pytest.raises(RuntimeError, match=error):
        if callable_name == "_ctx":
            callable_obj(SimpleNamespace())
        elif callable_name == "_eta":
            callable_obj(60)
        elif callable_name == "_queue_names":
            await callable_obj(db_session, [])
        elif callable_name == "_progress_map":
            await callable_obj(db_session, [], fallback_progress={})
        else:
            callable_obj()


def test_configure_downloads_routes_sets_runtime_dependencies() -> None:
    templates = RecordingTemplates()
    originals = {
        "_get_templates": downloads_routes._get_templates,
        "_build_context": downloads_routes._build_context,
        "_format_eta": downloads_routes._format_eta,
        "_build_queue_names": downloads_routes._build_queue_names,
        "_load_download_progress_map": downloads_routes._load_download_progress_map,
        "_sidebar_badge_no_store_headers": downloads_routes._sidebar_badge_no_store_headers,
    }

    async def _queue_names(_session: object, _downloads: list[DownloadHistory]) -> dict[int, str]:
        return {}

    async def _progress_map(
        _session: object,
        _queue_items: list[DownloadHistory],
        *,
        fallback_progress: dict[int, object],
    ) -> dict[int, object]:
        return fallback_progress

    try:
        downloads_routes.configure_downloads_routes(
            get_templates=lambda: templates,
            build_context=lambda request, user=None, **kwargs: {
                "request": request,
                "user": user,
                **kwargs,
            },
            format_eta=lambda value: f"eta:{value}",
            build_queue_names=_queue_names,
            load_download_progress_map=_progress_map,
            sidebar_badge_no_store_headers={"Cache-Control": "no-store"},
        )

        assert downloads_routes._templates() is templates
        assert downloads_routes._eta(30) == "eta:30"
    finally:
        for name, value in originals.items():
            setattr(downloads_routes, name, value)


def test_downloads_normalizers_and_filter_helpers_cover_edge_values() -> None:
    assert downloads_routes.normalize_download_history_sort(None) == "-updated_at"
    assert downloads_routes.normalize_download_history_sort("status") == "status"
    assert downloads_routes.normalize_download_history_sort("not-real") == "-updated_at"
    assert len(downloads_routes.get_download_history_order_by("-client")) == 3
    assert downloads_routes.download_client_type_label("sabnzbd") == "SABnzbd"
    assert downloads_routes.download_client_type_label("airdcpp") == "AirDC++"
    assert downloads_routes.download_client_type_label("custom_client") == "Custom Client"
    assert downloads_routes.normalize_download_queue_client_state(" Repairing ") == "Repairing"
    assert downloads_routes.normalize_download_queue_client_state("   ") is None
    assert downloads_routes.download_queue_client_state_token("Loading PARs") == "loadingpars"
    assert downloads_routes.is_download_queue_pollable_state(DownloadState.FINALIZING) is True
    assert downloads_routes.is_download_queue_pollable_state(DownloadState.COMPLETED) is False
    assert downloads_routes.is_download_queue_finalization_state("Loading PARs") is True
    assert downloads_routes.is_download_queue_finalization_state(None) is False
    assert len(downloads_routes.get_download_history_filters("cancelled", "sabnzbd")) == 3
    assert len(downloads_routes.get_download_history_filters("failed", None)) == 4


def test_manual_torznab_resolver_stage_is_visible_for_queued_torrent() -> None:
    download = DownloadHistory(
        issue_id=1,
        title="Ubuntu fixture",
        download_url="https://indexer.example/api?t=get&id=1",
        download_client=DownloadClientType.QBITTORRENT,
        state=DownloadState.QUEUED,
    )
    progress = SimpleNamespace(
        progress=0.0,
        speed_bytes=None,
        eta_seconds=None,
        client_state="Trying Byparr (resolver 2 of 3)",
        is_indeterminate=True,
    )

    row = downloads_routes.build_download_queue_row_view(download, progress, None)

    assert row.primary_phase == "Trying Byparr (resolver 2 of 3)"


def test_normalized_downloading_state_overrides_stale_queued_client_substate() -> None:
    download = DownloadHistory(
        issue_id=1,
        title="AirDC++ fixture",
        download_url="airdcpp://intent/ui-state",
        download_client=DownloadClientType.AIRDCPP,
        state=DownloadState.DOWNLOADING,
    )
    progress = SimpleNamespace(
        progress=0.42,
        speed_bytes=1_500_000,
        eta_seconds=None,
        client_state="Queued",
        is_indeterminate=False,
    )

    row = downloads_routes.build_download_queue_row_view(download, progress, None)

    assert row.primary_phase == "Downloading"
    assert row.progress_label == "42%"
    assert row.speed_bytes == 1_500_000


@pytest.mark.asyncio
async def test_download_queue_counts_and_context_rollups(
    configured_downloads_routes: RecordingTemplates,
    db_session,
) -> None:
    issue = await _seed_download_base(db_session)
    queue_items = [
        DownloadHistory(
            issue_id=issue.id,
            title="Downloading",
            download_url="https://example.test/downloading",
            download_client=DownloadClientType.SABNZBD,
            state=DownloadState.DOWNLOADING,
            external_id="download-1",
            file_size=100,
        ),
        DownloadHistory(
            issue_id=issue.id,
            title="Queued",
            download_url="https://example.test/queued",
            download_client=DownloadClientType.QBITTORRENT,
            state=DownloadState.QUEUED,
        ),
        DownloadHistory(
            issue_id=issue.id,
            title="Paused",
            download_url="https://example.test/paused",
            download_client=DownloadClientType.DELUGE,
            state=DownloadState.PAUSED,
        ),
    ]
    db_session.add_all(queue_items)
    await db_session.commit()

    context = await downloads_routes.load_download_queue_context(db_session)

    assert await downloads_routes.get_download_queue_count(db_session) == 3
    assert context["queue_count"] == 3
    assert context["active_count"] == 1
    assert context["queued_count"] == 1
    assert context["paused_count"] == 1
    assert context["waiting_count"] == 2
    assert context["combined_speed_bytes"] == 100
    assert next(row.display_title for row in context["queue_rows"]).startswith("Renamed")


@pytest.mark.asyncio
async def test_download_history_context_filters_counts_clients_and_paginates(
    configured_downloads_routes: RecordingTemplates,
    db_session,
) -> None:
    issue = await _seed_download_base(db_session)
    db_session.add_all(
        [
            DownloadClientConfig(
                name="Primary SAB",
                client_type=DownloadClientType.SABNZBD,
                url="http://sab",
                priority=1,
            ),
            DownloadClientConfig(
                name="Backup SAB",
                client_type=DownloadClientType.SABNZBD,
                url="http://sab2",
                priority=2,
            ),
        ]
    )
    db_session.add_all(
        [
            DownloadHistory(
                issue_id=issue.id,
                title="Batman Complete",
                download_url="https://example.test/completed",
                download_client=DownloadClientType.SABNZBD,
                state=DownloadState.COMPLETED,
                completed_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            DownloadHistory(
                issue_id=issue.id,
                title="Batman Failed",
                download_url="https://example.test/failed",
                download_client=DownloadClientType.SABNZBD,
                state=DownloadState.FAILED,
                error_message="Indexer failed",
            ),
            DownloadHistory(
                issue_id=issue.id,
                title="Batman Cancelled",
                download_url="https://example.test/cancelled",
                download_client=DownloadClientType.SABNZBD,
                state=DownloadState.FAILED,
                error_message="Cancelled by user",
            ),
            DownloadHistory(
                issue_id=issue.id,
                title="Batman Processing Failure",
                download_url="https://example.test/post-processing",
                download_client=DownloadClientType.SABNZBD,
                state=DownloadState.FAILED,
                downloaded_path="/downloads/processing.cbz",
                error_message="Archive failed",
            ),
        ]
    )
    await db_session.commit()

    context = await downloads_routes.load_download_history_context(
        db_session,
        status=None,
        client="transmission",
        search_query=" Batman ",
        page=99,
        sort="status",
    )

    assert await downloads_routes.get_download_history_count(db_session) == 3
    assert context["history_total"] == 0
    assert context["history_completed_count"] == 0
    assert context["history_failed_count"] == 0
    assert context["history_cancelled_count"] == 0
    assert context["history_pages"] == 1
    assert context["page"] == 1
    assert context["client_filter"] == "transmission"
    assert context["search_query"] == "Batman"
    assert context["sort"] == "status"
    assert context["client_options"] == [
        ("", "All Clients"),
        ("sabnzbd", "Primary SAB (SABnzbd)"),
        ("transmission", "Transmission"),
    ]


@pytest.mark.asyncio
async def test_download_routes_select_expected_templates(
    configured_downloads_routes: RecordingTemplates,
    monkeypatch: pytest.MonkeyPatch,
    db_session,
    route_request: SimpleNamespace,
) -> None:
    queue_ctx = {
        "queue_items": [],
        "queue_rows": [],
        "active_rows": [],
        "waiting_rows": [],
        "queue_count": 0,
        "active_count": 0,
        "queued_count": 0,
        "paused_count": 0,
        "waiting_count": 0,
        "combined_speed_bytes": 0,
        "progress_map": {},
        "renamed_names": {},
    }
    history_ctx = {
        "history_items": [],
        "history_total": 1,
        "history_completed_count": 1,
        "history_failed_count": 0,
        "history_cancelled_count": 0,
        "history_pages": 1,
        "page": 1,
        "status_filter": "",
        "client_filter": "",
        "search_query": "",
        "client_options": [("", "All Clients")],
        "sort": "-updated_at",
    }
    captured_history_args: list[dict[str, object]] = []

    async def _queue(_session: object) -> dict[str, object]:
        return queue_ctx

    async def _history(_session: object, **kwargs: object) -> dict[str, object]:
        captured_history_args.append(kwargs)
        return history_ctx

    monkeypatch.setattr(downloads_routes, "load_download_queue_context", _queue)
    monkeypatch.setattr(downloads_routes, "load_download_history_context", _history)

    full = await downloads_routes.downloads(
        route_request,
        user=SimpleNamespace(username="admin"),
        session=db_session,
        tab="weird",
        status=None,
        client=None,
        search="",
        sort="-updated_at",
        page=1,
    )
    hx_bundle = await downloads_routes.downloads(
        SimpleNamespace(headers={"HX-Request": "true"}, cookies={}, state=SimpleNamespace()),
        user=SimpleNamespace(username="admin"),
        session=db_session,
        tab="history",
        status="failed",
        client="sabnzbd",
        search="batman",
        sort="status",
        page=2,
    )
    hx_results = await downloads_routes.downloads(
        SimpleNamespace(
            headers={"HX-Request": "true", "HX-Target": "downloads-history-results"},
            cookies={},
            state=SimpleNamespace(),
        ),
        user=SimpleNamespace(username="admin"),
        session=db_session,
        tab="history",
        status="failed",
        client="sabnzbd",
        search="batman",
        sort="status",
        page=3,
    )

    assert full.template_name == "pages/downloads.html"
    assert full.context["tab"] == "queue"
    assert hx_bundle.template_name == "partials/downloads_content_bundle.html"
    assert hx_results.template_name == "partials/download_history_results_bundle.html"
    assert captured_history_args[0]["status"] == "failed"
    assert captured_history_args[1]["page"] == 3


@pytest.mark.asyncio
async def test_download_htmx_queue_history_and_error_detail_routes(
    configured_downloads_routes: RecordingTemplates,
    monkeypatch: pytest.MonkeyPatch,
    db_session,
    route_request: SimpleNamespace,
) -> None:
    queue_ctx = {
        "queue_items": [],
        "queue_rows": [],
        "active_rows": [],
        "waiting_rows": [],
        "queue_count": 0,
        "active_count": 0,
        "queued_count": 0,
        "paused_count": 0,
        "waiting_count": 0,
        "combined_speed_bytes": 0,
        "progress_map": {},
        "renamed_names": {},
    }
    history_ctx = {
        "history_items": [],
        "history_total": 0,
        "history_completed_count": 0,
        "history_failed_count": 0,
        "history_cancelled_count": 0,
        "history_pages": 1,
        "page": 1,
        "status_filter": "",
        "client_filter": "",
        "search_query": "",
        "client_options": [("", "All Clients")],
        "sort": "-updated_at",
    }

    async def _queue(_session: object) -> dict[str, object]:
        return queue_ctx

    async def _history(_session: object, **_kwargs: object) -> dict[str, object]:
        return history_ctx

    monkeypatch.setattr(downloads_routes, "load_download_queue_context", _queue)
    monkeypatch.setattr(downloads_routes, "load_download_history_context", _history)

    queue = await downloads_routes.htmx_download_queue(
        route_request,
        user=SimpleNamespace(username="admin"),
        session=db_session,
    )
    history = await downloads_routes.htmx_download_history(
        route_request,
        user=SimpleNamespace(username="admin"),
        session=db_session,
        status=None,
        client=None,
        search="",
        sort="-updated_at",
        page=1,
    )

    assert queue.template_name == "partials/download_queue_bundle.html"
    assert queue.headers == {"Cache-Control": "no-store"}
    assert history.template_name == "partials/download_history_results_bundle.html"
    assert history.context["tab"] == "history"

    issue = await _seed_download_base(db_session)
    failed = DownloadHistory(
        issue_id=issue.id,
        title="Batman Failed",
        download_url="https://example.test/failed",
        download_client=DownloadClientType.SABNZBD,
        state=DownloadState.FAILED,
        error_message="Indexer failed",
    )
    cancelled = DownloadHistory(
        issue_id=issue.id,
        title="Batman Cancelled",
        download_url="https://example.test/cancelled",
        download_client=DownloadClientType.SABNZBD,
        state=DownloadState.FAILED,
        error_message="Cancelled by user",
    )
    db_session.add_all([failed, cancelled])
    await db_session.commit()
    await db_session.refresh(failed)
    await db_session.refresh(cancelled)

    detail = await downloads_routes.htmx_download_history_error_detail(
        route_request,
        download_id=failed.id,
        user=SimpleNamespace(username="admin"),
        session=db_session,
    )
    assert detail.template_name == "partials/download_history_error_detail.html"
    assert detail.context["dl"].id == failed.id

    with pytest.raises(HTTPException) as exc:
        await downloads_routes.htmx_download_history_error_detail(
            route_request,
            download_id=cancelled.id,
            user=SimpleNamespace(username="admin"),
            session=db_session,
        )
    assert exc.value.status_code == 404

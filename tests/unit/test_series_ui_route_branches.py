"""Direct branch coverage for split series UI route helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.base import SeriesSearchResult
from pullbox.ui import series_routes

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class RecordingTemplates:
    """Tiny template recorder so route tests can assert context directly."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def TemplateResponse(  # noqa: N802 - mirrors Starlette/Jinja2 template API.
        self,
        _request: object,
        template_name: str,
        context: dict[str, object],
    ) -> SimpleNamespace:
        self.calls.append((template_name, context))
        return SimpleNamespace(template_name=template_name, context=context, status_code=200)


def _request(
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        headers=headers or {},
        cookies=cookies or {},
        state=SimpleNamespace(),
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin")


@pytest.mark.parametrize("column", [Series.status, Series.series_type])
def test_series_enum_sort_expression_casts_before_lowering(column) -> None:  # type: ignore[no-untyped-def]
    statement = select(Series.id).order_by(series_routes._lower_enum_sort(column))

    compiled = str(statement.compile(dialect=postgresql.dialect()))

    assert "lower(CAST(" in compiled
    assert f"lower(series.{column.key})" not in compiled


async def _config_values(
    _session: AsyncSession,
    keys: tuple[str, ...] | list[str],
) -> dict[str, str]:
    values = {
        "series_folder_template": "{Publisher} - {Series} ({Year})",
        "replace_illegal_characters": "true",
        "colon_replacement": "dash",
    }
    return {key: values[key] for key in keys if key in values}


@pytest.fixture
def configured_series_routes(monkeypatch: pytest.MonkeyPatch) -> RecordingTemplates:
    templates = RecordingTemplates()
    monkeypatch.setattr(series_routes, "_get_templates", lambda: templates)
    monkeypatch.setattr(
        series_routes,
        "_build_context",
        lambda request, user=None, **kwargs: {"request": request, "user": user, **kwargs},
    )
    monkeypatch.setattr(series_routes, "_load_system_config_values", _config_values)
    return templates


@pytest.mark.asyncio
async def test_series_route_runtime_seams_and_helpers_require_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(series_routes, "_get_templates", None)
    monkeypatch.setattr(series_routes, "_build_context", None)
    monkeypatch.setattr(series_routes, "_load_system_config_values", None)

    with pytest.raises(RuntimeError, match="templates"):
        series_routes._templates()
    with pytest.raises(RuntimeError, match="context builder"):
        series_routes._ctx(_request())
    with pytest.raises(RuntimeError, match="system config loader"):
        await series_routes._system_config_values(SimpleNamespace(), ["instance_name"])  # type: ignore[arg-type]

    assert series_routes.normalize_series_per_page(0) == 25
    assert series_routes.normalize_series_per_page(9999) == 500
    grid_request = _request(cookies={"series_view": "grid"})
    assert series_routes.resolve_series_view(grid_request, None) == "grid"
    assert series_routes.resolve_series_view(_request(), "list") == "list"
    unknown_view_request = _request(cookies={"series_view": "cards"})
    assert series_routes.resolve_series_view(unknown_view_request, None) == "list"
    assert series_routes.series_type_code("omnibus") == "OMNI"
    assert series_routes.series_type_code("custom") == "CUS"


@pytest.mark.asyncio
async def test_series_list_direct_route_builds_full_and_htmx_context(
    db_session: AsyncSession,
    configured_series_routes: RecordingTemplates,
) -> None:
    publisher = Publisher(name="DC Comics")
    root = LibraryRoot(name="Main", path="/comics", enabled=True)
    db_session.add_all([publisher, root])
    await db_session.flush()

    series = Series(
        title="Alpha Flight",
        sort_title="alpha flight",
        year_start=2025,
        status=SeriesStatus.UNKNOWN,
        monitored=True,
        issue_count=2,
        publisher_id=publisher.id,
        library_root_id=root.id,
        series_type=SeriesType.OMNIBUS,
        cover_url="https://example.test/alpha.jpg",
    )
    db_session.add(series)
    await db_session.flush()

    owned = Issue(
        series_id=series.id,
        issue_number=1,
        status=IssueStatus.OWNED,
        release_date=date.today(),
    )
    wanted = Issue(
        series_id=series.id,
        issue_number=2,
        status=IssueStatus.WANTED,
        release_date=date.today(),
    )
    db_session.add_all([owned, wanted])
    await db_session.flush()
    db_session.add(
        LibraryFile(
            issue_id=owned.id,
            library_root_id=root.id,
            file_path="/comics/Alpha Flight 001.cbz",
            file_name="Alpha Flight 001.cbz",
            file_size=1234,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
        )
    )
    await db_session.flush()

    full = await series_routes.series_list(
        _request(cookies={"series_view": "grid"}),
        _user(),
        db_session,
        q="Alpha",
        status=None,
        monitored="",
        sort="-year",
        page=99,
        per_page=0,
        partial="ignored",
        view_mode=None,
    )

    assert full.template_name == "pages/series_list.html"
    full_context = full.context
    assert full_context["active_view"] == "grid"
    assert full_context["page"] == 1
    assert full_context["per_page"] == 25
    assert full_context["filter_query"] == "q=Alpha&sort=-year"
    assert full_context["registry_metrics"]["owned_issue_count"] == 1  # type: ignore[index]
    assert full_context["registry_metrics"]["wanted_issue_count"] == 1  # type: ignore[index]
    assert full_context["registry_metrics"]["library_size_bytes"] == 1234  # type: ignore[index]
    assert series.status == SeriesStatus.CONTINUING
    assert full_context["series_data"][0]["type_code"] == "OMNI"  # type: ignore[index]
    assert full_context["series_data"][0]["cover_loading"] == "eager"  # type: ignore[index]

    htmx = await series_routes.series_list(
        _request(headers={"HX-Request": "true"}),
        _user(),
        db_session,
        q=None,
        status=None,
        monitored=None,
        sort="title",
        page=1,
        per_page=25,
        partial=None,
        view_mode="list",
    )

    assert htmx.template_name == "partials/series_results_bundle.html"
    assert configured_series_routes.calls[-1][0] == "partials/series_results_bundle.html"


@pytest.mark.asyncio
async def test_series_selection_ids_direct_route_applies_filters(
    db_session: AsyncSession,
) -> None:
    db_session.add_all(
        [
            Series(
                title="Selected One",
                sort_title="selected one",
                status=SeriesStatus.CONTINUING,
                monitored=True,
            ),
            Series(
                title="Selected Two",
                sort_title="selected two",
                status=SeriesStatus.ENDED,
                monitored=False,
            ),
        ]
    )
    await db_session.flush()

    response = await series_routes.series_selection_ids(
        _user(),
        db_session,
        q="Selected",
        status=SeriesStatus.CONTINUING.value,
        monitored="true",
    )

    assert response.status_code == 200
    assert response.body == b'{"ids":[1],"total":1}'


@pytest.mark.asyncio
async def test_add_series_context_covers_empty_preview_success_and_failure(
    db_session: AsyncSession,
    configured_series_routes: RecordingTemplates,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del configured_series_routes
    db_session.add(LibraryRoot(name="Main", path="/comics", enabled=True))
    await db_session.flush()

    too_short = await series_routes.load_add_series_search_context(db_session, "x", None)
    assert too_short["search_results"] == []
    assert too_short["library_roots_count"] == 1

    preview_not_ready = await series_routes.load_add_series_search_context(
        db_session,
        "the",
        "relevance",
        search_mode="preview",
    )
    assert preview_not_ready["search_results"] == []
    assert preview_not_ready["add_series_full_search_url"] == ""

    preview_result = SeriesSearchResult(
        provider_id="12345",
        title="The Brave and the Bold",
        year_start=2026,
        publisher="DC Comics",
        issue_count=3,
        status=None,
        cover_url=None,
        description=None,
    )
    full_result = SeriesSearchResult(
        provider_id="67890",
        title="Batman",
        year_start=2024,
        publisher="DC Comics",
        issue_count=12,
        status=None,
        cover_url=None,
        description=None,
    )
    get_key = AsyncMock(return_value="fake-key")
    page_search = AsyncMock(return_value=([preview_result], 1))
    global_search = AsyncMock(return_value=([full_result], 1))
    monkeypatch.setattr("pullbox.core.comicvine_key.get_comicvine_api_key", get_key)
    monkeypatch.setattr(
        "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_page",
        page_search,
    )
    monkeypatch.setattr(
        "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
        global_search,
    )

    preview = await series_routes.load_add_series_search_context(
        db_session,
        "The Brave",
        "not-a-sort",
        search_mode="preview",
    )
    assert preview["is_preview_search"] is True
    assert preview["add_series_sort"] == "relevance"
    assert preview["search_total_results"] == 1
    assert preview["search_shown_count"] == 1
    assert preview["search_results"][0]["title"] == "The Brave and the Bold"  # type: ignore[index]
    page_search.assert_awaited_once_with("The Brave", None, limit=20)

    full = await series_routes.load_add_series_search_context(
        db_session,
        "Batman 2024",
        "-year_start",
        page=99,
    )
    assert full["is_preview_search"] is False
    assert full["search_page"] == 1
    assert full["search_results"][0]["title"] == "Batman"  # type: ignore[index]
    global_search.assert_awaited_once()

    get_key.side_effect = RuntimeError("boom")
    failed = await series_routes.load_add_series_search_context(db_session, "Batman", "relevance")
    assert failed["search_error"] == "ComicVine search failed. Check your API key in settings."


@pytest.mark.asyncio
async def test_add_series_page_and_htmx_search_routes_render_expected_templates(
    db_session: AsyncSession,
    configured_series_routes: RecordingTemplates,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.add(LibraryRoot(name="Main", path="/comics", enabled=True))
    await db_session.flush()
    monkeypatch.setattr(series_routes, "get_request_session_factory", lambda _request: None)

    page = await series_routes.add_series_page(
        _request(),
        _user(),
        db_session,
        q=None,
        sort="relevance",
        page=1,
        search_mode=None,
    )
    assert page.template_name == "pages/add_series.html"
    assert page.context["roots"][0].name == "Main"  # type: ignore[index]
    assert "add_series_sort_options" in page.context

    htmx_page = await series_routes.add_series_page(
        _request(headers={"HX-Request": "true"}),
        _user(),
        db_session,
        q=None,
        sort="relevance",
        page=1,
        search_mode=None,
    )
    assert htmx_page.template_name == "partials/add_series_results_bundle.html"

    htmx_search = await series_routes.htmx_search_series(
        _request(),
        _user(),
        db_session,
        q="",
        sort="relevance",
        page=1,
        search_mode=None,
    )
    assert htmx_search.template_name == "partials/add_series_results_bundle.html"
    assert configured_series_routes.calls[-1][1]["search_query"] == ""

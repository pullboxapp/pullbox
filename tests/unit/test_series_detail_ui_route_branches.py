"""Direct branch coverage for split series/issue detail UI routes."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.publisher import Publisher
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import Series, SeriesStatus
from pullbox.providers.base import IssueMetadata
from pullbox.services.search_targets import IssueSearchTarget
from pullbox.ui import series_detail_routes

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


class FormRequest(SimpleNamespace):
    async def form(self) -> dict[str, str]:
        return getattr(self, "form_data", {})

    async def json(self) -> dict[str, object]:
        return getattr(self, "json_data", {})


def _request(
    *,
    form_data: dict[str, str] | None = None,
    json_data: dict[str, object] | None = None,
) -> FormRequest:
    return FormRequest(
        headers={},
        cookies={},
        state=SimpleNamespace(),
        form_data=form_data or {},
        json_data=json_data or {},
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=1, username="admin")


@pytest.fixture
def configured_detail_routes(monkeypatch: pytest.MonkeyPatch) -> RecordingTemplates:
    templates = RecordingTemplates()
    monkeypatch.setattr(series_detail_routes, "_get_templates", lambda: templates)
    monkeypatch.setattr(
        series_detail_routes,
        "_build_context",
        lambda request, user=None, **kwargs: {"request": request, "user": user, **kwargs},
    )
    return templates


async def _seed_detail_rows(session: AsyncSession) -> tuple[Series, Issue, Issue, Issue]:
    publisher = Publisher(name="DC Comics")
    root = LibraryRoot(name="Main", path="/comics", enabled=True)
    session.add_all([publisher, root])
    await session.flush()

    series = Series(
        title="Batman",
        sort_title="batman",
        year_start=2025,
        status=SeriesStatus.CONTINUING,
        monitored=True,
        issue_count=3,
        publisher_id=publisher.id,
        library_root_id=root.id,
        alternate_names=["The Bat"],
    )
    session.add(series)
    await session.flush()

    owned = Issue(
        series_id=series.id,
        issue_number=1,
        title="Owned",
        status=IssueStatus.OWNED,
        release_date=date(2025, 1, 1),
        issue_type=IssueType.ISSUE,
    )
    wanted = Issue(
        series_id=series.id,
        issue_number=2,
        title="Wanted",
        status=IssueStatus.WANTED,
        release_date=date(2025, 2, 1),
        issue_type=IssueType.ISSUE,
    )
    skipped = Issue(
        series_id=series.id,
        issue_number=3,
        title="Skipped",
        status=IssueStatus.SKIPPED,
        release_date=date(2025, 3, 1),
        issue_type=IssueType.ISSUE,
    )
    session.add_all([owned, wanted, skipped])
    await session.flush()
    session.add(
        LibraryFile(
            issue_id=owned.id,
            library_root_id=root.id,
            file_path="/comics/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=100,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
        )
    )
    await session.flush()
    return series, owned, wanted, skipped


@pytest.mark.asyncio
async def test_detail_route_runtime_seams_require_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(series_detail_routes, "_get_templates", None)
    monkeypatch.setattr(series_detail_routes, "_build_context", None)

    with pytest.raises(RuntimeError, match="templates"):
        series_detail_routes._templates()
    with pytest.raises(RuntimeError, match="context builder"):
        series_detail_routes._ctx(_request())


@pytest.mark.asyncio
async def test_load_series_issues_context_counts_filters_and_sorts(
    db_session: AsyncSession,
) -> None:
    series, _owned, _wanted, _skipped = await _seed_detail_rows(db_session)

    filtered = await series_detail_routes.load_series_issues_context(
        db_session,
        series.id,
        IssueStatus.OWNED.value,
        page=99,
        user_id=1,
        sort="title",
    )

    assert filtered["owned_count"] == 1
    assert filtered["wanted_count"] == 1
    assert filtered["filtered_total"] == 1
    assert filtered["page"] == 1
    assert filtered["issue_status"] == IssueStatus.OWNED.value
    assert filtered["issues"][0].title == "Owned"  # type: ignore[index]

    all_issues = await series_detail_routes.load_series_issues_context(
        db_session,
        series.id,
        None,
        page=1,
        user_id=1,
        sort="-release_date",
    )
    assert [issue.title for issue in all_issues["issues"]] == ["Skipped", "Wanted", "Owned"]


@pytest.mark.asyncio
async def test_series_detail_route_redirects_or_renders_context(
    db_session: AsyncSession,
    configured_detail_routes: RecordingTemplates,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series, _owned, _wanted, _skipped = await _seed_detail_rows(db_session)
    monkeypatch.setattr(
        series_detail_routes.SeriesService,
        "build_delete_context",
        AsyncMock(return_value=SimpleNamespace(linked_file_count=1)),
    )

    missing = await series_detail_routes.series_detail(
        _request(),
        99999,
        _user(),
        db_session,
    )
    assert missing.status_code == 302
    assert missing.headers["location"] == "/series"

    rendered = await series_detail_routes.series_detail(
        _request(),
        series.id,
        _user(),
        db_session,
        issue_status=IssueStatus.WANTED.value,
        page=1,
        issue_sort="-issue_number",
    )
    assert rendered.template_name == "pages/series_detail.html"
    assert rendered.context["series"].id == series.id
    assert rendered.context["file_count"] == 1
    assert rendered.context["delete_file_count"] == 1
    assert rendered.context["filtered_total"] == 1
    assert configured_detail_routes.calls[-1][0] == "pages/series_detail.html"


@pytest.mark.asyncio
async def test_issue_detail_route_enriches_missing_metadata_and_handles_missing(
    db_session: AsyncSession,
    configured_detail_routes: RecordingTemplates,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del configured_detail_routes
    _series, issue, _wanted, _skipped = await _seed_detail_rows(db_session)
    issue.comicvine_id = 111
    issue.description = None
    await db_session.flush()

    missing = await series_detail_routes.issue_detail(_request(), 99999, _user(), db_session)
    assert missing.status_code == 302
    assert missing.headers["location"] == "/series"

    meta = IssueMetadata(
        provider_id="111",
        series_provider_id="222",
        issue_number=1,
        title="Owned",
        description="Fresh description",
        release_date="2025-01-01",
        store_date="2024-12-18",
        cover_url="https://example.test/cover.jpg",
        page_count=22,
        comicvine_url="https://comicvine.gamespot.com/issue/4000-111/",
    )
    provider = SimpleNamespace(get_issue=AsyncMock(return_value=meta))
    monkeypatch.setattr(
        "pullbox.core.comicvine_key.get_comicvine_api_key",
        AsyncMock(return_value="fake-key"),
    )
    monkeypatch.setattr(
        "pullbox.providers.metadata.comicvine.ComicVineProvider",
        lambda api_key: provider if api_key == "fake-key" else None,
    )
    monkeypatch.setattr(
        series_detail_routes,
        "wrap_comicvine_provider_for_ui_cache",
        lambda wrapped_provider, _request: wrapped_provider,
    )

    rendered = await series_detail_routes.issue_detail(_request(), issue.id, _user(), db_session)

    assert rendered.template_name == "pages/issue_detail.html"
    assert rendered.context["issue"].description == "Fresh description"
    assert (
        rendered.context["issue"].comicvine_url == "https://comicvine.gamespot.com/issue/4000-111/"
    )
    assert rendered.context["issue"].cover_url == "https://example.test/cover.jpg"
    assert rendered.context["issue"].store_date == date(2024, 12, 18)
    provider.get_issue.assert_awaited_once_with("111")


@pytest.mark.asyncio
async def test_issue_status_toggle_branches(
    db_session: AsyncSession,
    configured_detail_routes: RecordingTemplates,
) -> None:
    _series, _owned, wanted, skipped = await _seed_detail_rows(db_session)

    missing = await series_detail_routes.htmx_toggle_issue_status(
        _request(),
        99999,
        _user(),
        db_session,
    )
    assert missing.status_code == 404

    toggled_wanted = await series_detail_routes.htmx_toggle_issue_status(
        _request(),
        wanted.id,
        _user(),
        db_session,
    )
    assert toggled_wanted.template_name == "partials/issue_row.html"
    assert wanted.status == IssueStatus.SKIPPED
    assert wanted.manual_skip is True

    toggled_skipped = await series_detail_routes.htmx_toggle_issue_status(
        _request(),
        skipped.id,
        _user(),
        db_session,
    )
    assert toggled_skipped.template_name == "partials/issue_row.html"
    assert skipped.status == IssueStatus.WANTED
    assert skipped.manual_skip is False
    assert configured_detail_routes.calls[-1][0] == "partials/issue_row.html"


@pytest.mark.asyncio
async def test_issue_search_results_render_no_runtime_and_runtime_branches(
    db_session: AsyncSession,
    configured_detail_routes: RecordingTemplates,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _series, issue, _wanted, _skipped = await _seed_detail_rows(db_session)
    target = IssueSearchTarget(
        issue_id=issue.id,
        series_id=issue.series_id,
        series_title="Batman",
        issue_number=1,
        issue_type=IssueType.ISSUE,
    )
    issue_ctx = SimpleNamespace(id=issue.id, issue_number=issue.issue_number)
    no_runtime_bundle = SimpleNamespace(
        issue=issue_ctx,
        target=target,
        runtime=None,
        outcome=None,
        search_time_ms=7,
        matched_items=[],
        rejected_items=[],
    )
    matched = SimpleNamespace(model_dump=lambda: {"title": "Batman 001"})
    rejected = SimpleNamespace(model_dump=lambda: {"title": "Wrong Batman"})
    runtime_bundle = SimpleNamespace(
        issue=issue_ctx,
        target=target,
        runtime=object(),
        outcome=None,
        search_time_ms=11,
        matched_items=[matched],
        rejected_items=[rejected],
    )
    run_search = AsyncMock(side_effect=[no_runtime_bundle, runtime_bundle])
    monkeypatch.setattr("pullbox.api.v1.issues._run_issue_search", run_search)
    monkeypatch.setattr(
        "pullbox.api.v1.issues._build_issue_search_log",
        lambda _bundle: SearchLog(
            issue_id=issue.id,
            series_title="Batman",
            issue_number=1,
            search_type=SearchType.MANUAL,
            results_found=1,
        ),
    )

    missing = await series_detail_routes.htmx_issue_search_results(
        _request(),
        99999,
        _user(),
        db_session,
    )
    assert missing.status_code == 404

    no_runtime = await series_detail_routes.htmx_issue_search_results(
        _request(),
        issue.id,
        _user(),
        db_session,
    )
    assert no_runtime.template_name == "partials/issue_search_results.html"
    assert no_runtime.context["matched"] == []
    assert no_runtime.context["search_time_ms"] == 7

    runtime = await series_detail_routes.htmx_issue_search_results(
        _request(),
        issue.id,
        _user(),
        db_session,
    )
    assert runtime.context["matched"] == [{"title": "Batman 001"}]
    assert runtime.context["rejected"] == [{"title": "Wrong Batman"}]
    assert runtime.context["search_log_id"] > 0
    assert configured_detail_routes.calls[-1][0] == "partials/issue_search_results.html"


@pytest.mark.asyncio
async def test_delete_and_alternate_name_routes_cover_success_and_missing_branches(
    db_session: AsyncSession,
    configured_detail_routes: RecordingTemplates,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series, _owned, _wanted, _skipped = await _seed_detail_rows(db_session)
    delete = AsyncMock()
    monkeypatch.setattr("pullbox.services.series_service.SeriesService.delete", delete)

    deleted = await series_detail_routes.htmx_delete_series(
        _request(json_data={"delete_files": True, "delete_folder": True}),
        series.id,
        _user(),
        db_session,
    )
    assert deleted.status_code == 302
    delete.assert_awaited_once_with(
        db_session,
        series.id,
        delete_files=True,
        delete_folder=True,
    )

    blank = await series_detail_routes.htmx_add_alternate_name(
        _request(form_data={"name": "   "}),
        series.id,
        _user(),
        db_session,
    )
    assert blank.status_code == 400

    missing_add = await series_detail_routes.htmx_add_alternate_name(
        _request(form_data={"name": "Detective"}),
        99999,
        _user(),
        db_session,
    )
    assert missing_add.status_code == 404

    added = await series_detail_routes.htmx_add_alternate_name(
        _request(form_data={"name": "Detective"}),
        series.id,
        _user(),
        db_session,
    )
    assert added.template_name == "partials/series_detail_alternate_names_list.html"
    assert "Detective" in series.alternate_names

    missing_remove = await series_detail_routes.htmx_remove_alternate_name(
        _request(),
        99999,
        "Detective",
        _user(),
        db_session,
    )
    assert missing_remove.status_code == 404

    removed = await series_detail_routes.htmx_remove_alternate_name(
        _request(),
        series.id,
        "Detective",
        _user(),
        db_session,
    )
    assert removed.template_name == "partials/series_detail_alternate_names_list.html"
    assert "Detective" not in series.alternate_names
    assert (
        configured_detail_routes.calls[-1][0] == "partials/series_detail_alternate_names_list.html"
    )


@pytest.mark.asyncio
async def test_series_issues_partial_route_handles_missing_and_success(
    db_session: AsyncSession,
    configured_detail_routes: RecordingTemplates,
) -> None:
    series, _owned, _wanted, _skipped = await _seed_detail_rows(db_session)

    missing = await series_detail_routes.htmx_series_issues(
        _request(),
        99999,
        _user(),
        db_session,
    )
    assert missing.status_code == 404

    rendered = await series_detail_routes.htmx_series_issues(
        _request(),
        series.id,
        _user(),
        db_session,
        issue_status=IssueStatus.WANTED.value,
        page=1,
        issue_sort="status",
    )
    assert rendered.template_name == "partials/series_issues_bundle.html"
    assert rendered.context["series"].id == series.id
    assert rendered.context["filtered_total"] == 1
    assert configured_detail_routes.calls[-1][0] == "partials/series_issues_bundle.html"

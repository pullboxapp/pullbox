"""Direct branch coverage for the split What's New UI route."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from pullbox.ui import whats_new_routes


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


class QueryParams:
    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def multi_items(self) -> list[tuple[str, str]]:
        return list(self._pairs)


def _request(
    *,
    headers: dict[str, str] | None = None,
    query_pairs: list[tuple[str, str]] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        headers=headers or {},
        cookies={},
        state=SimpleNamespace(),
        query_params=QueryParams(query_pairs or []),
        url=SimpleNamespace(path="/whats-new"),
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(username="admin")


def _release(
    title: str,
    *,
    issue_number: object = "1",
    publisher: str = "DC Comics",
    store_date: object = "2026-04-01",
    price: object = "4.99",
    pull_count: object = 10,
    rating: object = "8.5",
    variant_count: object = 0,
    series_id: object = 100,
) -> dict[str, Any]:
    return {
        "title": title,
        "display_title": title,
        "issue_number": issue_number,
        "store_date": store_date,
        "price": price,
        "community_rating": rating,
        "variant_count": variant_count,
        "community_counts": {"pull": pull_count},
        "publisher": {"name": publisher},
        "series": {"locg_series_id": series_id, "title": title.split("#")[0].strip()},
    }


def _row(payload: dict[str, Any], *, store_date: date | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1 if store_date is not None else 2,
        payload=payload,
        store_date=store_date,
        publisher=None,
        fetched_at=datetime.now(UTC),
        last_successful_refresh_at=datetime.now(UTC),
    )


class FakeWhatsNewService:
    def __init__(self) -> None:
        self.current = _row(
            {
                "store_date": "2026-04-01",
                "issues": [
                    _release("Batman #1", issue_number="1", pull_count=12),
                    _release("Batman #1 Cover B", issue_number="1", pull_count=3),
                    _release("Superman #2", issue_number="2", publisher="DC Comics"),
                    _release("Saga #3", issue_number="3", publisher="Image Comics"),
                    "bad row",
                ],
            },
            store_date=date(2026, 4, 1),
        )
        self.upcoming = _row(
            {
                "weeks": [
                    {
                        "store_date": "2026-04-08",
                        "count": "2",
                        "variant_rows_hidden": "1",
                        "issues": [
                            _release(
                                "Wonder Woman #4",
                                issue_number="4",
                                publisher="DC Comics",
                                store_date="2026-04-08",
                            ),
                            {"title": "Not enough data"},
                        ],
                    },
                    {
                        "store_date": "2026-04-15",
                        "count": 1,
                        "issues": [
                            _release(
                                "Transformers #5",
                                issue_number="5",
                                publisher="Image Comics",
                                store_date="2026-04-15",
                                series_id=200,
                            )
                        ],
                    },
                    {"store_date": "not-a-date", "issues": []},
                    "bad week",
                ],
                "lookahead_weeks": 8,
            }
        )
        self.current_week_calls: list[date | None] = []

    async def get_latest_current_week(self, _session: object) -> SimpleNamespace:
        return self.current

    async def get_current_week(self, _session: object, store_date: date | None) -> SimpleNamespace:
        self.current_week_calls.append(store_date)
        return self.current

    async def get_upcoming(self, _session: object) -> SimpleNamespace:
        return self.upcoming

    def cache_status_label(self, _row: object) -> str:
        return "fresh"

    def is_stale(self, _row: object) -> bool:
        return False


@pytest.fixture
def configured_whats_new_routes(monkeypatch: pytest.MonkeyPatch) -> RecordingTemplates:
    templates = RecordingTemplates()
    monkeypatch.setattr(whats_new_routes, "_get_templates", lambda: templates)
    monkeypatch.setattr(
        whats_new_routes,
        "_build_context",
        lambda request, user=None, **kwargs: {"request": request, "user": user, **kwargs},
    )
    return templates


def test_whats_new_runtime_seams_and_edge_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(whats_new_routes, "_get_templates", None)
    monkeypatch.setattr(whats_new_routes, "_build_context", None)

    with pytest.raises(RuntimeError, match="templates"):
        whats_new_routes._templates()
    with pytest.raises(RuntimeError, match="context builder"):
        whats_new_routes._ctx(_request())

    assert whats_new_routes._active_window("upcoming", None, {"payload": {}}) == "upcoming"
    assert whats_new_routes._active_window("bad", None, {"payload": {}}) == "upcoming"
    assert whats_new_routes._active_window("current", {"payload": {}}, None) == "current"
    assert whats_new_routes._publisher_options(None, q="") == [("", "All Publishers")]
    assert (
        whats_new_routes._filtered_view_model(
            None,
            q="",
            publisher="",
            sort="release",
            page=1,
            per_page=25,
        )
        is None
    )
    assert whats_new_routes._payload_int(None, "total", 7) == 7
    assert whats_new_routes._payload_int({"payload": {"total": "bad"}}, "total", 7) == 7
    assert whats_new_routes._week_view("bad") == "bad"
    assert whats_new_routes._release_view("bad") == "bad"
    assert whats_new_routes._parse_float(True) is None
    assert whats_new_routes._parse_float("") is None
    assert whats_new_routes._parse_float("bad") is None
    assert whats_new_routes._parse_int(True) is None
    assert whats_new_routes._parse_int("") is None
    assert whats_new_routes._parse_int("bad") is None
    assert whats_new_routes._parse_date("bad") == "bad"


def test_whats_new_sort_and_grouping_helpers_cover_comparison_branches() -> None:
    releases = [
        _release("Beta #2", issue_number="2", publisher="Image Comics", pull_count=2),
        _release("Alpha #1", issue_number="1", publisher="DC Comics", pull_count=10),
        _release("Gamma #3", issue_number=None, publisher="", store_date=None, price=None),
    ]

    for sort in (
        "date",
        "-date",
        "issue",
        "-issue",
        "publisher",
        "-publisher",
        "pulls",
        "-pulls",
        "rating",
        "-rating",
        "price",
        "-price",
        "variants",
        "-variants",
        "release",
        "-release",
    ):
        assert len(whats_new_routes._sort_releases(list(releases), sort)) == 3

    grouped = whats_new_routes._release_list_view(
        [
            _release("Alpha #1 Cover B", issue_number="1", variant_count="bad"),
            _release("Alpha #1", issue_number="1", variant_count=0),
            {"display_title": "", "title": "", "store_date": "2026-04-01"},
        ]
    )
    assert grouped[0]["display_title"] == "Alpha #1"
    assert grouped[0]["variant_count"] == 1
    assert any(item.get("title") == "" for item in grouped)


@pytest.mark.asyncio
async def test_whats_new_page_direct_route_renders_current_and_upcoming_contexts(
    db_session: object,
    configured_whats_new_routes: RecordingTemplates,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeWhatsNewService()
    monkeypatch.setattr(whats_new_routes, "WhatsNewCacheService", lambda: service)

    current = await whats_new_routes.whats_new_page(
        _request(query_pairs=[("q", "batman"), ("publisher", "DC Comics"), ("page", "2")]),
        _user(),
        db_session,
        store_date=date(2026, 4, 1),
        q="batman",
        publisher="DC Comics",
        sort="not-real",
        window="current",
        release_week="",
        page=2,
        per_page=1,
    )

    assert current.template_name == "pages/whats_new.html"
    assert current.context["active_window"] == "current"
    assert current.context["sort"] == "release"
    assert current.context["publisher_filter"] == "DC Comics"
    assert current.context["page"] == 1
    assert current.context["total"] == 1
    assert current.context["pagination_base_url"] == "/whats-new?q=batman&publisher=DC+Comics"
    assert current.context["sort_base_url"] == "/whats-new?q=batman&publisher=DC+Comics"
    assert service.current_week_calls == [date(2026, 4, 1)]

    upcoming = await whats_new_routes.whats_new_page(
        _request(
            headers={"HX-Request": "true"},
            query_pairs=[
                ("window", "upcoming"),
                ("release_week", "2026-04-15"),
                ("publisher", "Image Comics"),
                ("sort", "-date"),
            ],
        ),
        _user(),
        db_session,
        store_date=None,
        q="",
        publisher="Image Comics",
        sort="-date",
        window="upcoming",
        release_week="2026-04-15",
        page=1,
        per_page=25,
    )

    assert upcoming.template_name == "partials/whats_new_results_bundle.html"
    assert upcoming.context["active_window"] == "upcoming"
    assert upcoming.context["publisher_filter"] == "Image Comics"
    assert upcoming.context["selected_upcoming_week"] == "2026-04-15"
    assert upcoming.context["upcoming_week_nav"]["position"] == 2  # type: ignore[index]
    assert upcoming.context["upcoming"]["payload"]["selected_week"] == "2026-04-15"  # type: ignore[index]
    assert configured_whats_new_routes.calls[-1][0] == "partials/whats_new_results_bundle.html"

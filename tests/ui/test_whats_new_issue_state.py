"""Release tables surface existing issue state without changing series actions."""

import re
from datetime import UTC, date, datetime
from html.parser import HTMLParser

import pytest

from pullbox.config import get_settings
from pullbox.core.metadata_identity import IdentityEvidenceKind, IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import Issue, IssueStatus, Series
from pullbox.models.metadata_identity import SeriesExternalIdentity
from pullbox.models.whats_new import WhatsNewCacheKind, WhatsNewReleaseCache
from pullbox.ui.whats_new_routes import _release_list_view
from tests.ui.test_whats_new_ui_routes import _issue_summary

pytest_plugins = ["conftest_security"]


class _ReleaseLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.nodes.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


@pytest.fixture
def actions_enabled(monkeypatch):
    monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "series_title",
    ["Absolute Flash", 'Flash" onfocus="alert(1)"><i data-label-x>&'],
    ids=["normal", "escaped"],
)
async def test_confirmed_release_links_issue_and_unresolved_row_stays_non_actionable(
    authenticated_client, sec_db, actions_enabled, series_title
):
    async with sec_db.begin() as session:
        series = Series(title=series_title, sort_title=series_title)
        session.add(series)
        await session.flush()
        identity = SeriesExternalIdentity(
            series_id=series.id,
            identity_namespace=IdentityNamespace.LOCG,
            external_id="_replaced",
            verification_state=IdentityVerificationState.VERIFIED,
            evidence_kind=IdentityEvidenceKind.USER_SELECTION,
        )
        source = _issue_summary()
        assert isinstance(source["series"], dict)
        source["series"]["title"] = series_title
        identity.external_id = str(source["series"]["locg_series_id"])
        issue = Issue(
            series_id=series.id,
            issue_number=1,
            store_date=date(2026, 3, 11),
            status=IssueStatus.WANTED,
        )
        session.add_all([identity, issue])
        unresolved = {
            **source,
            "locg_issue_id": 1514021,
            "issue_number": "2",
            "display_title": "Absolute Flash #2",
        }
        session.add(
            WhatsNewReleaseCache(
                cache_key="current-week:2026-03-11",
                cache_kind=WhatsNewCacheKind.CURRENT_WEEK,
                store_date=date(2026, 3, 11),
                payload={"store_date": "2026-03-11", "issues": [source, unresolved]},
                fetched_at=datetime.now(UTC),
                last_successful_refresh_at=datetime.now(UTC),
            )
        )
        await session.flush()
        issue_id = issue.id
    response = await authenticated_client.get("/whats-new")
    assert response.status_code == 200
    rows = re.findall(r'<tr\b[^>]*data-testid="whats-new-release-row".*?</tr>', response.text, re.S)
    linked = next(row for row in rows if "#1" in row)
    assert 'data-testid="whats-new-issue-state"' in linked, (
        "existing issue state is absent from the release table"
    )
    assert re.search(r'data-testid="whats-new-issue-state"[^>]*>\s*Missing\s*</span>', linked)
    assert f'href="/issues/{issue_id}"' in linked
    assert 'data-testid="whats-new-local-series"' in linked
    unlinked = next(row for row in rows if "#2" in row)
    assert "Issue not linked" in unlinked
    assert 'data-testid="whats-new-local-issue"' not in unlinked
    assert 'data-testid="whats-new-grab"' not in response.text
    parser = _ReleaseLinkParser()
    parser.feed(response.text)
    links = [
        attrs
        for tag, attrs in parser.nodes
        if tag == "a" and attrs.get("data-testid") == "whats-new-local-issue"
    ]
    assert len(links) == 1
    assert links[0]["aria-label"] == f"View issue 1 of {series_title}"
    assert links[0]["href"] == f"/issues/{issue_id}"
    assert "View issue" in parser.text
    assert "onfocus" not in links[0]
    assert not any("data-label-x" in attrs for _, attrs in parser.nodes)


def test_grouping_retains_every_source_release_without_changing_cache():
    from copy import deepcopy

    first = _issue_summary()
    second = {**first, "locg_issue_id": 1514021, "metron_issue_id": 999}
    sources = [first, second]
    snapshot = deepcopy(sources)
    grouped = _release_list_view(sources)
    assert len(grouped) == 1
    assert grouped[0].get("_discovery_releases") == sources, (
        "variant evidence is lost before resolution"
    )
    assert sources == snapshot


async def test_sort_state_is_on_column_headers_not_buttons(authenticated_client, sec_db):
    async with sec_db.begin() as session:
        session.add(
            WhatsNewReleaseCache(
                cache_key="current-week:2026-03-11",
                cache_kind=WhatsNewCacheKind.CURRENT_WEEK,
                store_date=date(2026, 3, 11),
                payload={"store_date": "2026-03-11", "issues": [_issue_summary()]},
                fetched_at=datetime.now(UTC),
                last_successful_refresh_at=datetime.now(UTC),
            )
        )
    response = await authenticated_client.get("/whats-new")
    assert re.search(r'<th\b[^>]*aria-sort="ascending"', response.text), (
        "sort state is missing from the column header"
    )
    assert not re.search(r"<button\b[^>]*aria-sort=", response.text)

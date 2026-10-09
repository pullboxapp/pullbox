"""Reviewed issue links and single-issue refresh never rematch or modify files."""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import func, select

from pullbox.models import Issue, Series
from pullbox.models.issue import IssueStatus
from pullbox.models.metadata_identity import (
    IssueExternalIdentity,
    IssueIdentityEvent,
    SeriesExternalIdentity,
)
from pullbox.schemas.metadata_sources import MetadataFetch, MetadataPage, SourceStatus
from pullbox.services.metadata_discovery import MetadataSourceRegistry
from tests.api.test_metadata_sources_api import csrf, policy
from tests.api.test_series_metadata_links import confirmation
from tests.unit.test_metadata_source_reads import issue_row

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest_plugins = ["conftest_security"]


@pytest.fixture
async def issue_target(authenticated_client, sec_db, monkeypatch):
    configured = await authenticated_client.put(
        "/api/v1/metadata/sources/metron_api",
        json=policy(credential="synthetic-token"),
        headers=csrf(authenticated_client),
    )
    assert configured.status_code == 200
    async with sec_db.begin() as session:
        series = Series(title="Existing series", sort_title="existing", path="/kept/original")
        session.add(series)
        await session.flush()
        session.add(
            SeriesExternalIdentity(
                series_id=series.id,
                identity_namespace="metron",
                external_id="42",
                verification_state="verified",
                evidence_kind="user_selection",
            )
        )
        issue = Issue(
            series_id=series.id,
            issue_number=50,
            issue_number_text="50-X",
            title="My manual title",
            status=IssueStatus.OWNED,
            manual_skip=True,
        )
        session.add(issue)
        await session.flush()
        issue_id = issue.id

    async def detail(self, source, external_id):
        return MetadataFetch(
            status=SourceStatus.OK,
            data=issue_row(
                external_id=external_id,
                title="Provider title",
                description="Provider description",
                cover_date=date(1999, 1, 1),
                page_count=32,
            ),
        )

    async def candidates(self, source, external_id, *, page=1):
        assert external_id == "42"
        return MetadataFetch(
            status=SourceStatus.OK, data=MetadataPage(results=[issue_row()], total=1)
        )

    monkeypatch.setattr(MetadataSourceRegistry, "issue", detail)
    monkeypatch.setattr(MetadataSourceRegistry, "issues", candidates)
    return issue_id


def url(issue_id):
    return f"/api/v1/issues/{issue_id}/metadata-links"


async def preview(client, issue_id):
    return await client.post(
        url(issue_id) + "/preview",
        json={"source": "metron_api", "external_id": "123"},
        headers=csrf(client),
    )


async def link(client, issue_id):
    result = await preview(client, issue_id)
    assert result.status_code == 200, result.text
    result = await client.post(
        url(issue_id) + "/confirm", json=confirmation(result.json()), headers=csrf(client)
    )
    assert result.status_code == 200, result.text


async def test_issue_review_candidates_confirmation_and_panel(
    authenticated_client, issue_target, sec_db
):
    panel = await authenticated_client.get(url(issue_target))
    assert panel.status_code == 200, panel.text
    assert panel.json()["sources"][0]["series_external_id"] == "42"
    candidates = await authenticated_client.post(
        url(issue_target) + "/candidates",
        json={"source": "metron_api", "page": 1},
        headers=csrf(authenticated_client),
    )
    assert candidates.status_code == 200
    assert candidates.json()["data"]["results"][0]["issue_number_text"] == "50-x"
    result = await preview(authenticated_client, issue_target)
    assert result.status_code == 200
    data = result.json()
    assert data["current"]["issue_number_text"] == "50-X"
    assert data["current"].get("page_count", "missing") is None
    assert data["review"]["verification_state"] == "observed"
    for replayed in (False, True):
        confirmed = await authenticated_client.post(
            url(issue_target) + "/confirm",
            json=confirmation(data),
            headers=csrf(authenticated_client),
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["replayed"] is replayed
    async with sec_db() as session:
        issue = await session.get(Issue, issue_target)
        assert issue.title == "My manual title" and issue.issue_number_text == "50-X"
        assert issue.status is IssueStatus.OWNED and issue.manual_skip
        assert (await session.scalar(select(IssueExternalIdentity))).external_id == "123"
        assert await session.scalar(select(func.count()).select_from(Issue)) == 1
    page = await authenticated_client.get(f"/issues/{issue_target}")
    assert 'data-testid="issue-metadata-links"' in page.text
    assert "Refresh metadata" in page.text


@pytest.mark.parametrize("change", ["parent", "number", "crosswalk", "identity"])
async def test_wrong_provider_issue_is_not_recorded(
    authenticated_client, issue_target, sec_db, monkeypatch, change
):
    from pullbox.core.metadata_identity import (
        ExternalIdentityRef,
        IdentityNamespace,
        MetadataEntityKind,
    )

    async def wrong(self, source, external_id):
        updates = {
            "parent": {"series_external_id": "99"},
            "number": {"issue_number_text": "50-o"},
            "identity": {"external_id": "999"},
            "crosswalk": {
                "cross_identities": [
                    ExternalIdentityRef(IdentityNamespace.COMICVINE, MetadataEntityKind.ISSUE, "2")
                ]
            },
        }
        return MetadataFetch(status=SourceStatus.OK, data=issue_row(**updates[change]))

    if change == "crosswalk":
        async with sec_db.begin() as session:
            (await session.get(Issue, issue_target)).comicvine_id = 1
    monkeypatch.setattr(MetadataSourceRegistry, "issue", wrong)
    result = await preview(authenticated_client, issue_target)
    assert result.status_code == 409, result.text
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(IssueIdentityEvent)) == 0


@pytest.mark.parametrize("change", ["parent", "target", "policy", "owner"])
async def test_issue_review_rechecks_authority_before_confirmation(
    authenticated_client, issue_target, sec_db, change
):
    result = await preview(authenticated_client, issue_target)
    assert result.status_code == 200
    if change == "policy":
        await authenticated_client.put(
            "/api/v1/metadata/sources/metron_api",
            json=policy(revision=1, enabled=False),
            headers=csrf(authenticated_client),
        )
    else:
        async with sec_db.begin() as session:
            issue = await session.get(Issue, issue_target)
            if change == "parent":
                claim = await session.scalar(select(SeriesExternalIdentity))
                claim.external_id = "99"
                claim.revision += 1
            elif change == "target":
                issue.title = "New manual edit"
            else:
                other = Issue(series_id=issue.series_id, issue_number=51, issue_number_text="51")
                session.add(other)
                await session.flush()
                session.add(
                    IssueExternalIdentity(
                        issue_id=other.id,
                        identity_namespace="metron",
                        external_id="123",
                        verification_state="verified",
                        evidence_kind="user_selection",
                    )
                )
    result = await authenticated_client.post(
        url(issue_target) + "/confirm",
        json=confirmation(result.json()),
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 409, result.text
    async with sec_db() as session:
        assert (
            await session.scalar(
                select(IssueExternalIdentity).where(IssueExternalIdentity.issue_id == issue_target)
            )
            is None
        )


async def test_issue_refresh_fills_gaps_and_preserves_manual_fields_and_ownership(
    authenticated_client, issue_target, sec_db, monkeypatch
):
    await link(authenticated_client, issue_target)
    result = await authenticated_client.post(
        f"/api/v1/issues/{issue_target}/refresh-metadata", headers=csrf(authenticated_client)
    )
    assert result.status_code == 200, result.text
    panel = await authenticated_client.get(url(issue_target))
    origins = {item["field"]: item for item in panel.json()["origins"]}
    assert origins["description"]["source"] == "metron_api"
    assert origins["title"]["source"] is None
    async with sec_db.begin() as session:
        issue = await session.get(Issue, issue_target)
        assert issue.title == "My manual title" and issue.description == "Provider description"
        assert issue.page_count == 32 and issue.release_date == date(1999, 1, 1)
        assert issue.issue_number_text == "50-X" and issue.status is IssueStatus.OWNED
        assert issue.manual_skip
        issue.description = "Later manual edit"

    panel = await authenticated_client.get(url(issue_target))
    origins = {item["field"]: item for item in panel.json()["origins"]}
    assert origins["description"]["user_override"], "Show current local edits, not stale provenance"

    async def updated(self, source, external_id):
        return MetadataFetch(
            status=SourceStatus.OK,
            data=issue_row(title="New title", description="Updated description", page_count=40),
        )

    monkeypatch.setattr(MetadataSourceRegistry, "issue", updated)
    result = await authenticated_client.post(
        f"/api/v1/issues/{issue_target}/refresh-metadata", headers=csrf(authenticated_client)
    )
    assert result.status_code == 200, result.text
    panel = await authenticated_client.get(url(issue_target))
    origins = {item["field"]: item for item in panel.json()["origins"]}
    assert origins["description"]["user_override"]
    async with sec_db() as session:
        issue = await session.get(Issue, issue_target)
        assert issue.description == "Later manual edit" and issue.page_count == 40


async def test_issue_refresh_endpoint_fills_proven_cached_store_date(
    authenticated_client, issue_target, sec_db
):
    from pullbox.services.whats_new_cache_service import WhatsNewCacheService

    await link(authenticated_client, issue_target)
    async with sec_db.begin() as session:
        issue = await session.get(Issue, issue_target)
        session.add(
            SeriesExternalIdentity(
                series_id=issue.series_id,
                identity_namespace="locg",
                external_id="77",
                verification_state="verified",
                evidence_kind="user_selection",
            )
        )
        await WhatsNewCacheService().upsert_upcoming(
            session,
            payload={
                "weeks": [
                    {
                        "issues": [
                            {
                                "locg_issue_id": 1001,
                                "locg_series_id": 77,
                                "metron_issue_id": 123,
                                "issue_number": "50-x",
                                "store_date": "2026-09-30",
                                "series": {"locg_series_id": 77, "metron_series_id": 42},
                            }
                        ]
                    }
                ]
            },
        )
    response = await authenticated_client.post(
        f"/api/v1/issues/{issue_target}/refresh-metadata", headers=csrf(authenticated_client)
    )
    assert response.status_code == 200, response.text
    panel = await authenticated_client.get(url(issue_target))
    origin = next(item for item in panel.json()["origins"] if item["field"] == "store_date")
    assert origin["source"] is None
    assert origin["passive_release"]["match_kind"] == "exact_issue"
    assert origin["passive_release"]["issue_number_text"] == "50-X"
    async with sec_db() as session:
        issue = await session.get(Issue, issue_target)
        assert issue.store_date == date(2026, 9, 30) and issue.release_date == date(1999, 1, 1)
        assert issue.status is IssueStatus.OWNED and issue.manual_skip
        assert issue.title == "My manual title"
        assert await session.scalar(select(func.count()).select_from(Issue)) == 1
        assert await session.scalar(select(func.count()).select_from(IssueExternalIdentity)) == 1


async def test_link_and_refresh_require_operator_csrf_and_parent_link(
    authenticated_client, unauthenticated_client, sec_api_key, issue_target, sec_db
):
    for endpoint in (
        url(issue_target) + "/preview",
        f"/api/v1/issues/{issue_target}/refresh-metadata",
    ):
        body = (
            {"source": "metron_api", "external_id": "123"} if endpoint.endswith("preview") else None
        )
        assert (await authenticated_client.post(endpoint, json=body)).status_code == 403
        assert (await unauthenticated_client.post(endpoint, json=body)).status_code in {401, 403}
        assert (
            await unauthenticated_client.post(
                endpoint, json=body, headers={"X-API-Key": sec_api_key}
            )
        ).status_code in {401, 403}
    async with sec_db.begin() as session:
        (await session.scalar(select(SeriesExternalIdentity))).verification_state = "stale"
    panel = await authenticated_client.get(url(issue_target))
    assert panel.status_code == 200 and panel.json()["sources"] == []
    assert (await preview(authenticated_client, issue_target)).status_code == 409


async def test_initial_sync_disables_issue_refresh_without_hiding_links(
    authenticated_client, issue_target, sec_db
):
    from pullbox.models.series import IssueCatalogState

    await link(authenticated_client, issue_target)
    async with sec_db.begin() as session:
        (await session.scalar(select(Series))).issue_catalog_state = IssueCatalogState.HYDRATING
    panel = await authenticated_client.get(url(issue_target))
    assert panel.status_code == 200
    assert not panel.json()["can_refresh"]
    assert panel.json()["syncing"]
    assert panel.json()["identities"][0]["state"] == "verified"

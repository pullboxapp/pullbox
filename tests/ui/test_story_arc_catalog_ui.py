"""Provider-backed Story Arc discovery remains explicit and source preserving."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pullbox.models.config import SystemConfig
from pullbox.models.library import LibraryRoot
from pullbox.models.story_arc import IssueStoryArc, StoryArc
from pullbox.providers.metadata.comicvine import ComicVineError
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService
from pullbox.services.story_arc_catalog import StoryArcCatalogService
from tests.story_arc_catalog_fixtures import CatalogProvider

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest_plugins = ["conftest_security"]


@pytest.fixture
def catalog_provider(monkeypatch: pytest.MonkeyPatch) -> CatalogProvider:
    provider = CatalogProvider()
    monkeypatch.setattr(
        "pullbox.core.comicvine_key.get_comicvine_api_key", AsyncMock(return_value="test")
    )
    monkeypatch.setattr(
        "pullbox.providers.metadata.comicvine.ComicVineProvider", lambda **_: provider
    )
    return provider


async def _root(factory: async_sessionmaker[AsyncSession], path: str) -> int:
    async with factory() as session:
        root = LibraryRoot(name="Canonical library", path=path, enabled=True)
        session.add(root)
        await session.commit()
        return root.id


def _csrf(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(SESSION_COOKIE_NAME) or ""
    return {"X-CSRF-Token": AuthService.get_csrf_token_from_session(token) or ""}


async def test_registry_links_to_dedicated_comicvine_add_page(
    authenticated_client: AsyncClient,
):
    registry = await authenticated_client.get("/story-arcs")

    assert registry.status_code == 200
    assert 'data-testid="story-arcs-add-link"' in registry.text
    assert 'href="/story-arcs/add"' in registry.text
    assert 'data-testid="story-arc-catalog-search"' not in registry.text

    response = await authenticated_client.get("/story-arcs/add")

    assert response.status_code == 200
    assert 'data-testid="story-arc-add-page"' in response.text
    assert 'data-testid="story-arc-add-header"' in response.text
    assert 'data-testid="story-arc-add-title"' in response.text
    assert "ADD <span>STORY ARC</span>" in response.text
    assert 'data-testid="story-arc-catalog-search"' in response.text
    assert 'hx-get="/story-arcs/add"' in response.text
    assert 'hx-target="#story-arc-add-results"' in response.text
    assert 'data-search-field-contract="baseline-v2"' in response.text
    assert 'data-testid="story-arc-add-results"' in response.text
    assert 'data-testid="story-arc-add-footer-dock"' in response.text
    assert "Search Comic Vine" in response.text
    assert 'data-testid="story-arcs-create-form"' not in response.text


async def test_catalog_empty_query_does_not_search_provider(authenticated_client: AsyncClient):
    response = await authenticated_client.get("/story-arcs/catalog", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "Enter at least two characters" in response.text
    assert "<!DOCTYPE" not in response.text


async def test_provider_arc_detail_offers_review_before_refresh(
    authenticated_client: AsyncClient, sec_db: async_sessionmaker[AsyncSession]
):
    async with sec_db() as session:
        arc = StoryArc(name="Provider event", normalized_name="provider event", comicvine_id=42)
        session.add(arc)
        await session.commit()
        arc_id = arc.id

    response = await authenticated_client.get(f"/story-arcs/{arc_id}")

    assert response.status_code == 200
    assert f'href="/story-arcs/{arc_id}/catalog-refresh"' in response.text
    assert "Review provider changes" in response.text
    assert f'action="/story-arcs/{arc_id}/search"' in response.text
    assert "Search missing issues" in response.text
    assert 'href="https://comicvine.gamespot.com/story-arc/4045-42/"' in response.text


async def test_catalog_search_marks_already_added_and_does_not_create(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    catalog_provider: CatalogProvider,
):
    async with sec_db() as session:
        arc = StoryArc(name="Already Here", normalized_name="already here", comicvine_id=43)
        session.add(arc)
        await session.commit()
        arc_id = arc.id
    response = await authenticated_client.get(
        "/story-arcs/catalog?q=event", headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert 'href="/story-arcs/catalog/42"' in response.text
    assert f'href="/story-arcs/{arc_id}"' in response.text
    assert "Already added" in response.text
    assert catalog_provider.searches == [("event", 20, 0)]
    assert catalog_provider.closed == 1
    async with sec_db() as session:
        assert len(list(await session.scalars(select(StoryArc)))) == 1


async def test_catalog_preview_requires_order_review_and_separate_canonical_root(
    authenticated_client: AsyncClient, catalog_provider: CatalogProvider
):
    response = await authenticated_client.get("/story-arcs/catalog/42")
    assert response.status_code == 200
    assert "Comic Vine response order — reading order unverified" in response.text
    assert "1000000" in response.text and "1AU" in response.text
    assert 'name="order_reviewed"' in response.text
    assert 'name="reading_orders"' in response.text
    assert "Library root for new series" in response.text
    assert 'name="library_root_id"' in response.text
    assert 'name="target_library_root_id"' in response.text
    assert "Keep the original filename" in response.text
    assert "Prefix arc filenames with the reading order" in response.text


async def test_partial_catalog_blocks_add_and_retains_retry(
    authenticated_client: AsyncClient, catalog_provider: CatalogProvider
):
    catalog_provider.metadata = replace(
        catalog_provider.metadata, membership_complete=False, declared_issue_count=3
    )
    response = await authenticated_client.get("/story-arcs/catalog/42")
    assert response.status_code == 200
    assert "Incomplete member list" in response.text
    assert 'data-testid="story-arc-catalog-add-form"' not in response.text
    assert "Retry preview" in response.text


async def test_provider_failure_is_safe_and_retryable(
    authenticated_client: AsyncClient, catalog_provider: CatalogProvider
):
    catalog_provider.fail = True
    response = await authenticated_client.get(
        "/story-arcs/catalog?q=event", headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert "Comic Vine search failed" in response.text
    assert "secret-bearing" not in response.text
    assert 'role="alert"' in response.text


@pytest.mark.parametrize("invalid", ["unreviewed", "stale", "duplicate-order", "missing-member"])
async def test_add_rejects_unreviewed_stale_or_invalid_member_set(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    catalog_provider: CatalogProvider,
    tmp_path,
    invalid: str,
):
    root_id = await _root(sec_db, str(tmp_path))
    response = await authenticated_client.get("/story-arcs/catalog/42")
    assert response.status_code == 200
    token = re.search(r'name="fingerprint" value="([^"]+)"', response.text)
    assert token is not None
    data = {
        "fingerprint": token.group(1),
        "order_reviewed": "true",
        "library_root_id": str(root_id),
        "issue_provider_ids": ["101", "102"],
        "reading_orders": ["2", "1"],
    }
    if invalid == "unreviewed":
        data.pop("order_reviewed")
    elif invalid == "stale":
        data["fingerprint"] = "old"
    elif invalid == "duplicate-order":
        data["reading_orders"] = ["1", "1"]
    else:
        data["issue_provider_ids"] = ["101"]
        data["reading_orders"] = ["1"]
    response = await authenticated_client.post(
        "/story-arcs/catalog/42",
        data=data,
        headers=_csrf(authenticated_client),
        follow_redirects=False,
    )
    assert response.status_code in (303, 409)
    async with sec_db() as session:
        assert list(await session.scalars(select(StoryArc))) == []


async def test_add_persists_reviewed_order_and_independent_copy_settings(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    catalog_provider: CatalogProvider,
    tmp_path,
):
    root_id = await _root(sec_db, str(tmp_path))
    response = await authenticated_client.get("/story-arcs/catalog/42")
    assert response.status_code == 200
    token = re.search(r'name="fingerprint" value="([^"]+)"', response.text)
    assert token is not None
    response = await authenticated_client.post(
        "/story-arcs/catalog/42",
        data={
            "fingerprint": token.group(1),
            "order_reviewed": "true",
            "library_root_id": str(root_id),
            "issue_provider_ids": ["101", "102"],
            "reading_orders": ["2", "1"],
            "mode": "copy",
            "target_library_root_id": str(root_id),
            "destination_root": str(tmp_path),
            "prefix_reading_order": "true",
            "reading_order_width": "2",
            "synchronize": "true",
        },
        headers=_csrf(authenticated_client),
        follow_redirects=False,
    )
    assert response.status_code == 303
    async with sec_db() as session:
        arc = await session.scalar(select(StoryArc).where(StoryArc.comicvine_id == 42))
        assert arc is not None
        members = list(
            await session.scalars(
                select(IssueStoryArc)
                .where(IssueStoryArc.story_arc_id == arc.id)
                .order_by(IssueStoryArc.sequence_number)
            )
        )
        assert [member.source_issue_id for member in members] == ["102", "101"]
        assert arc.policy_snapshot["file_template"] == "{ReadingOrder:02d} - {OriginalFilename}"
        assert arc.policy_snapshot["mode"] == "copy"
    assert list(tmp_path.iterdir()) == []


async def test_failed_provider_cleanup_cannot_report_failure_after_committing_add(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    catalog_provider: CatalogProvider,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    root_id = await _root(sec_db, str(tmp_path))
    response = await authenticated_client.get("/story-arcs/catalog/42")
    token = re.search(r'name="fingerprint" value="([^"]+)"', response.text)
    assert token is not None
    monkeypatch.setattr(
        catalog_provider, "close", AsyncMock(side_effect=ComicVineError(500, "close failed"))
    )
    response = await authenticated_client.post(
        "/story-arcs/catalog/42",
        data={
            "fingerprint": token.group(1),
            "order_reviewed": "true",
            "library_root_id": str(root_id),
            "issue_provider_ids": ["101", "102"],
            "reading_orders": ["1", "2"],
        },
        headers=_csrf(authenticated_client),
        follow_redirects=False,
    )
    assert response.status_code == 303
    async with sec_db() as session:
        assert list(await session.scalars(select(StoryArc))) == []


@pytest.mark.parametrize(
    "global_enabled,arc_monitored,search_missing,expected",
    [
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
        (True, True, True, True),
    ],
)
async def test_add_auto_search_requires_all_optins_and_runs_after_commit(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    catalog_provider: CatalogProvider,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    global_enabled: bool,
    arc_monitored: bool,
    search_missing: bool,
    expected: bool,
):
    root_id = await _root(sec_db, str(tmp_path))
    async with sec_db() as session:
        session.add(
            SystemConfig(
                key="search_on_add_default", value=str(global_enabled).lower(), value_type="bool"
            )
        )
        await session.commit()
    schedule = Mock(return_value=True)
    monkeypatch.setattr("pullbox.tasks.story_arc_search_task.schedule_story_arc_search", schedule)
    response = await authenticated_client.get("/story-arcs/catalog/42")
    token = re.search(r'name="fingerprint" value="([^"]+)"', response.text)
    assert token is not None
    response = await authenticated_client.post(
        "/story-arcs/catalog/42",
        data={
            "fingerprint": token.group(1),
            "order_reviewed": "true",
            "library_root_id": str(root_id),
            "issue_provider_ids": ["101", "102"],
            "reading_orders": ["1", "2"],
            "monitored": str(arc_monitored).lower(),
            "search_missing": str(search_missing).lower(),
        },
        headers=_csrf(authenticated_client),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert schedule.call_count == int(expected)
    async with sec_db() as session:
        arc = await session.scalar(select(StoryArc))
        assert arc is not None
        if expected:
            schedule.assert_called_once_with(arc.id)


async def test_refresh_reviews_additions_and_preserves_removed_members_and_order(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    catalog_provider: CatalogProvider,
    tmp_path,
):
    root_id = await _root(sec_db, str(tmp_path))
    service = StoryArcCatalogService(catalog_provider)
    preview = await service.preview("42")
    async with sec_db() as session:
        arc = await service.add(
            session, preview, ordered_issue_provider_ids=["102", "101"], library_root_id=root_id
        )
        await session.commit()
        arc_id = arc.id
    catalog_provider.metadata = replace(
        catalog_provider.metadata, issue_provider_ids=("101", "103")
    )
    response = await authenticated_client.get(f"/story-arcs/{arc_id}/catalog-refresh")
    assert response.status_code == 200
    assert "1 new members to review" in response.text
    assert "Comic Vine issue ID 102 — preserved" in response.text
    token = re.search(r'name="fingerprint" value="([^"]+)"', response.text)
    revision = re.search(r'name="expected_revision" value="([^"]+)"', response.text)
    assert token is not None and revision is not None
    async with sec_db() as session:
        assert len(list(await session.scalars(select(IssueStoryArc)))) == 2
    response = await authenticated_client.post(
        f"/story-arcs/{arc_id}/catalog-refresh",
        data={
            "fingerprint": token.group(1),
            "expected_revision": revision.group(1),
            "confirm_refresh": "true",
        },
        headers=_csrf(authenticated_client),
        follow_redirects=False,
    )
    assert response.status_code == 303
    async with sec_db() as session:
        members = list(
            await session.scalars(select(IssueStoryArc).order_by(IssueStoryArc.sequence_number))
        )
        assert [member.source_issue_id for member in members] == ["102", "101", "103"]
        assert members[-1].resolution_state.value == "pending"
        assert members[-1].sync_eligible is False
    detail = await authenticated_client.get(f"/story-arcs/{arc_id}")
    assert "Confirm this member" in detail.text
    assert "1 no longer listed by Comic Vine but preserved here" in detail.text
    assert 'data-tip="Open issue"' in detail.text
    assert "Open issue / manual search" not in detail.text


async def test_manual_arc_search_is_authenticated_csrf_protected_and_active_only(
    authenticated_client: AsyncClient,
    unauthenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    async with sec_db() as session:
        arc = StoryArc(name="Unmonitored manual arc", normalized_name="unmonitored manual arc")
        session.add(arc)
        await session.commit()
        arc_id = arc.id
    schedule = Mock(return_value=True)
    monkeypatch.setattr("pullbox.tasks.story_arc_search_task.schedule_story_arc_search", schedule)
    denied = await authenticated_client.post(f"/story-arcs/{arc_id}/search", follow_redirects=False)
    assert denied.status_code == 403
    denied = await unauthenticated_client.get("/story-arcs/catalog?q=event", follow_redirects=False)
    assert denied.status_code in (302, 303, 401)
    schedule.assert_not_called()
    result = await authenticated_client.post(
        f"/story-arcs/{arc_id}/search", headers=_csrf(authenticated_client), follow_redirects=False
    )
    assert result.status_code == 303
    schedule.assert_called_once_with(arc_id)
    missing = await authenticated_client.post(
        "/story-arcs/999999/search", headers=_csrf(authenticated_client), follow_redirects=False
    )
    assert missing.status_code == 404


async def test_detail_shows_initial_copy_progress_and_explicit_retry(
    authenticated_client: AsyncClient, sec_db: async_sessionmaker[AsyncSession]
):
    async with sec_db() as session:
        arc = StoryArc(
            name="Copy review",
            normalized_name="copy review",
            comicvine_id=42,
            diagnostics={
                "catalog_initial_placements": {
                    "schema_version": 1,
                    "state": "failed",
                    "total": 5,
                    "completed": 2,
                    "failed": 1,
                    "pending": 2,
                }
            },
        )
        session.add(arc)
        await session.commit()
        arc_id = arc.id
    response = await authenticated_client.get(f"/story-arcs/{arc_id}")
    assert response.status_code == 200
    assert 'data-testid="story-arc-initial-placements"' in response.text
    assert "2 completed" in response.text
    assert "1 failed" in response.text
    assert "2 pending" in response.text
    assert f'action="/story-arcs/{arc_id}/initial-placements/retry"' in response.text
    assert "Retry initial arc files" in response.text


async def test_empty_complete_provider_list_is_not_offered_as_addable(
    authenticated_client: AsyncClient, catalog_provider: CatalogProvider
):
    catalog_provider.metadata = replace(
        catalog_provider.metadata, issue_provider_ids=(), declared_issue_count=0
    )
    response = await authenticated_client.get("/story-arcs/catalog/42")
    assert response.status_code == 200
    assert "No members available" in response.text
    assert 'data-testid="story-arc-catalog-add-form"' not in response.text


async def test_add_runs_initial_copy_after_commit_even_when_future_sync_is_off(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    catalog_provider: CatalogProvider,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    root_id = await _root(sec_db, str(tmp_path))
    original_add = StoryArcCatalogService.add

    async def add_with_pending_copy(service, session, *args, **kwargs):
        arc = await original_add(service, session, *args, **kwargs)
        arc.diagnostics = {
            **arc.diagnostics,
            "catalog_initial_placements": {
                "schema_version": 1,
                "state": "pending",
                "total": 1,
                "completed": 0,
                "failed": 0,
                "pending": 1,
            },
        }
        await session.flush()
        return arc

    async def assert_committed(arc_id, **kwargs):
        assert kwargs["session_factory"] is sec_db
        async with sec_db() as session:
            arc = await session.get(StoryArc, arc_id)
            assert arc is not None and not arc.sync_enabled

    initial = AsyncMock(side_effect=assert_committed)
    monkeypatch.setattr(StoryArcCatalogService, "add", add_with_pending_copy)
    monkeypatch.setattr(
        "pullbox.services.story_arc_catalog_placement.run_catalog_initial_placements", initial
    )
    response = await authenticated_client.get("/story-arcs/catalog/42")
    token = re.search(r'name="fingerprint" value="([^"]+)"', response.text)
    assert token is not None
    response = await authenticated_client.post(
        "/story-arcs/catalog/42",
        data={
            "fingerprint": token.group(1),
            "order_reviewed": "true",
            "library_root_id": str(root_id),
            "issue_provider_ids": ["101", "102"],
            "reading_orders": ["1", "2"],
            "mode": "copy",
            "target_library_root_id": str(root_id),
            "destination_root": str(tmp_path),
            "synchronize": "false",
        },
        headers=_csrf(authenticated_client),
        follow_redirects=False,
    )
    assert response.status_code == 303
    initial.assert_awaited_once()


async def test_initial_copy_retry_is_explicit_and_scoped_to_provider_arc(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    async with sec_db() as session:
        arc = StoryArc(
            name="Retry copy",
            normalized_name="retry copy",
            comicvine_id=42,
            diagnostics={
                "catalog_initial_placements": {"state": "failed", "failed": 1, "pending": 0}
            },
        )
        session.add(arc)
        await session.commit()
        arc_id = arc.id
    initial = AsyncMock()
    monkeypatch.setattr(
        "pullbox.services.story_arc_catalog_placement.run_catalog_initial_placements", initial
    )
    response = await authenticated_client.post(
        f"/story-arcs/{arc_id}/initial-placements/retry",
        headers=_csrf(authenticated_client),
        follow_redirects=False,
    )
    assert response.status_code == 303
    initial.assert_awaited_once_with(arc_id, retry_failed=True, session_factory=sec_db)


async def test_imported_arc_refresh_requires_explicit_root_for_new_series_only(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    catalog_provider: CatalogProvider,
    tmp_path,
):
    root_id = await _root(sec_db, str(tmp_path))
    async with sec_db() as session:
        arc = StoryArc(
            name="Imported user name", normalized_name="imported user name", comicvine_id=42
        )
        session.add(arc)
        await session.commit()
        arc_id = arc.id
    response = await authenticated_client.get(f"/story-arcs/{arc_id}/catalog-refresh")
    assert response.status_code == 200
    assert "Library root for new series" in response.text
    assert 'name="library_root_id"' in response.text
    assert "Existing series paths and arc storage stay unchanged" in response.text
    token = re.search(r'name="fingerprint" value="([^"]+)"', response.text)
    revision = re.search(r'name="expected_revision" value="([^"]+)"', response.text)
    assert token is not None and revision is not None
    form = {
        "fingerprint": token.group(1),
        "expected_revision": revision.group(1),
        "confirm_refresh": "true",
    }
    missing = await authenticated_client.post(
        f"/story-arcs/{arc_id}/catalog-refresh",
        data=form,
        headers=_csrf(authenticated_client),
        follow_redirects=False,
    )
    assert missing.status_code == 303
    async with sec_db() as session:
        assert list(await session.scalars(select(IssueStoryArc))) == []
    response = await authenticated_client.post(
        f"/story-arcs/{arc_id}/catalog-refresh",
        data={**form, "library_root_id": str(root_id)},
        headers=_csrf(authenticated_client),
        follow_redirects=False,
    )
    assert response.status_code == 303
    async with sec_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc is not None and arc.name == "Imported user name"
        assert arc.diagnostics["provider_catalog"]["canonical_library_root_id"] == root_id
        assert arc.target_library_root_id is None


async def test_saved_unavailable_refresh_root_has_actionable_guidance(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    catalog_provider: CatalogProvider,
    tmp_path,
):
    root_id = await _root(sec_db, str(tmp_path))
    async with sec_db() as session:
        root = await session.get(LibraryRoot, root_id)
        root.enabled = False
        arc = StoryArc(
            name="Saved root",
            comicvine_id=42,
            diagnostics={"provider_catalog": {"canonical_library_root_id": root_id}},
        )
        session.add(arc)
        await session.commit()
        arc_id = arc.id
    preview = await authenticated_client.get(f"/story-arcs/{arc_id}/catalog-refresh")
    token = re.search(r'name="fingerprint" value="([^"]+)"', preview.text)
    revision = re.search(r'name="expected_revision" value="([^"]+)"', preview.text)
    assert token is not None and revision is not None
    result = await authenticated_client.post(
        f"/story-arcs/{arc_id}/catalog-refresh",
        data={
            "fingerprint": token.group(1),
            "expected_revision": revision.group(1),
            "confirm_refresh": "true",
        },
        headers=_csrf(authenticated_client),
        follow_redirects=False,
    )
    assert result.status_code == 303
    error = await authenticated_client.get(result.headers["location"])
    assert "Restore or enable the saved library root in Settings" in error.text
    async with sec_db() as session:
        assert list(await session.scalars(select(IssueStoryArc))) == []

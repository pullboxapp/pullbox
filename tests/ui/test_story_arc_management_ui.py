"""UI contracts for first-class Story Arc management."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcLifecycle
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService
from pullbox.services.story_arc_service import StoryArcService

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-story-arc-ui")


def _csrf_header_for(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(SESSION_COOKIE_NAME) or ""
    csrf = AuthService.get_csrf_token_from_session(token) or ""
    return {"X-CSRF-Token": csrf}


async def _seed_list_arcs(factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    service = StoryArcService()
    async with factory() as session:
        absolute = await service.create(
            session,
            name="Absolute Power",
            description="A multiverse event",
            monitored=True,
            search_missing=True,
        )
        await service.add_membership(
            session,
            absolute.id,
            issue_id=None,
            sequence_number=1,
            source_issue_number_text="1AU",
        )
        brainiac = await service.create(session, name="House of Brainiac", monitored=False)
        archived = await service.create(session, name="Absolute Universe")
        await service.archive(session, archived.id, expected_revision=1)
        await session.commit()
        return {
            "absolute": absolute.id,
            "brainiac": brainiac.id,
            "archived": archived.id,
        }


async def _seed_detail_arc(factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    service = StoryArcService()
    async with factory() as session:
        root = LibraryRoot(name="Story Arc Library", path="/tmp/story-arc-ui", enabled=True)
        million_series = Series(title="DC One Million", sort_title="dc one million")
        annual_series = Series(title="Batman Annual", sort_title="batman annual")
        session.add_all([root, million_series, annual_series])
        await session.flush()

        million = Issue(
            series_id=million_series.id,
            issue_number=1_000_000,
            issue_number_text="1000000",
            title="The Final Hour",
            status=IssueStatus.OWNED,
        )
        annual = Issue(
            series_id=annual_series.id,
            issue_number=1,
            issue_number_text="1",
            title="Annual Resolution Target",
            status=IssueStatus.WANTED,
        )
        session.add_all([million, annual])
        await session.flush()
        session.add(
            LibraryFile(
                issue_id=million.id,
                library_root_id=root.id,
                file_path="/tmp/story-arc-ui/DC One Million 1000000.cbz",
                file_name="DC One Million 1000000.cbz",
                file_size=1024,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime(2026, 8, 30, tzinfo=UTC),
                match_confidence=MatchConfidence.MANUAL,
            )
        )

        arc = await service.create(
            session,
            name="DC Numbering",
            description="Exact issue number regression coverage",
            monitored=True,
            search_missing=True,
        )
        million_membership = await service.add_membership(
            session,
            arc.id,
            issue_id=million.id,
            sequence_number=1,
            source_issue_number_text="1000000",
        )
        annual_membership = await service.add_membership(
            session,
            arc.id,
            issue_id=None,
            sequence_number=2,
            source_issue_number_text="1AU",
        )
        fractional_membership = await service.add_membership(
            session,
            arc.id,
            issue_id=None,
            sequence_number=3,
            source_issue_number_text="0.5",
        )
        await session.commit()
        return {
            "arc": arc.id,
            "million_issue": million.id,
            "annual_issue": annual.id,
            "million_membership": million_membership.id,
            "annual_membership": annual_membership.id,
            "fractional_membership": fractional_membership.id,
        }


@pytest.mark.asyncio
class TestStoryArcManagementUI:
    """Verify normal-navigation Story Arc management behavior."""

    async def test_list_is_searchable_filterable_paginated_and_in_normal_navigation(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = await _seed_list_arcs(sec_db)

        response = await authenticated_client.get(
            "/story-arcs",
            params={
                "q": "absolute",
                "lifecycle": "active",
                "monitored": "true",
                "page": 1,
                "per_page": 1,
            },
        )

        assert response.status_code == 200
        assert 'data-testid="story-arcs-page"' in response.text
        assert 'data-testid="sidebar-link-story-arcs"' in response.text
        assert 'data-nav-path="/story-arcs"' in response.text
        assert 'aria-current="page"' in response.text
        assert 'data-testid="story-arcs-search-input"' in response.text
        assert 'value="absolute"' in response.text
        assert 'data-testid="story-arcs-filter-form"' in response.text
        assert 'data-testid="story-arcs-create-form"' in response.text
        assert '<label for="story-arc-create-name"' in response.text
        assert '<label for="story-arc-create-description"' in response.text
        assert f'data-story-arc-id="{ids["absolute"]}"' in response.text
        assert "Absolute Power" in response.text
        assert "House of Brainiac" not in response.text
        assert "Absolute Universe" not in response.text
        assert 'data-testid="story-arc-missing-count">1<' in response.text
        assert "1 result" in response.text

        oversized = await authenticated_client.get("/story-arcs?per_page=101")
        assert oversized.status_code == 422

    async def test_create_empty_arc_commits_and_redirects_to_detail(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        response = await authenticated_client.post(
            "/story-arcs",
            data={
                "name": "  Batman   Endgame ",
                "description": "Created from the management page",
                "monitored": "true",
                "search_missing": "true",
            },
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/story-arcs/")
        async with sec_db() as session:
            arc = (
                await session.scalars(select(StoryArc).where(StoryArc.name == "Batman Endgame"))
            ).one()
            assert response.headers["location"] == f"/story-arcs/{arc.id}?notice=created"
            assert arc.description == "Created from the management page"
            assert arc.monitored is True
            assert arc.search_missing is True
            assert arc.revision == 1
            assert (
                await session.scalars(
                    select(IssueStoryArc).where(IssueStoryArc.story_arc_id == arc.id)
                )
            ).all() == []

    async def test_detail_is_bounded_exact_and_keyboard_accessible(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = await _seed_detail_arc(sec_db)

        response = await authenticated_client.get(f"/story-arcs/{ids['arc']}?page=1&per_page=2")

        assert response.status_code == 200
        assert 'data-testid="story-arc-detail-page"' in response.text
        assert "DC Numbering" in response.text
        assert 'data-exact-issue-number="1000000"' in response.text
        assert 'data-exact-issue-number="1AU"' in response.text
        assert 'data-exact-issue-number="0.5"' not in response.text
        assert "1e+06" not in response.text
        assert "DC One Million" in response.text
        assert "The Final Hour" in response.text
        assert "File available" in response.text
        assert 'data-resolution-state="resolved"' in response.text
        assert 'data-resolution-state="missing"' in response.text
        assert 'role="status" aria-live="polite"' in response.text
        assert 'aria-label="Move issue 1000000 down"' in response.text
        assert 'aria-label="Move issue 1AU up"' in response.text
        assert 'aria-label="Move issue 1AU down"' in response.text
        assert 'data-testid="story-arc-edit-form"' in response.text
        assert '<label for="story-arc-name"' in response.text
        assert '<label for="story-arc-description"' in response.text
        assert '<label for="story-arc-monitored"' in response.text
        assert 'data-testid="story-arc-add-membership-form"' in response.text
        assert '<label for="story-arc-add-issue-id"' in response.text
        assert '<label for="story-arc-add-exact-number"' in response.text
        assert 'data-testid="story-arc-resolve-membership-' in response.text
        assert '<label for="story-arc-resolve-issue-' in response.text
        assert 'data-testid="story-arc-remove-membership-' in response.text
        assert "Canonical issues stay in your library." in response.text
        assert "Canonical files stay in their current folders." in response.text
        assert "Monitoring and automation will be disabled." in response.text
        assert "pbConfirm" in response.text

        second_page = await authenticated_client.get(f"/story-arcs/{ids['arc']}?page=2&per_page=2")
        assert second_page.status_code == 200
        assert 'data-exact-issue-number="0.5"' in second_page.text
        assert 'data-exact-issue-number="1000000"' not in second_page.text
        assert 'data-exact-issue-number="1AU"' not in second_page.text

    async def test_edit_and_add_unresolved_entry_use_service_revision_contracts(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = await _seed_detail_arc(sec_db)
        async with sec_db() as session:
            revision = (await session.get(StoryArc, ids["arc"])).revision  # type: ignore[union-attr]

        edited = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/edit",
            data={
                "expected_revision": revision,
                "name": "DC Numbering Updated",
                "description": "Updated metadata",
                "include_upcoming": "true",
            },
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert edited.status_code == 303
        assert edited.headers["location"].endswith("?notice=updated")

        added = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/memberships",
            data={
                "sequence_number": 4,
                "issue_id": "",
                "source_issue_number_text": "2AU",
            },
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert added.status_code == 303
        assert added.headers["location"].endswith("?notice=membership-added")

        async with sec_db() as session:
            arc = await session.get(StoryArc, ids["arc"])
            assert arc is not None
            assert arc.name == "DC Numbering Updated"
            assert arc.description == "Updated metadata"
            assert arc.monitored is False
            assert arc.search_missing is False
            assert arc.include_upcoming is True
            exact_values = list(
                await session.scalars(
                    select(IssueStoryArc.source_issue_number_text)
                    .where(IssueStoryArc.story_arc_id == arc.id)
                    .order_by(IssueStoryArc.sequence_number)
                )
            )
            assert exact_values == ["1000000", "1AU", "0.5", "2AU"]

    async def test_move_resolve_remove_and_archive_preserve_canonical_issue_and_file(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = await _seed_detail_arc(sec_db)

        async with sec_db() as session:
            arc = await session.get(StoryArc, ids["arc"])
            assert arc is not None
            revision = arc.revision
        moved = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/memberships/{ids['million_membership']}/move",
            data={"direction": "down", "expected_revision": revision},
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert moved.status_code == 303
        assert moved.headers["location"].endswith("?notice=moved-down")

        resolved = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/memberships/{ids['annual_membership']}/resolve",
            data={"issue_id": ids["annual_issue"]},
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert resolved.status_code == 303
        assert resolved.headers["location"].endswith("?notice=resolved")

        removed = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/memberships/{ids['annual_membership']}/remove",
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert removed.status_code == 303
        assert removed.headers["location"].endswith("?notice=membership-removed")

        async with sec_db() as session:
            ordered_ids = list(
                await session.scalars(
                    select(IssueStoryArc.id)
                    .where(IssueStoryArc.story_arc_id == ids["arc"])
                    .order_by(
                        IssueStoryArc.sequence_number,
                        IssueStoryArc.source_ordinal,
                        IssueStoryArc.id,
                    )
                )
            )
            assert ordered_ids == [
                ids["million_membership"],
                ids["fractional_membership"],
            ]
            assert await session.get(Issue, ids["annual_issue"]) is not None
            arc = await session.get(StoryArc, ids["arc"])
            assert arc is not None
            revision = arc.revision

        archived = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/archive",
            data={"expected_revision": revision},
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert archived.status_code == 303
        assert archived.headers["location"].endswith("?notice=archived")

        async with sec_db() as session:
            arc = await session.get(StoryArc, ids["arc"])
            assert arc is not None
            assert arc.lifecycle == StoryArcLifecycle.ARCHIVED
            assert await session.get(Issue, ids["million_issue"]) is not None
            assert await session.get(Issue, ids["annual_issue"]) is not None
            assert await session.get(IssueStoryArc, ids["million_membership"]) is not None
            library_file = await session.scalar(
                select(LibraryFile).where(LibraryFile.issue_id == ids["million_issue"])
            )
            assert library_file is not None
            assert library_file.file_path == "/tmp/story-arc-ui/DC One Million 1000000.cbz"

"""UI contracts for first-class Story Arc management."""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.publisher import Publisher
from pullbox.models.reader import IssueReaderState
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcPlacement,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSymlinkStyle,
)
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService
from pullbox.services.story_arc_managed_reorder import StoryArcManagedReorderService
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementPolicyInput,
    StoryArcPlacementPolicyMode,
    StoryArcPlacementSyncService,
)
from pullbox.services.story_arc_service import StoryArcService
from pullbox.ui import story_arc_routes
from pullbox.ui.story_arc_presenters import _load_sync_work_summary

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-story-arc-ui")


@pytest.fixture(autouse=True)
def _enable_manual_story_arc_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        story_arc_routes,
        "get_settings",
        lambda: SimpleNamespace(story_arc_manual_create_enabled=True),
        raising=False,
    )


def _csrf_header_for(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(SESSION_COOKIE_NAME) or ""
    csrf = AuthService.get_csrf_token_from_session(token) or ""
    return {"X-CSRF-Token": csrf}


def _copy_policy_form(
    ids: dict[str, int | str],
    *,
    expected_revision: int | None = None,
) -> dict[str, str | int]:
    return {
        "expected_revision": (
            int(ids["revision"]) if expected_revision is None else expected_revision
        ),
        "mode": "copy",
        "target_library_root_id": int(ids["root"]),
        "destination_root": str(ids["destination"]),
        "folder_template": "{StoryArc}",
        "file_template": "{ReadingOrder:03d} - {Series} {IssueNumber}{IssueTitleOptional}",
        "symlink_style": "",
        "synchronize": "true",
    }


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


async def _seed_registry_metrics_arc(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: int,
    root_path: Path,
) -> int:
    service = StoryArcService()
    async with factory() as session:
        publisher = Publisher(name="DC Comics")
        root = LibraryRoot(name="Registry library", path=str(root_path), enabled=True)
        series = Series(title="Registry Series", sort_title="registry series")
        session.add_all([publisher, root, series])
        await session.flush()
        owned = Issue(
            series_id=series.id,
            issue_number=1,
            issue_number_text="1",
            status=IssueStatus.OWNED,
        )
        wanted = Issue(
            series_id=series.id,
            issue_number=2,
            issue_number_text="2",
            status=IssueStatus.WANTED,
        )
        session.add_all([owned, wanted])
        await session.flush()
        library_file = LibraryFile(
            issue_id=owned.id,
            library_root_id=root.id,
            file_path=str(root_path / "Registry Series 001.cbz"),
            file_name="Registry Series 001.cbz",
            file_size=1024,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.MANUAL,
        )
        session.add(library_file)
        session.add(
            IssueReaderState(
                user_id=user_id,
                issue_id=owned.id,
                completed_at=datetime.now(UTC),
                completion_updated_at=datetime.now(UTC),
            )
        )
        arc = await service.create(
            session,
            name="Registry Event",
            monitored=True,
            search_missing=True,
        )
        arc.publisher_id = publisher.id
        arc.comicvine_id = 31
        arc.comicvine_url = "https://comicvine.example/story-arc/31"
        arc.cover_url = "https://comicvine.example/story-arc/31.jpg"
        owned_membership = await service.add_membership(
            session,
            arc.id,
            issue_id=owned.id,
            sequence_number=1,
        )
        await service.add_membership(
            session,
            arc.id,
            issue_id=wanted.id,
            sequence_number=2,
        )
        ambiguous = await service.add_membership(
            session,
            arc.id,
            issue_id=None,
            sequence_number=3,
            source_issue_number_text="3",
        )
        ambiguous.resolution_state = StoryArcResolutionState.AMBIGUOUS
        session.add(
            StoryArcPlacement(
                issue_story_arc_id=owned_membership.id,
                library_file_id=library_file.id,
                library_root_id=root.id,
                placement_path=str(root_path / "Story Arcs" / "Registry Event 001.cbz"),
                ownership=StoryArcPlacementOwnership.REFERENCED,
                state=StoryArcPlacementState.FAILED,
            )
        )
        await session.commit()
        return arc.id


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


async def _seed_placement_arc(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> dict[str, int | str]:
    canonical = tmp_path / "library" / "DC One Million 1000000.cbz"
    canonical.parent.mkdir()
    canonical.write_bytes(b"canonical story arc issue")
    destination = canonical.parent / "StoryArcs"
    destination.mkdir()
    service = StoryArcService()
    async with factory() as session:
        root = LibraryRoot(name="Approved Comics", path=str(canonical.parent), enabled=True)
        series = Series(
            title="DC One Million",
            sort_title="dc one million",
            library_root=root,
        )
        issue = Issue(
            series=series,
            issue_number=1_000_000,
            issue_number_text="1000000",
            title="The Final Hour",
            status=IssueStatus.OWNED,
        )
        library_file = LibraryFile(
            issue=issue,
            library_root=root,
            file_path=str(canonical),
            file_name=canonical.name,
            file_size=canonical.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.MANUAL,
        )
        session.add(library_file)
        await session.flush()
        arc = await service.create(
            session,
            name="DC One Million",
            monitored=True,
            search_missing=True,
            include_upcoming=True,
        )
        membership = await service.add_membership(
            session,
            arc.id,
            issue_id=issue.id,
            sequence_number=1,
            source_issue_number_text="1000000",
        )
        await session.commit()
        return {
            "arc": arc.id,
            "membership": membership.id,
            "root": root.id,
            "revision": arc.revision,
            "canonical": str(canonical),
            "destination": str(destination),
        }


async def _seed_sync_work_summary(
    factory: async_sessionmaker[AsyncSession],
    ids: dict[str, int | str],
) -> None:
    async with factory() as session:
        library_file = (await session.scalars(select(LibraryFile))).one()
        arc = await session.get(StoryArc, int(ids["arc"]))
        assert arc is not None
        states = (
            StoryArcSyncWorkState.QUEUED,
            StoryArcSyncWorkState.RUNNING,
            StoryArcSyncWorkState.RETRY_WAIT,
            StoryArcSyncWorkState.FAILED,
            StoryArcSyncWorkState.COMPLETED,
        )
        for index, state in enumerate(states, start=1):
            session.add(
                StoryArcSyncWork(
                    issue_story_arc_id=int(ids["membership"]),
                    library_file_id=library_file.id,
                    desired_generation=f"ui-generation-{index}",
                    source_signature_hash=f"{index:064d}",
                    source_file_path=library_file.file_path,
                    source_file_size=library_file.file_size,
                    source_file_modified_at=library_file.file_modified_at,
                    story_arc_revision=arc.revision,
                    membership_sequence=1,
                    policy_schema_version=1,
                    state=state,
                )
            )
        await session.commit()


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
        assert 'data-testid="story-arcs-registry-header"' in response.text
        assert 'data-testid="story-arcs-registry-title"' in response.text
        assert "STORY ARC <span>REGISTRY</span>" in response.text
        assert 'data-testid="story-arcs-add-link"' in response.text
        assert 'href="/story-arcs/add"' in response.text
        assert 'data-search-field-contract="baseline-v2"' in response.text
        assert 'data-dropdown-select-contract="v1"' in response.text
        assert 'data-testid="story-arcs-results-body"' in response.text
        assert 'data-testid="story-arcs-mission-control-table"' in response.text
        assert 'data-testid="story-arcs-footer-dock"' in response.text
        assert 'data-testid="story-arcs-create-form"' not in response.text
        assert 'data-testid="story-arc-catalog-search"' not in response.text
        assert f'data-story-arc-id="{ids["absolute"]}"' in response.text
        assert "Absolute Power" in response.text
        assert "House of Brainiac" not in response.text
        assert "Absolute Universe" not in response.text
        assert re.search(r'data-testid="story-arc-review-count"[^>]*>1<', response.text)
        assert "1 result" in response.text

        oversized = await authenticated_client.get("/story-arcs?per_page=101")
        assert oversized.status_code == 422

    async def test_registry_matches_series_owned_read_acquisition_review_and_action_contract(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        sec_user,
        tmp_path: Path,
    ) -> None:  # type: ignore[no-untyped-def]
        arc_id = await _seed_registry_metrics_arc(
            sec_db,
            user_id=sec_user.id,
            root_path=tmp_path,
        )

        response = await authenticated_client.get("/story-arcs?q=Registry")

        assert response.status_code == 200
        assert '<th class="c" style="width: 92px">Status</th>' in response.text
        assert '<th class="c" style="width: 78px">Owned</th>' in response.text
        assert '<th style="width: 220px">Acquisition</th>' in response.text
        assert "Needs Review" in response.text
        assert re.search(r'data-testid="story-arc-owned-count"[^>]*>1/3<', response.text)
        assert 'data-testid="story-arc-list-reading"' in response.text
        assert "Read 1/1" in response.text
        assert re.search(r'data-testid="story-arc-acquisition"[^>]*>33%<', response.text)
        assert re.search(r'data-testid="story-arc-review-count"[^>]*>2<', response.text)
        assert "1 ambiguous" in response.text
        assert "1 placement problem" in response.text
        assert f'action="/story-arcs/{arc_id}/search"' in response.text
        assert 'data-tip="Search missing"' in response.text
        assert f'href="/story-arcs/{arc_id}"' in response.text
        assert "DC Comics" in response.text

    async def test_registry_grid_view_persists_and_uses_story_arc_cover_contract(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        sec_user,
        tmp_path: Path,
    ) -> None:  # type: ignore[no-untyped-def]
        arc_id = await _seed_registry_metrics_arc(
            sec_db,
            user_id=sec_user.id,
            root_path=tmp_path,
        )

        response = await authenticated_client.get(
            "/story-arcs",
            params={
                "q": "Registry",
                "lifecycle": "",
                "monitored": "",
                "per_page": "25",
                "view_mode": "grid",
            },
        )

        assert response.status_code == 200
        assert response.cookies.get("story_arc_view") == "grid"
        assert 'data-testid="story-arcs-view-toggle"' in response.text
        assert 'data-testid="story-arcs-collector-wall-view"' in response.text
        assert 'data-testid="story-arcs-mission-control-view"' not in response.text
        assert f'data-story-arc-id="{arc_id}"' in response.text
        assert 'data-testid="story-arc-grid-card"' in response.text
        assert f'src="/api/v1/story-arcs/{arc_id}/cover?v=' in response.text
        assert 'data-testid="story-arc-grid-reading"' in response.text
        assert "Read 1/1" in response.text
        assert 'data-testid="story-arc-grid-review"' in response.text
        assert "Search" in response.text
        assert "Open" in response.text

        authenticated_client.cookies.set("story_arc_view", "grid")
        persisted = await authenticated_client.get("/story-arcs?q=Registry")
        assert 'data-testid="story-arcs-collector-wall-view"' in persisted.text

    async def test_manual_creation_is_hidden_and_unreachable_when_feature_flag_is_off(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            story_arc_routes,
            "get_settings",
            lambda: SimpleNamespace(story_arc_manual_create_enabled=False),
            raising=False,
        )

        page = await authenticated_client.get("/story-arcs/add")

        assert page.status_code == 200
        assert 'data-testid="story-arc-add-page"' in page.text
        assert 'data-testid="story-arc-catalog-search"' in page.text
        assert 'data-testid="story-arcs-create-form"' not in page.text

        response = await authenticated_client.post(
            "/story-arcs",
            data={"name": "Hidden empty arc"},
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )

        assert response.status_code == 404
        async with sec_db() as session:
            assert (
                await session.scalar(select(StoryArc.id).where(StoryArc.name == "Hidden empty arc"))
                is None
            )

    @pytest.mark.parametrize(
        ("prefix", "expected_template"),
        [(False, "{OriginalFilename}"), (True, "{ReadingOrder:02d} - {OriginalFilename}")],
    )
    async def test_create_arc_selects_source_preserving_copy_policy(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        prefix: bool,
        expected_template: str,
    ) -> None:
        async with sec_db() as session:
            root = LibraryRoot(name="Arc library", path=str(tmp_path), enabled=True)
            session.add(root)
            await session.commit()
            root_id = root.id
        destination = tmp_path / "Story Arcs"
        destination.mkdir()
        form = {
            "name": "New copy arc",
            "mode": "copy",
            "target_library_root_id": str(root_id),
            "destination_root": str(destination),
            "filename_style": "original",
            "reading_order_width": "2",
            "synchronize": "true",
        }
        if prefix:
            form["prefix_reading_order"] = "true"
        response = await authenticated_client.post(
            "/story-arcs",
            data=form,
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert response.status_code == 303
        async with sec_db() as session:
            arc = (
                await session.scalars(select(StoryArc).where(StoryArc.name == form["name"]))
            ).one()
            assert arc.target_library_root_id == root_id
            assert arc.policy_snapshot["mode"] == "copy"
            assert arc.policy_snapshot["file_template"] == expected_template
            assert arc.policy_snapshot["destination_root"] == str(destination)
            assert arc.sync_enabled is True
            assert (await session.scalars(select(StoryArcPlacement))).all() == []
        assert list(destination.iterdir()) == []

        page = await authenticated_client.get("/story-arcs/add")
        assert 'data-testid="story-arc-create-storage"' in page.text
        assert 'name="prefix_reading_order"' in page.text
        assert 'name="filename_style"' in page.text
        assert "Original files stay in their series folders" in page.text

    async def test_invalid_create_copy_destination_does_not_leave_an_arc(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        root_path = tmp_path / "library"
        root_path.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        async with sec_db() as session:
            root = LibraryRoot(name="Arc library", path=str(root_path), enabled=True)
            session.add(root)
            await session.commit()
            root_id = root.id
        response = await authenticated_client.post(
            "/story-arcs",
            data={
                "name": "Invalid copy arc",
                "mode": "copy",
                "target_library_root_id": str(root_id),
                "destination_root": str(outside),
            },
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        async with sec_db() as session:
            assert (
                await session.scalar(select(StoryArc.id).where(StoryArc.name == "Invalid copy arc"))
                is None
            )
        assert list(outside.iterdir()) == []

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
        assert 'data-testid="story-arc-detail-back-link"' in response.text
        assert 'data-testid="story-arc-detail-breadcrumbs"' in response.text
        assert "Back to story arcs" in response.text
        assert "All Story Arcs" in response.text
        assert "DC Numbering" in response.text
        assert 'data-exact-issue-number="1000000"' in response.text
        assert 'data-exact-issue-number="1AU"' in response.text
        assert f'data-membership-id="{ids["million_membership"]}"' in response.text
        assert f'data-membership-id="{ids["annual_membership"]}"' in response.text
        assert f'data-membership-id="{ids["fractional_membership"]}"' not in response.text
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
        assert (
            f"/issues/{ids['million_issue']}?source=story-arc&amp;story_arc_id={ids['arc']}"
            "&amp;story_arc_page=1&amp;story_arc_per_page=2"
        ) in response.text

        issue_from_arc = await authenticated_client.get(
            f"/issues/{ids['million_issue']}",
            params={
                "source": "story-arc",
                "story_arc_id": ids["arc"],
                "story_arc_page": 1,
                "story_arc_per_page": 2,
            },
        )
        assert issue_from_arc.status_code == 200
        assert 'data-testid="issue-detail-back-link"' in issue_from_arc.text
        assert 'data-testid="issue-detail-breadcrumbs"' in issue_from_arc.text
        assert 'data-breadcrumb-origin="story-arc"' in issue_from_arc.text
        assert "Back to story arc" in issue_from_arc.text
        assert "All Story Arcs" in issue_from_arc.text
        assert f'href="/story-arcs/{ids["arc"]}?page=1&amp;per_page=2"' in issue_from_arc.text
        assert "DC Numbering" in issue_from_arc.text

        invalid_arc_origin = await authenticated_client.get(
            f"/issues/{ids['million_issue']}",
            params={"source": "story-arc", "story_arc_id": ids["arc"] + 10_000},
        )
        assert invalid_arc_origin.status_code == 200
        assert 'data-breadcrumb-origin="series"' in invalid_arc_origin.text
        assert "Back to series" in invalid_arc_origin.text
        assert "All Story Arcs" not in invalid_arc_origin.text

        second_page = await authenticated_client.get(f"/story-arcs/{ids['arc']}?page=2&per_page=2")
        assert second_page.status_code == 200
        assert 'data-exact-issue-number="0.5"' in second_page.text
        assert f'data-membership-id="{ids["fractional_membership"]}"' in second_page.text
        assert f'data-membership-id="{ids["million_membership"]}"' not in second_page.text
        assert f'data-membership-id="{ids["annual_membership"]}"' not in second_page.text

    async def test_missing_entry_searches_bounded_local_issues_and_prefills_add_series(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = await _seed_detail_arc(sec_db)
        async with sec_db() as session:
            membership = await session.get(IssueStoryArc, ids["annual_membership"])
            assert membership is not None
            membership.source_series_name = "Batman Annual"
            membership.source_issue_title = "Annual Resolution Target"
            await session.commit()

        detail = await authenticated_client.get(f"/story-arcs/{ids['arc']}")

        assert detail.status_code == 200
        assert (
            f'data-testid="story-arc-local-issue-search-{ids["annual_membership"]}"' in detail.text
        )
        assert 'value="Batman Annual"' in detail.text
        assert 'href="/series/add?q=Batman+Annual"' in detail.text

        search = await authenticated_client.get(
            (f"/story-arcs/{ids['arc']}/memberships/{ids['annual_membership']}/local-issues"),
            params={"q": "Annual", "return_page": 1, "return_per_page": 25},
        )

        assert search.status_code == 200
        assert 'data-testid="story-arc-local-issue-results"' in search.text
        assert f'data-local-issue-id="{ids["annual_issue"]}"' in search.text
        assert "Batman Annual" in search.text
        assert "Annual Resolution Target" in search.text
        assert f'value="{ids["annual_issue"]}"' in search.text
        assert f'data-local-issue-id="{ids["million_issue"]}"' not in search.text
        assert "Showing 1 local match" in search.text

        oversized = await authenticated_client.get(
            (f"/story-arcs/{ids['arc']}/memberships/{ids['annual_membership']}/local-issues"),
            params={"q": "a" * 501},
        )
        assert oversized.status_code == 422

    async def test_detail_exposes_accessible_logical_and_separate_folder_policy_contract(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = await _seed_detail_arc(sec_db)

        response = await authenticated_client.get(f"/story-arcs/{ids['arc']}")

        assert response.status_code == 200
        assert 'data-testid="story-arc-placement-policy"' in response.text
        assert 'data-testid="story-arc-placement-policy-form"' in response.text
        assert "<legend" in response.text
        assert "Logical only" in response.text
        assert "Separate Story Arc folder" in response.text
        assert '<option value="logical" selected' in response.text
        assert '<option value="copy"' in response.text
        assert '<option value="hardlink"' in response.text
        assert '<option value="symlink"' in response.text
        assert '<option value="reference_only"' in response.text
        assert 'value="move"' not in response.text
        assert '<label for="story-arc-placement-root"' in response.text
        assert '<label for="story-arc-destination-root"' in response.text
        assert '<label for="story-arc-folder-template"' in response.text
        assert '<label for="story-arc-file-template"' in response.text
        assert '<label for="story-arc-symlink-style"' in response.text
        assert '<label for="story-arc-synchronize"' in response.text
        assert 'data-testid="story-arc-placement-preview"' in response.text
        assert 'data-classification="logical_only"' in response.text
        assert 'data-exact-issue-number="1000000"' in response.text
        assert 'scope="col">Effective target<' in response.text
        assert 'scope="col">Method<' in response.text
        assert 'scope="col">Ownership<' in response.text
        assert 'scope="col">Collision<' in response.text
        assert 'scope="col">Bytes<' in response.text
        assert "Canonical library files are never moved by Story Arc placement." in response.text
        assert (
            "Referenced artifacts remain user-owned and are never overwritten or deleted."
            in response.text
        )

        oversized_page = await authenticated_client.get(
            f"/story-arcs/{ids['arc']}?placement_page=999"
        )
        assert oversized_page.status_code == 200
        assert 'data-placement-page="1"' in oversized_page.text
        assert 'data-classification="logical_only"' in oversized_page.text

    async def test_policy_preview_is_write_free_then_save_freezes_complete_policy(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ids = await _seed_placement_arc(sec_db, tmp_path)
        form = _copy_policy_form(ids)

        preview = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/placement-policy/preview",
            data=form,
            headers=_csrf_header_for(authenticated_client),
        )

        assert preview.status_code == 200, preview.text
        assert 'data-testid="story-arc-placement-preview-only"' in preview.text
        assert "Preview only — no policy or files were changed." in preview.text
        assert '<option value="copy" selected' in preview.text
        assert f'value="{ids["destination"]}"' in preview.text
        assert 'data-classification="will_materialize"' in preview.text
        assert "001 - DC One Million 1000000 - The Final Hour.cbz" in preview.text
        assert "Copy" in preview.text
        assert "Managed" in preview.text
        assert "25 bytes" in preview.text

        async with sec_db() as session:
            arc = await session.get(StoryArc, int(ids["arc"]))
            assert arc is not None
            assert arc.revision == int(ids["revision"])
            assert arc.policy_schema_version is None
            assert arc.policy_snapshot == {}
            assert (await session.scalars(select(StoryArcPlacement))).all() == []

        saved = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/placement-policy",
            data=form,
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert saved.headers["location"].endswith("?notice=placement-policy-updated")

        async with sec_db() as session:
            arc = await session.get(StoryArc, int(ids["arc"]))
            assert arc is not None
            assert arc.revision == int(ids["revision"]) + 1
            assert arc.sync_enabled is True
            assert arc.policy_snapshot == {
                "schema_version": 1,
                "mode": "copy",
                "target_library_root_id": int(ids["root"]),
                "destination_root": str(ids["destination"]),
                "folder_template": "{StoryArc}",
                "file_template": (
                    "{ReadingOrder:03d} - {Series} {IssueNumber}{IssueTitleOptional}"
                ),
                "symlink_style": None,
                "synchronize": True,
            }

        detail = await authenticated_client.get(f"/story-arcs/{ids['arc']}")
        assert detail.status_code == 200
        assert "Separate Story Arc folder" in detail.text
        assert '<option value="copy" selected' in detail.text
        assert 'id="story-arc-synchronize"' in detail.text
        assert 'id="story-arc-synchronize" type="checkbox"' in detail.text
        assert "checked" in detail.text
        assert f'<option value="{ids["root"]}" selected' in detail.text
        assert "Approved Comics" in detail.text

    async def test_managed_placement_state_can_sync_and_repair_from_detail(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ids = await _seed_placement_arc(sec_db, tmp_path)
        placement_service = StoryArcPlacementSyncService()
        async with sec_db() as session:
            await placement_service.update_policy(
                session,
                int(ids["arc"]),
                expected_revision=int(ids["revision"]),
                proposal=StoryArcPlacementPolicyInput(
                    mode=StoryArcPlacementPolicyMode.COPY,
                    target_library_root_id=int(ids["root"]),
                    destination_root=str(ids["destination"]),
                    synchronize=True,
                ),
            )

        before = await authenticated_client.get(f"/story-arcs/{ids['arc']}")
        assert before.status_code == 200
        assert 'data-classification="will_materialize"' in before.text
        assert 'data-testid="story-arc-placement-sync-' in before.text

        synced = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/memberships/{ids['membership']}/placement-sync",
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert synced.status_code == 303
        assert synced.headers["location"].endswith("?notice=placement-synchronized")

        async with sec_db() as session:
            placement = (await session.scalars(select(StoryArcPlacement))).one()
            placement_id = placement.id
            target = Path(placement.placement_path)
        assert target.read_bytes() == Path(str(ids["canonical"])).read_bytes()

        current = await authenticated_client.get(f"/story-arcs/{ids['arc']}")
        assert current.status_code == 200
        assert 'data-testid="story-arc-placement-state"' in current.text
        assert f'data-placement-id="{placement_id}"' in current.text
        assert 'data-placement-state="current"' in current.text
        assert "Managed" in current.text
        assert 'data-classification="managed_current"' in current.text

        target.write_bytes(b"user replacement")
        drifted = await authenticated_client.get(f"/story-arcs/{ids['arc']}")
        assert drifted.status_code == 200
        assert 'data-classification="managed_drifted"' in drifted.text
        assert f'data-testid="story-arc-placement-repair-{placement_id}"' not in drifted.text
        assert target.read_bytes() == b"user replacement"

        target.unlink()
        missing = await authenticated_client.get(f"/story-arcs/{ids['arc']}")
        assert missing.status_code == 200
        assert 'data-classification="managed_missing"' in missing.text
        assert f'data-testid="story-arc-placement-repair-{placement_id}"' in missing.text
        assert "Canonical and referenced files will not be changed." in missing.text

        repaired = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/placements/{placement_id}/repair",
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert repaired.status_code == 303
        assert repaired.headers["location"].endswith("?notice=placement-repaired")
        assert target.read_bytes() == Path(str(ids["canonical"])).read_bytes()

        removable = await authenticated_client.get(f"/story-arcs/{ids['arc']}")
        assert removable.status_code == 200
        assert f'data-testid="story-arc-placement-remove-{placement_id}"' in removable.text
        assert 'name="confirm_managed_artifact_removal" value="true"' in removable.text
        assert "Only the Pullbox-managed Story Arc copy will be removed." in removable.text
        assert "The canonical library file will stay in place." in removable.text

        unconfirmed = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/placements/{placement_id}/remove",
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert unconfirmed.status_code == 303
        assert unconfirmed.headers["location"].endswith("?error=placement")
        assert target.exists()

        removed = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/placements/{placement_id}/remove",
            data={"confirm_managed_artifact_removal": "true"},
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert removed.status_code == 303
        assert removed.headers["location"].endswith("?notice=placement-managed-removed")
        assert not target.exists()
        assert Path(str(ids["canonical"])).read_bytes() == b"canonical story arc issue"
        async with sec_db() as session:
            assert await session.get(StoryArcPlacement, placement_id) is None

    async def test_representation_only_symlink_drift_has_no_repair_action(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ids = await _seed_placement_arc(sec_db, tmp_path)
        placement_service = StoryArcPlacementSyncService()
        async with sec_db() as session:
            await placement_service.update_policy(
                session,
                int(ids["arc"]),
                expected_revision=int(ids["revision"]),
                proposal=StoryArcPlacementPolicyInput(
                    mode=StoryArcPlacementPolicyMode.SYMLINK,
                    target_library_root_id=int(ids["root"]),
                    destination_root=str(ids["destination"]),
                    symlink_style=StoryArcSymlinkStyle.RELATIVE,
                    synchronize=True,
                ),
            )
            synchronized = await placement_service.sync_membership(
                session,
                int(ids["arc"]),
                int(ids["membership"]),
            )
        assert synchronized.placement is not None
        target = Path(synchronized.placement.placement_path)
        target.unlink()
        target.symlink_to(Path(str(ids["canonical"])))
        assert target.readlink().is_absolute()

        response = await authenticated_client.get(f"/story-arcs/{ids['arc']}")

        assert response.status_code == 200
        assert 'data-classification="managed_drifted"' in response.text
        assert 'data-inspection-code="representation_changed"' in response.text
        assert (
            f'data-testid="story-arc-placement-repair-{synchronized.placement.id}"'
            not in response.text
        )
        assert "No write available" in response.text

    async def test_blocked_managed_removal_disables_durable_actions_and_explains_safety(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ids = await _seed_placement_arc(sec_db, tmp_path)
        placement_service = StoryArcPlacementSyncService()
        async with sec_db() as session:
            await placement_service.update_policy(
                session,
                int(ids["arc"]),
                expected_revision=int(ids["revision"]),
                proposal=StoryArcPlacementPolicyInput(
                    mode=StoryArcPlacementPolicyMode.COPY,
                    target_library_root_id=int(ids["root"]),
                    destination_root=str(ids["destination"]),
                    synchronize=True,
                ),
            )
            synchronized = await placement_service.sync_membership(
                session,
                int(ids["arc"]),
                int(ids["membership"]),
            )
        assert synchronized.placement is not None
        target = Path(synchronized.placement.placement_path)
        target.write_bytes(b"user replacement")

        blocked = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/placements/{synchronized.placement.id}/remove",
            data={"confirm_managed_artifact_removal": "true"},
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert blocked.status_code == 303
        assert blocked.headers["location"].endswith("?error=placement")

        response = await authenticated_client.get(f"/story-arcs/{ids['arc']}")

        assert response.status_code == 200
        assert f'data-placement-id="{synchronized.placement.id}"' in response.text
        assert 'data-placement-state="drifted"' in response.text
        assert 'data-testid="story-arc-placement-safety-blocked-' in response.text
        assert "no longer matches its recorded ownership evidence" in response.text
        assert (
            f'data-testid="story-arc-placement-retry-{synchronized.placement.id}"'
            not in response.text
        )
        assert (
            f'data-testid="story-arc-placement-remove-{synchronized.placement.id}"'
            not in response.text
        )
        assert target.read_bytes() == b"user replacement"
        assert Path(str(ids["canonical"])).read_bytes() == b"canonical story arc issue"

    async def test_untracked_identical_adoption_and_forget_preserve_user_artifact(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ids = await _seed_placement_arc(sec_db, tmp_path)
        placement_service = StoryArcPlacementSyncService()
        async with sec_db() as session:
            await placement_service.update_policy(
                session,
                int(ids["arc"]),
                expected_revision=int(ids["revision"]),
                proposal=StoryArcPlacementPolicyInput(
                    mode=StoryArcPlacementPolicyMode.COPY,
                    target_library_root_id=int(ids["root"]),
                    destination_root=str(ids["destination"]),
                    synchronize=True,
                ),
            )
        async with sec_db() as session:
            preview = await placement_service.preview_arc(
                session,
                int(ids["arc"]),
                limit=1,
                offset=0,
            )
        target = Path(preview.items[0].target_path or "")
        target.parent.mkdir(parents=True)
        target.write_bytes(Path(str(ids["canonical"])).read_bytes())

        untracked = await authenticated_client.get(f"/story-arcs/{ids['arc']}")
        assert untracked.status_code == 200
        assert 'data-classification="untracked_identical"' in untracked.text
        assert "User-owned (untracked)" in untracked.text
        assert f'data-testid="story-arc-placement-adopt-{ids["membership"]}"' in untracked.text

        adopted = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/memberships/{ids['membership']}/placement-sync",
            data={"adopt_identical_existing": "true"},
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert adopted.status_code == 303
        async with sec_db() as session:
            placement = (await session.scalars(select(StoryArcPlacement))).one()
            placement_id = placement.id
            assert placement.ownership is StoryArcPlacementOwnership.REFERENCED

        referenced = await authenticated_client.get(f"/story-arcs/{ids['arc']}")
        assert referenced.status_code == 200
        assert 'data-classification="referenced_current"' in referenced.text
        assert f'data-testid="story-arc-placement-forget-{placement_id}"' in referenced.text

        forgotten = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/placements/{placement_id}/remove",
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert forgotten.status_code == 303
        assert forgotten.headers["location"].endswith("?notice=placement-reference-forgotten")
        assert target.read_bytes() == Path(str(ids["canonical"])).read_bytes()
        async with sec_db() as session:
            assert await session.get(StoryArcPlacement, placement_id) is None

    async def test_detail_summarizes_durable_sync_work_with_one_bounded_aggregate(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ids = await _seed_placement_arc(sec_db, tmp_path)
        await _seed_sync_work_summary(sec_db, ids)

        response = await authenticated_client.get(f"/story-arcs/{ids['arc']}")

        assert response.status_code == 200
        assert 'data-testid="story-arc-sync-work-summary"' in response.text
        assert 'data-sync-state="queued">1<' in response.text
        assert 'data-sync-state="running">1<' in response.text
        assert 'data-sync-state="retry_wait">1<' in response.text
        assert 'data-sync-state="failed">1<' in response.text
        assert 'data-sync-state="completed">1<' in response.text
        assert 'data-sync-state="cancelled">0<' in response.text

    async def test_sync_work_summary_query_cardinality_is_constant_for_large_history(
        self,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ids = await _seed_placement_arc(sec_db, tmp_path)
        async with sec_db() as session:
            library_file = (await session.scalars(select(LibraryFile))).one()
            arc = await session.get(StoryArc, int(ids["arc"]))
            assert arc is not None
            states = tuple(StoryArcSyncWorkState)
            session.add_all(
                [
                    StoryArcSyncWork(
                        issue_story_arc_id=int(ids["membership"]),
                        library_file_id=library_file.id,
                        desired_generation=f"history-{index}",
                        source_signature_hash=f"{index:064x}",
                        source_file_path=library_file.file_path,
                        source_file_size=library_file.file_size,
                        source_file_modified_at=library_file.file_modified_at,
                        story_arc_revision=arc.revision,
                        membership_sequence=1,
                        policy_schema_version=1,
                        state=states[index % len(states)],
                    )
                    for index in range(250)
                ]
            )
            await session.commit()

        selects: list[str] = []

        def record_select(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)

        async_engine = sec_db.kw["bind"]
        event.listen(async_engine.sync_engine, "before_cursor_execute", record_select)
        try:
            async with sec_db() as session:
                summary = await _load_sync_work_summary(
                    session,
                    story_arc_id=int(ids["arc"]),
                )
        finally:
            event.remove(async_engine.sync_engine, "before_cursor_execute", record_select)

        assert summary.total == 250
        assert len(selects) == 1

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
        previewed = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/memberships/{ids['million_membership']}/move",
            data={"direction": "down", "expected_revision": revision},
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert previewed.status_code == 200
        assert 'data-testid="story-arc-reorder-preview"' in previewed.text
        assert "No reading order" in previewed.text
        assert "Logical order only" in previewed.text
        token_match = re.search(
            r'name="preview_token" value="([^"]+)"',
            previewed.text,
        )
        assert token_match is not None
        preview_token = token_match.group(1)

        async with sec_db() as session:
            arc = await session.get(StoryArc, ids["arc"])
            assert arc is not None and arc.revision == revision

        not_confirmed = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/memberships/{ids['million_membership']}/move",
            data={
                "direction": "down",
                "expected_revision": revision,
                "preview_token": preview_token,
            },
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert not_confirmed.status_code == 303
        assert not_confirmed.headers["location"].endswith("?error=reorder")

        moved = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/memberships/{ids['million_membership']}/move",
            data={
                "direction": "down",
                "expected_revision": revision,
                "preview_token": preview_token,
                "confirm_reorder": "true",
            },
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

    async def test_managed_move_requires_preview_then_renames_only_arc_placements(
        self,
        authenticated_client: AsyncClient,
        sec_db: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ids = await _seed_placement_arc(sec_db, tmp_path)
        second_canonical = tmp_path / "library" / "DC One Million 2.cbz"
        second_canonical.write_bytes(b"second canonical story arc issue")
        async with sec_db() as session:
            root = await session.get(LibraryRoot, int(ids["root"]))
            series = await session.scalar(select(Series).limit(1))
            assert root is not None and series is not None
            second_issue = Issue(
                series=series,
                issue_number=2,
                issue_number_text="2",
                title="The Second Hour",
                status=IssueStatus.OWNED,
            )
            session.add(
                LibraryFile(
                    issue=second_issue,
                    library_root=root,
                    file_path=str(second_canonical),
                    file_name=second_canonical.name,
                    file_size=second_canonical.stat().st_size,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.now(UTC),
                    match_confidence=MatchConfidence.MANUAL,
                )
            )
            await session.flush()
            second_membership = await StoryArcService().add_membership(
                session,
                int(ids["arc"]),
                issue_id=second_issue.id,
                sequence_number=2,
                source_issue_number_text="2",
            )
            await session.commit()
            arc = await session.get(StoryArc, int(ids["arc"]))
            assert arc is not None
            policy_revision = arc.revision

        placement_service = StoryArcPlacementSyncService()
        async with sec_db() as session:
            policy = await placement_service.update_policy(
                session,
                int(ids["arc"]),
                expected_revision=policy_revision,
                proposal=StoryArcPlacementPolicyInput(
                    mode=StoryArcPlacementPolicyMode.COPY,
                    target_library_root_id=int(ids["root"]),
                    destination_root=str(ids["destination"]),
                    folder_template="{StoryArc}",
                    file_template="{ReadingOrder:03d} - {Series} {IssueNumber}",
                    synchronize=True,
                ),
            )
            first_sync = await placement_service.sync_membership(
                session,
                int(ids["arc"]),
                int(ids["membership"]),
            )
            second_sync = await placement_service.sync_membership(
                session,
                int(ids["arc"]),
                second_membership.id,
            )
        assert first_sync.placement is not None
        assert second_sync.placement is not None
        old_targets = (
            Path(first_sync.placement.placement_path),
            Path(second_sync.placement.placement_path),
        )

        previewed = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/memberships/{ids['membership']}/move",
            data={"direction": "down", "expected_revision": policy.revision},
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert previewed.status_code == 200
        assert "Managed renames" in previewed.text
        assert ">2</dd>" in previewed.text
        assert "Durable recovery checkpoint path" in previewed.text
        assert "canonical file" in previewed.text
        token_match = re.search(
            r'name="preview_token" value="([^"]+)"',
            previewed.text,
        )
        assert token_match is not None
        preview_token = token_match.group(1)
        preview = StoryArcManagedReorderService().inspect_preview_token(
            story_arc_id=int(ids["arc"]),
            membership_id=int(ids["membership"]),
            direction="down",
            expected_revision=policy.revision,
            preview_token=preview_token,
        )
        new_targets = tuple(
            Path(str(item.new_path)) for item in preview.items if item.action == "rename"
        )
        assert len(new_targets) == 2
        assert [path.read_bytes() for path in old_targets] == [
            b"canonical story arc issue",
            b"second canonical story arc issue",
        ]

        # Simulate request/process loss immediately after the durable prepare
        # commit.  A normal fresh detail GET must rediscover the operation and
        # mint a usable recovery confirmation without the browser-held token.
        preparing_service = StoryArcManagedReorderService()
        async with sec_db() as session:
            await preparing_service._verify_and_prepare(
                session,
                preparing_service._decode_plan(preview_token),
            )
        recovered_page = await authenticated_client.get(f"/story-arcs/{ids['arc']}")
        assert recovered_page.status_code == 200
        assert "Reorder recovery" in recovered_page.text
        assert "interrupted request" in recovered_page.text
        assert "Retry recovery" in recovered_page.text
        recovered_token_match = re.search(
            r'name="preview_token" value="([^"]+)"',
            recovered_page.text,
        )
        assert recovered_token_match is not None
        preview_token = recovered_token_match.group(1)

        confirmed = await authenticated_client.post(
            f"/story-arcs/{ids['arc']}/memberships/{ids['membership']}/move",
            data={
                "direction": "down",
                "expected_revision": policy.revision,
                "preview_token": preview_token,
                "confirm_reorder": "true",
            },
            headers=_csrf_header_for(authenticated_client),
            follow_redirects=False,
        )
        assert confirmed.status_code == 303
        assert confirmed.headers["location"].endswith("?notice=moved-down")
        assert [path.read_bytes() for path in new_targets] == [
            b"canonical story arc issue",
            b"second canonical story arc issue",
        ]
        assert not any(path.exists() for path in old_targets)
        assert Path(str(ids["canonical"])).read_bytes() == b"canonical story arc issue"
        assert second_canonical.read_bytes() == b"second canonical story arc issue"
        assert not list(Path(str(ids["destination"])).rglob(".pullbox-story-arc-reorder-*"))

        async with sec_db() as session:
            ordered_ids = list(
                await session.scalars(
                    select(IssueStoryArc.id)
                    .where(IssueStoryArc.story_arc_id == int(ids["arc"]))
                    .order_by(
                        IssueStoryArc.sequence_number,
                        IssueStoryArc.source_ordinal,
                        IssueStoryArc.id,
                    )
                )
            )
            assert ordered_ids == [second_membership.id, int(ids["membership"])]
            placements = list((await session.scalars(select(StoryArcPlacement))).all())
            assert {row.placement_path for row in placements} == {str(path) for path in new_targets}

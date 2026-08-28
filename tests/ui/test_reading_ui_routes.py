"""Route contracts for the private Reading workspace."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.reader import IssueReaderState
from pullbox.models.series import Series

if TYPE_CHECKING:
    from pullbox.models.user import User

pytest_plugins = ["conftest_security"]


async def _seed_reading_items(
    factory,  # type: ignore[no-untyped-def]
    user: User,
    *,
    count: int,
    mode: str,
    unavailable_last: bool = False,
) -> list[int]:
    now = datetime.now(UTC)
    async with factory() as session:
        root = LibraryRoot(name=f"Reading {mode}", path=f"/reading-{mode}", enabled=True)
        series = Series(
            title=f"Reading {mode.title()}",
            sort_title=f"reading {mode}",
            year_start=2026,
            monitored=True,
            cover_url="https://example.test/series-cover.jpg",
            library_root=root,
        )
        session.add_all([root, series])
        await session.flush()
        issue_ids: list[int] = []
        for index in range(1, count + 1):
            issue = Issue(
                series_id=series.id,
                issue_number=float(index),
                title=f"Chapter {index}",
                status=IssueStatus.OWNED,
                cover_url=f"https://example.test/issue-{index}.jpg",
            )
            session.add(issue)
            await session.flush()
            issue_ids.append(issue.id)
            unavailable = unavailable_last and index == count
            if not unavailable:
                session.add(
                    LibraryFile(
                        file_path=f"/reading-{mode}/issue-{index}.cbz",
                        file_name=f"issue-{index}.cbz",
                        file_size=1024,
                        file_format=FileFormat.CBZ,
                        file_modified_at=now,
                        match_confidence=MatchConfidence.HIGH,
                        issue_id=issue.id,
                        library_root_id=root.id,
                    )
                )
            opened_at = now + timedelta(seconds=index)
            session.add(
                IssueReaderState(
                    user_id=user.id,
                    issue_id=issue.id,
                    last_page_index=1 if mode == "continue" else (4 if mode == "read" else None),
                    content_revision="revision" if mode in {"continue", "read"} else None,
                    page_count=5 if mode in {"continue", "read"} else None,
                    progress_updated_at=opened_at if mode in {"continue", "read"} else None,
                    last_opened_at=opened_at if mode in {"continue", "read"} else None,
                    completed_at=opened_at if mode == "read" else None,
                    completion_updated_at=opened_at if mode == "read" else None,
                    want_to_read=mode == "want",
                    want_to_read_updated_at=opened_at if mode == "want" else None,
                )
            )
        await session.commit()
        return issue_ids


@pytest.mark.asyncio
class TestReadingWorkspaceRoutes:
    async def test_reader_gate_disables_workspace_route(
        self,
        authenticated_client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.ui import reading_routes

        monkeypatch.setattr(
            reading_routes,
            "get_settings",
            lambda: SimpleNamespace(reader_enabled=False),
            raising=False,
        )

        response = await authenticated_client.get("/reading")

        assert response.status_code == 404
        assert 'data-testid="reading-card"' not in response.text
        assert 'data-reading-action="want-to-read"' not in response.text
        assert 'data-reading-action="completion"' not in response.text

    async def test_default_workspace_renders_continue_empty_contract_and_active_sidebar(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/reading")

        assert response.status_code == 200
        assert "<title>Reading — Pullbox</title>" in response.text
        assert 'data-testid="reading-page"' in response.text
        assert 'data-testid="reading-title"' in response.text
        assert (
            "Pick up a comic, choose what’s next, or revisit what you’ve read."  # noqa: RUF001
            in response.text
        )
        assert 'data-testid="reading-view-continue"' in response.text
        assert 'aria-current="page"' in response.text
        assert 'data-testid="reading-results"' in response.text
        assert 'data-testid="reading-empty-continue"' in response.text
        assert "Nothing to pick up yet." in response.text
        assert "Open a downloaded issue and Pullbox will save your place." in response.text
        assert 'data-testid="sidebar-link-reading"' in response.text
        assert 'data-nav-path="/reading"' in response.text
        assert 'hx-history="false"' in response.text

    async def test_workspace_normalizes_unknown_view_and_page_size(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/reading?view=surprise&per_page=25")

        assert response.status_code == 200
        assert 'data-reading-view="continue"' in response.text
        assert 'data-reading-per-page="24"' in response.text
        assert 'data-dropdown-value="24"' in response.text
        assert 'href="/reading?view=want-to-read&amp;per_page=24"' in response.text

    async def test_want_to_read_renders_readable_and_unavailable_cards(
        self,
        authenticated_client,
        sec_db,
        sec_user,
    ) -> None:  # type: ignore[no-untyped-def]
        issue_ids = await _seed_reading_items(
            sec_db,
            sec_user,
            count=2,
            mode="want",
            unavailable_last=True,
        )

        response = await authenticated_client.get("/reading?view=want-to-read")

        assert response.status_code == 200
        assert response.text.count('data-testid="reading-card"') == 2
        assert 'data-testid="reading-state-' in response.text
        assert "Not started" in response.text
        assert "File unavailable" in response.text
        assert ">Remove</button>" in response.text
        assert f'href="/issues/{issue_ids[-1]}"' in response.text
        assert 'data-reading-action="want-to-read"' in response.text
        assert 'data-reading-action="completion"' in response.text
        assert 'x-data="readingStateActions()"' in response.text
        assert "x-bind:aria-live=\"statusIsError ? 'assertive' : 'polite'\"" in response.text

    async def test_issue_card_state_actions_use_shared_button_contract(
        self,
        authenticated_client,
        sec_db,
        sec_user,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_reading_items(sec_db, sec_user, count=1, mode="want")

        response = await authenticated_client.get("/reading?view=want-to-read")

        assert response.status_code == 200
        completion_start = response.text.index('data-reading-action="completion"')
        completion_markup = response.text[completion_start : completion_start + 180]
        queue_start = response.text.index('data-reading-action="want-to-read"')
        queue_markup = response.text[queue_start : queue_start + 220]
        assert 'class="btn-primary btn-sm reading-card-primary-action"' in response.text
        assert 'class="btn-ghost btn-sm reading-card-completion-action"' in completion_markup
        assert 'class="btn-ghost btn-sm reading-card-queue-action"' in queue_markup
        assert "btn-secondary" not in response.text

    async def test_view_context_controls_queue_and_reread_actions(
        self,
        authenticated_client,
        sec_db,
        sec_user,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_reading_items(sec_db, sec_user, count=1, mode="continue")
        await _seed_reading_items(sec_db, sec_user, count=1, mode="want")
        await _seed_reading_items(sec_db, sec_user, count=1, mode="read")

        continue_response = await authenticated_client.get("/reading?view=continue")
        want_response = await authenticated_client.get("/reading?view=want-to-read")
        read_response = await authenticated_client.get("/reading?view=read")

        assert ">Want to Read</button>" in continue_response.text
        assert ">Remove</button>" in want_response.text
        assert ">Reread</a>" in read_response.text
        assert "reading-card-view-read" in read_response.text

    async def test_cards_reserve_queue_action_space_when_continue_item_is_already_queued(
        self,
        authenticated_client,
        sec_db,
        sec_user,
    ) -> None:  # type: ignore[no-untyped-def]
        issue_ids = await _seed_reading_items(sec_db, sec_user, count=1, mode="continue")
        async with sec_db() as session:
            state = (
                await session.execute(
                    select(IssueReaderState).where(
                        IssueReaderState.user_id == sec_user.id,
                        IssueReaderState.issue_id == issue_ids[0],
                    )
                )
            ).scalar_one()
            state.want_to_read = True
            state.want_to_read_updated_at = datetime.now(UTC)
            await session.commit()

        response = await authenticated_client.get("/reading?view=continue")

        assert response.status_code == 200
        assert 'class="reading-card-state-region"' in response.text
        assert 'class="reading-card-queue-action-placeholder"' in response.text
        assert 'data-reading-action="want-to-read"' not in response.text

    async def test_workspace_pagination_preserves_view_and_page_size(
        self,
        authenticated_client,
        sec_db,
        sec_user,
    ) -> None:  # type: ignore[no-untyped-def]
        await _seed_reading_items(sec_db, sec_user, count=25, mode="want")

        response = await authenticated_client.get("/reading?view=want-to-read&per_page=24")

        assert response.status_code == 200
        assert response.text.count('data-testid="reading-card"') == 24
        assert 'data-testid="reading-pagination"' in response.text
        assert "view=want-to-read&amp;per_page=24&amp;page=2" in response.text

    async def test_workspace_clamps_a_stale_page_after_its_last_item_is_removed(
        self,
        authenticated_client,
        sec_db,
        sec_user,
    ) -> None:  # type: ignore[no-untyped-def]
        issue_ids = await _seed_reading_items(sec_db, sec_user, count=25, mode="want")
        async with sec_db() as session:
            state = (
                await session.execute(
                    select(IssueReaderState).where(
                        IssueReaderState.user_id == sec_user.id,
                        IssueReaderState.issue_id == issue_ids[0],
                    )
                )
            ).scalar_one()
            state.want_to_read = False
            await session.commit()

        response = await authenticated_client.get(
            "/reading?view=want-to-read&per_page=24&page=2",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert response.text.count('data-testid="reading-card"') == 24
        assert 'data-reading-refresh-url="/reading?view=want-to-read&per_page=24&page=1"' in (
            response.text
        )

    async def test_htmx_workspace_request_returns_only_results_and_footer(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/reading?view=read",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'data-testid="reading-results"' in response.text
        assert 'id="page-footer-dock"' in response.text
        assert 'data-testid="reading-page"' not in response.text

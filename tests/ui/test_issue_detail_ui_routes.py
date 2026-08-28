"""Route-contract tests for the issue detail page rewrite."""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-issue-detail-ui-tests")


def _issue_detail_content_html(html: str) -> str:
    """Return page-specific content without shared shell recovery scripts."""
    start = html.index('data-testid="issue-detail-page"')
    end = html.index('data-testid="page-footer-dock"', start)
    return html[start:end]


@pytest.fixture
async def seeded_issue_detail_ui_data(sec_db) -> dict[str, int]:  # type: ignore[no-untyped-def]
    """Seed a small issue-detail dataset with both owned and wanted states."""
    from pullbox.models.creator import Creator, IssueCreator
    from pullbox.models.issue import Issue, IssueStatus
    from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
    from pullbox.models.publisher import Publisher
    from pullbox.models.series import Series, SeriesStatus

    async with sec_db() as session:
        publisher = Publisher(name="DC Comics")
        root = LibraryRoot(name="Issue UI Test Library", path="/tmp/issue-ui", enabled=True)
        session.add_all([publisher, root])
        await session.flush()

        series_path = Path("/tmp/issue-ui/batman")
        series_path.mkdir(parents=True, exist_ok=True)
        (series_path / "issue_001.png").write_bytes(
            bytes.fromhex(
                "89504E470D0A1A0A0000000D4948445200000001000000010804000000B51C0C020000000B49444154789C63FCFF1F00030302"
                "00EE7E07D70000000049454E44AE426082"
            )
        )

        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2016,
            status=SeriesStatus.CONTINUING,
            monitored=True,
            issue_count=2,
            publisher_id=publisher.id,
            library_root_id=root.id,
            path=str(series_path),
        )
        session.add(series)
        await session.flush()

        writer = Creator(name="Tom King")
        artist = Creator(name="David Finch")
        session.add_all([writer, artist])
        await session.flush()

        owned_issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            title="I Am Gotham",
            description="<p>Batman faces a new threat over Gotham.</p>",
            release_date=date(2016, 6, 1),
            store_date=date(2016, 5, 25),
            page_count=32,
            cover_path="/api/v1/issues/1/cover",
            comicvine_id=50001,
            comicvine_url="https://comicvine.gamespot.com/batman-1/4000-50001/",
            status=IssueStatus.OWNED,
        )
        wanted_issue = Issue(
            series_id=series.id,
            issue_number=2.0,
            title="Night of the Monster Men",
            release_date=date(2016, 7, 1),
            status=IssueStatus.WANTED,
        )
        skipped_issue = Issue(
            series_id=series.id,
            issue_number=3.0,
            title="The Many Deaths of the Batman",
            release_date=date(2016, 8, 1),
            status=IssueStatus.SKIPPED,
            manual_skip=True,
        )
        session.add_all([owned_issue, wanted_issue, skipped_issue])
        await session.flush()

        session.add_all(
            [
                IssueCreator(issue_id=owned_issue.id, creator_id=writer.id, role="Writer"),
                IssueCreator(issue_id=owned_issue.id, creator_id=artist.id, role="Artist"),
            ]
        )

        library_file = LibraryFile(
            issue_id=owned_issue.id,
            library_root_id=root.id,
            file_path="/tmp/issue-ui/batman/Batman's 001 (2016).cbz",
            file_name="Batman's 001 (2016).cbz",
            file_size=52_428_800,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime(2024, 1, 1, tzinfo=UTC),
            match_confidence=MatchConfidence.MANUAL,
        )
        session.add(library_file)
        await session.commit()

        return {
            "owned_issue_id": owned_issue.id,
            "wanted_issue_id": wanted_issue.id,
            "skipped_issue_id": skipped_issue.id,
            "series_id": series.id,
        }


@pytest.mark.asyncio
class TestIssueDetailRouteContracts:
    """Verify the server-side issue-detail rendering contract."""

    async def test_full_page_renders_stable_issue_shell(
        self,
        authenticated_client,
        seeded_issue_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            f"/issues/{seeded_issue_detail_ui_data['owned_issue_id']}"
        )

        assert response.status_code == 200
        assert 'data-testid="issue-detail-page"' in response.text
        assert 'hx-history="false"' in response.text
        assert 'data-testid="issue-detail-back-link"' in response.text
        assert 'data-testid="issue-detail-breadcrumbs"' in response.text
        assert 'data-testid="issue-detail-hero"' in response.text
        assert 'data-testid="issue-detail-cover-link"' in response.text
        assert 'data-testid="issue-detail-title"' in response.text
        assert 'data-testid="issue-detail-title-link"' in response.text
        assert 'href="https://comicvine.gamespot.com/batman-1/4000-50001/"' in response.text
        assert 'aria-label="Open Batman #1 on ComicVine"' in response.text
        assert 'target="_blank"' in response.text
        assert 'rel="noopener"' in response.text
        assert 'data-testid="issue-detail-status-row"' in response.text
        assert 'data-testid="issue-detail-hero-summary-panel"' in response.text
        assert 'data-testid="issue-detail-hero-actions-panel"' in response.text
        assert 'class="series-domain-actions-card"' not in response.text
        actions_index = response.text.index('data-testid="issue-detail-hero-actions-panel"')
        meta_grid_index = response.text.index('data-testid="issue-detail-meta-grid"')
        assert actions_index < meta_grid_index
        assert 'data-testid="issue-detail-meta-grid"' in response.text
        assert 'data-testid="issue-detail-stat-strip"' in response.text
        assert 'data-testid="issue-detail-actions"' in response.text
        assert 'data-testid="issue-detail-actions-title"' in response.text
        assert "Manage <span>issue</span>" in response.text
        assert "Manage <span>this issue</span>" not in response.text
        assert 'data-testid="issue-action-download"' in response.text
        assert 'data-testid="issue-action-read"' in response.text
        assert '@click="openReader($event)"' in response.text
        assert 'data-testid="comic-reader-dialog"' in response.text
        assert 'data-testid="comic-reader-viewport"' in response.text
        assert 'data-testid="comic-reader-page"' in response.text
        assert 'data-testid="comic-reader-close"' in response.text
        assert "readerManifestUrl:" in response.text
        assert 'data-testid="issue-action-import"' in response.text
        assert 'data-testid="issue-action-manual-search"' in response.text
        assert 'data-testid="issue-action-delete-file"' in response.text
        assert 'data-testid="issue-search-modal"' in response.text
        assert 'data-testid="issue-search-close"' in response.text
        assert 'data-testid="issue-search-modal-footer-close"' in response.text
        assert 'data-testid="issue-search-modal-stats"' in response.text
        assert 'data-testid="issue-search-modal-footer-meta"' in response.text
        assert 'data-testid="issue-description-section"' in response.text
        assert 'data-testid="issue-description-title"' in response.text
        assert 'data-testid="issue-creators-section"' in response.text
        assert 'data-testid="issue-creators-title"' in response.text
        assert 'data-testid="issue-creators-grid"' in response.text
        assert 'data-testid="issue-library-file-section"' in response.text
        assert 'data-testid="issue-library-file-title"' in response.text
        assert 'data-testid="issue-library-file-path-panel"' in response.text
        assert 'data-testid="issue-library-file-copy"' in response.text
        assert 'data-tip="Copy path"' in response.text
        assert 'data-testid="issue-detail-telemetry-strip"' not in response.text
        assert "navigator.clipboard.writeText(" in response.text
        assert "\\u0027" in response.text
        assert "navigator.clipboard.writeText(&#39;" not in response.text
        assert 'data-testid="header-donations-button"' in response.text
        assert 'data-testid="header-theme-toggle"' in response.text
        assert 'data-testid="live-updates-toggle"' not in response.text
        assert 'data-testid="page-footer-dock"' in response.text
        assert 'data-testid="app-footer"' not in response.text
        assert 'data-testid="page-dock-inner"' in response.text
        assert 'data-testid="page-dock-status"' in response.text
        assert "page-dock-inner page-dock-inner-status-only" in response.text
        assert 'data-testid="page-dock-pagination"' not in response.text
        assert "transition:true" not in response.text
        assert "window.location.reload()" not in _issue_detail_content_html(response.text)

    async def test_issue_detail_renders_private_progress_and_targeted_state_actions(
        self,
        authenticated_client,
        sec_db,
        sec_user,
        seeded_issue_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.reader import IssueReaderState

        now = datetime.now(UTC)
        issue_id = seeded_issue_detail_ui_data["owned_issue_id"]
        async with sec_db() as session:
            session.add(
                IssueReaderState(
                    user_id=sec_user.id,
                    issue_id=issue_id,
                    last_page_index=1,
                    content_revision="issue-detail-revision",
                    page_count=5,
                    progress_updated_at=now,
                    last_opened_at=now,
                    want_to_read=True,
                    want_to_read_updated_at=now,
                )
            )
            await session.commit()

        response = await authenticated_client.get(f"/issues/{issue_id}")

        assert response.status_code == 200
        assert 'data-testid="issue-reading-state"' in response.text
        assert re.search(
            r'data-testid="issue-reading-state"[^>]*>\s*Page 2 of 5\s*</span>',
            response.text,
        )
        assert 'data-testid="issue-action-read"' in response.text
        assert re.search(
            r'data-testid="issue-action-read"[\s\S]*?Continue\s*</button>',
            response.text,
        )
        assert 'data-testid="issue-action-want-to-read"' in response.text
        assert "In Want to Read" in response.text
        assert 'data-testid="issue-action-completion"' in response.text
        assert "Mark read" in response.text
        assert 'data-reading-refresh-root="issue-detail"' in response.text
        assert f'data-reading-refresh-url="/htmx/issues/{issue_id}/reading"' in response.text
        assert 'x-data="readingStateActions()"' in response.text

    async def test_issue_detail_omits_other_users_private_reading_state(
        self,
        authenticated_client,
        sec_db,
        seeded_issue_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.reader import IssueReaderState
        from pullbox.models.user import User
        from pullbox.services.auth_service import AuthService

        now = datetime.now(UTC)
        issue_id = seeded_issue_detail_ui_data["owned_issue_id"]
        async with sec_db() as session:
            other_user = User(
                username="other-reader",
                password_hash=AuthService.hash_password("Other@1234"),
            )
            session.add(other_user)
            await session.flush()
            session.add(
                IssueReaderState(
                    user_id=other_user.id,
                    issue_id=issue_id,
                    last_page_index=3,
                    content_revision="private-revision",
                    page_count=5,
                    progress_updated_at=now,
                    last_opened_at=now,
                )
            )
            await session.commit()

        response = await authenticated_client.get(f"/issues/{issue_id}")

        assert response.status_code == 200
        assert 'data-testid="issue-reading-state"' not in response.text
        assert "Page 4 of 5" not in response.text
        assert re.search(r'data-testid="issue-action-read"[\s\S]*?Read\s*</button>', response.text)

    async def test_read_query_deep_opens_only_for_allowlisted_value_and_readable_issue(
        self,
        authenticated_client,
        seeded_issue_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        issue_id = seeded_issue_detail_ui_data["owned_issue_id"]
        valid = await authenticated_client.get(f"/issues/{issue_id}?read=1")
        ignored = await authenticated_client.get(f"/issues/{issue_id}?read=yes")
        unavailable = await authenticated_client.get(
            f"/issues/{seeded_issue_detail_ui_data['wanted_issue_id']}?read=1"
        )

        assert valid.status_code == 200
        assert "openReaderOnLoad: true" in valid.text
        assert "openReaderOnLoad: false" in ignored.text
        assert "openReaderOnLoad: false" in unavailable.text
        assert 'data-testid="issue-reader-unavailable-message"' in unavailable.text
        assert "A readable file is not available for this issue." in unavailable.text

    async def test_issue_reading_fragment_uses_the_same_private_canonical_state(
        self,
        authenticated_client,
        sec_db,
        sec_user,
        seeded_issue_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.reader import IssueReaderState

        now = datetime.now(UTC)
        issue_id = seeded_issue_detail_ui_data["owned_issue_id"]
        async with sec_db() as session:
            session.add(
                IssueReaderState(
                    user_id=sec_user.id,
                    issue_id=issue_id,
                    completed_at=now,
                    completion_updated_at=now,
                    state_version=2,
                )
            )
            await session.commit()

        response = await authenticated_client.get(f"/htmx/issues/{issue_id}/reading")

        assert response.status_code == 200
        assert response.text.count('data-testid="issue-detail-hero"') == 1
        assert 'data-testid="issue-reading-state"' in response.text
        assert re.search(r'data-testid="issue-reading-state"[^>]*>\s*Read\s*</span>', response.text)
        assert re.search(
            r'data-testid="issue-action-read"[\s\S]*?Read again\s*</button>',
            response.text,
        )
        assert "Mark unread" in response.text

    async def test_wanted_issue_renders_import_action_without_library_file_section(
        self,
        authenticated_client,
        seeded_issue_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            f"/issues/{seeded_issue_detail_ui_data['wanted_issue_id']}"
        )

        assert response.status_code == 200
        assert 'data-testid="issue-detail-page"' in response.text
        assert 'data-testid="issue-action-import"' in response.text
        assert '@click="openImportFileBrowser()"' in response.text
        assert 'data-testid="issue-import-modal"' in response.text
        assert 'data-testid="issue-import-modal-title"' in response.text
        assert 'data-testid="issue-import-progress-bar"' in response.text
        assert 'data-testid="issue-import-progress-value"' in response.text
        assert 'data-testid="issue-import-cancel"' in response.text
        assert "cancelIssueImport()" in response.text
        import_modal_fragment = response.text[
            response.text.index('data-testid="issue-import-modal"') : response.text.index(
                'data-testid="file-browser-modal"'
            )
        ]
        assert "Close import dialog" not in import_modal_fragment
        assert "@keydown.escape.window" not in import_modal_fragment
        assert '@click.outside="closeImportModal()"' not in import_modal_fragment
        assert 'data-testid="file-browser-modal"' in response.text
        assert 'data-testid="file-browser-title"' in response.text
        assert 'data-testid="issue-action-search"' in response.text
        assert 'data-testid="issue-action-read"' not in response.text
        assert 'data-testid="issue-library-file-section"' not in response.text

    async def test_emergency_reader_gate_hides_the_entry_point(
        self,
        authenticated_client,
        seeded_issue_detail_ui_data,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        from pullbox.ui import series_detail_routes

        monkeypatch.setattr(
            series_detail_routes,
            "get_settings",
            lambda: SimpleNamespace(reader_enabled=False),
        )

        response = await authenticated_client.get(
            f"/issues/{seeded_issue_detail_ui_data['owned_issue_id']}"
        )

        assert response.status_code == 200
        assert 'data-testid="issue-action-read"' not in response.text
        assert 'data-testid="issue-action-want-to-read"' not in response.text
        assert 'data-testid="issue-action-completion"' not in response.text
        assert 'data-testid="comic-reader-dialog"' not in response.text

    async def test_missing_issue_metadata_uses_persistent_comicvine_cache(
        self,
        authenticated_client,
        sec_db,
        seeded_issue_detail_ui_data,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.issue import Issue
        from pullbox.providers.base import IssueMetadata
        from pullbox.services.comicvine_persistent_cache import PersistentComicVineCacheProvider

        issue_id = seeded_issue_detail_ui_data["owned_issue_id"]
        async with sec_db() as session:
            issue = await session.get(Issue, issue_id)
            assert issue is not None
            issue.description = None
            issue.comicvine_url = None
            await session.commit()

        class _CachedIssueProvider:
            async def get_issue(self, issue_provider_id: str) -> IssueMetadata:
                return IssueMetadata(
                    provider_id=issue_provider_id,
                    series_provider_id="123",
                    issue_number=1.0,
                    title="I Am Gotham",
                    description="<p>Cached ComicVine description.</p>",
                    release_date="2016-06-01",
                    store_date="2016-05-25",
                    cover_url="https://example.test/issue.jpg",
                    page_count=32,
                    comicvine_url="https://comicvine.gamespot.com/batman-1/4000-50001/",
                )

        cache = PersistentComicVineCacheProvider(_CachedIssueProvider(), sec_db)
        await cache.get_issue("50001")

        async def _unexpected_raw_fetch(*_args: object, **_kwargs: object) -> IssueMetadata:
            raise AssertionError("raw ComicVine issue fetch should not run on cache hit")

        monkeypatch.setattr(
            "pullbox.providers.metadata.comicvine.ComicVineProvider.get_issue",
            _unexpected_raw_fetch,
        )

        response = await authenticated_client.get(f"/issues/{issue_id}")

        assert response.status_code == 200
        assert "Cached ComicVine description." in response.text

    async def test_missing_issue_redirects_back_to_series_index(
        self,
        authenticated_client,
        seeded_issue_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/issues/999999", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"] == "/series"

    async def test_toggle_route_can_render_skipped_issue_row(
        self,
        authenticated_client,
        seeded_issue_detail_ui_data,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.api.middleware import SESSION_COOKIE_NAME
        from pullbox.services.auth_service import AuthService

        session_token = authenticated_client.cookies.get(SESSION_COOKIE_NAME)
        assert session_token

        response = await authenticated_client.post(
            f"/htmx/issues/{seeded_issue_detail_ui_data['skipped_issue_id']}/toggle",
            headers={"x-csrf-token": AuthService.get_csrf_token_from_session(session_token) or ""},
        )

        assert response.status_code == 200
        assert 'data-testid="series-issue-row"' in response.text
        assert 'data-testid="series-issue-manual-search"' in response.text
        assert "Batman" in response.text
        assert "2016" in response.text
        assert "Wanted" in response.text

        detail_response = await authenticated_client.get(
            f"/issues/{seeded_issue_detail_ui_data['skipped_issue_id']}"
        )

        assert detail_response.status_code == 200
        assert 'data-testid="issue-detail-status-row"' in detail_response.text
        assert "Mark Skipped" in detail_response.text
        assert "Wanted" in detail_response.text

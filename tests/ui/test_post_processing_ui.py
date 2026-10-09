"""Behavior and query-contract tests for the tabbed post-processing page."""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.models.user import APIKey, User
from pullbox.services.auth_service import AuthService

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-post-processing-ui")


@pytest.fixture
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    from pullbox.tasks.post_processing_progress import _post_processing_cache

    # Each fresh database reuses IDs; progress from other tests must not hide its rows.
    _post_processing_cache.clear()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        _post_processing_cache.clear()
        await engine.dispose()


@pytest.fixture
async def _api_key_header(
    _db_factory: async_sessionmaker[AsyncSession],
) -> str:
    raw_key = "pb_k1_" + "p" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="ppuiuser",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="pp-ui-test"))
        await session.commit()
    return raw_key


@pytest.fixture
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
    _api_key_header: str,
) -> AsyncGenerator[AsyncClient, None]:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()
    # Keep setup/auth middleware on the same in-memory database as the route deps.
    app.state.db_session_factory = _db_factory

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with _db_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_dep] = _override_db
    reset_setup_cache()

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-Api-Key": _api_key_header},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


async def _ensure_issue(
    factory: async_sessionmaker[AsyncSession],
    *,
    series_title: str = "Test Series",
    sort_title: str | None = None,
    issue_number: float = 1.0,
) -> int:
    """Create a series + issue and return the issue id."""
    async with factory() as session:
        series = Series(title=series_title, sort_title=sort_title or series_title.lower())
        session.add(series)
        await session.flush()
        issue = Issue(series_id=series.id, issue_number=issue_number)
        session.add(issue)
        await session.flush()
        issue_id = issue.id
        await session.commit()
        return issue_id


async def _seed_download(
    factory: async_sessionmaker[AsyncSession],
    issue_id: int,
    state: DownloadState,
    *,
    title: str = "Test Release.cbz",
    downloaded_path: str | None = None,
    final_path: str | None = None,
    error_message: str | None = None,
    file_size: int | None = None,
    imported_at: datetime | None = None,
    completed_at: datetime | None = None,
    updated_at: datetime | None = None,
    download_client: DownloadClientType = DownloadClientType.SABNZBD,
) -> int:
    """Insert a download row using the current post-processing semantics."""
    if state == DownloadState.POST_PROCESSING:
        state = DownloadState.COMPLETED
    elif state == DownloadState.IMPORTED:
        state = DownloadState.COMPLETED
        if imported_at is None:
            imported_at = datetime.now(tz=UTC)

    if downloaded_path is None and state == DownloadState.COMPLETED:
        downloaded_path = f"/data/downloads/{title}"
    if final_path is None and imported_at is not None:
        final_path = f"/data/library/{title}"
    if completed_at is None and state in {DownloadState.COMPLETED, DownloadState.FAILED}:
        completed_at = datetime.now(tz=UTC)
    if updated_at is None:
        updated_at = completed_at or imported_at or datetime.now(tz=UTC)

    async with factory() as session:
        dl = DownloadHistory(
            title=title,
            state=state,
            download_client=download_client,
            download_url="https://example.com/dl/test",
            external_id=f"ext-{title}",
            issue_id=issue_id,
            downloaded_path=downloaded_path,
            final_path=final_path,
            error_message=error_message,
            file_size=file_size,
            imported_at=imported_at,
            completed_at=completed_at,
            updated_at=updated_at,
        )
        session.add(dl)
        await session.flush()
        dl_id = dl.id
        await session.commit()
        return dl_id


async def _seed_post_processing_dataset(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, str]:
    """Seed a small mixed post-processing dataset used by multiple tests."""
    batman_12 = await _ensure_issue(
        factory,
        series_title="Batman",
        sort_title="batman",
        issue_number=12.0,
    )
    action_50 = await _ensure_issue(
        factory,
        series_title="Action Comics",
        sort_title="action comics",
        issue_number=50.0,
    )
    detective_3 = await _ensure_issue(
        factory,
        series_title="Detective Comics",
        sort_title="detective comics",
        issue_number=3.0,
    )

    rows = {
        "imported_batman": "Deluxe Release.cbz",
        "failed_action": "Archive Edition.cbz",
        "imported_detective": "Collector Vault.cbz",
        "active_detective": "In Flight Transfer.cbz",
    }

    await _seed_download(
        factory,
        batman_12,
        DownloadState.IMPORTED,
        title=rows["imported_batman"],
        file_size=52_428_800,
        download_client=DownloadClientType.SABNZBD,
        completed_at=datetime(2026, 4, 3, 18, 0, tzinfo=UTC),
        imported_at=datetime(2026, 4, 3, 18, 10, tzinfo=UTC),
        updated_at=datetime(2026, 4, 3, 18, 10, tzinfo=UTC),
        final_path="/data/library/Batman/Deluxe Release.cbz",
    )
    await _seed_download(
        factory,
        action_50,
        DownloadState.FAILED,
        title=rows["failed_action"],
        file_size=83_886_080,
        download_client=DownloadClientType.TRANSMISSION,
        completed_at=datetime(2026, 4, 3, 19, 0, tzinfo=UTC),
        updated_at=datetime(2026, 4, 3, 19, 15, tzinfo=UTC),
        downloaded_path="/data/downloads/Action Comics/Archive Edition.cbz",
        error_message="Move failed: disk full",
    )
    await _seed_download(
        factory,
        detective_3,
        DownloadState.IMPORTED,
        title=rows["imported_detective"],
        file_size=31_457_280,
        download_client=DownloadClientType.NZBGET,
        completed_at=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
        imported_at=datetime(2026, 4, 2, 10, 30, tzinfo=UTC),
        updated_at=datetime(2026, 4, 2, 10, 30, tzinfo=UTC),
        final_path="/data/library/Detective Comics/Collector Vault.cbz",
    )
    await _seed_download(
        factory,
        detective_3,
        DownloadState.POST_PROCESSING,
        title=rows["active_detective"],
        file_size=41_943_040,
        download_client=DownloadClientType.QBITTORRENT,
        completed_at=datetime(2026, 4, 3, 20, 0, tzinfo=UTC),
        updated_at=datetime(2026, 4, 3, 20, 5, tzinfo=UTC),
        downloaded_path="/data/downloads/Detective/In Flight Transfer.cbz",
    )
    return rows


@pytest.mark.asyncio
class TestPostProcessingPage:
    """Server-rendered behaviors for the tabbed post-processing page."""

    async def test_page_loads_standardized_shell(self, client: AsyncClient) -> None:
        response = await client.get("/post-processing")

        assert response.status_code == 200
        assert "Post-Processing" in response.text
        assert 'data-testid="post-processing-shell"' in response.text
        assert 'data-testid="post-processing-body"' in response.text
        assert 'data-testid="post-processing-tabs"' in response.text
        assert 'data-testid="post-processing-content"' in response.text
        assert 'data-testid="pp-queue-panel"' in response.text
        assert 'data-testid="post-processing-header"' in response.text
        assert 'data-testid="pp-gauges"' in response.text
        assert 'data-testid="pp-footer-dock"' in response.text
        assert 'data-testid="pp-queue-active-section"' in response.text
        assert 'data-testid="pp-queue-imported-section"' in response.text
        assert 'data-testid="pp-summary-cards"' not in response.text

    async def test_empty_queue_renders_intentional_empty_card(self, client: AsyncClient) -> None:
        response = await client.get("/post-processing")

        assert response.status_code == 200
        assert 'data-testid="pp-queue-panel"' in response.text
        assert 'data-testid="pp-queue-item"' not in response.text
        assert 'data-testid="pp-queue-empty"' in response.text
        assert 'data-testid="pp-queue-imported-empty"' in response.text
        assert "No active imports" in response.text

    async def test_query_params_and_legacy_aliases_are_accepted(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_post_processing_dataset(_db_factory)

        response = await client.get(
            "/post-processing?tab=history&result=failed&client=transmission&search=Archive&sort=title&page=1"
        )
        assert response.status_code == 200
        assert 'name="sort" value="title"' in response.text
        assert 'data-dropdown-value="transmission"' in response.text
        assert "Archive Edition.cbz" in response.text

        legacy = await client.get("/post-processing?tab=history&filter=failed")
        assert legacy.status_code == 200
        assert 'data-dropdown-value="failed"' in legacy.text

        legacy_active = await client.get("/post-processing?tab=history&filter=active")
        assert legacy_active.status_code == 200
        assert 'data-dropdown-value="all"' in legacy_active.text

        invalid_tab = await client.get("/post-processing?tab=unknown")
        assert invalid_tab.status_code == 200
        assert 'data-testid="pp-queue-panel"' in invalid_tab.text
        assert 'data-testid="pp-history-panel"' not in invalid_tab.text

    async def test_queue_items_render_only_when_active_items_exist(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        rows = await _seed_post_processing_dataset(_db_factory)

        response = await client.get("/htmx/post-processing/queue", headers={"HX-Request": "true"})

        assert response.status_code == 200
        assert 'data-testid="pp-queue-item"' in response.text
        assert 'data-testid="pp-queue-active-table"' in response.text
        assert rows["active_detective"] in response.text
        assert 'data-testid="pp-queue-item-details-toggle"' in response.text
        assert 'data-testid="pp-queue-item-detail-placeholder"' in response.text
        assert 'data-testid="pp-queue-item-detail-content"' not in response.text
        assert "/data/downloads/Detective/In Flight Transfer.cbz" not in response.text
        assert (
            'hx-trigger="every 2s [window.postProcessingQueueRefreshEnabled()], '
            'post-processing:refresh from:body"'
        ) in response.text
        assert 'data-testid="pp-queue-empty"' not in response.text

    async def test_active_items_are_excluded_from_history(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        rows = await _seed_post_processing_dataset(_db_factory)

        history = await client.get(
            "/htmx/post-processing/history",
            headers={"HX-Request": "true"},
        )

        assert history.status_code == 200
        assert rows["active_detective"] not in history.text
        assert rows["imported_batman"] in history.text
        assert rows["failed_action"] in history.text

    async def test_result_filter_shows_failed_only(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        rows = await _seed_post_processing_dataset(_db_factory)

        response = await client.get("/htmx/post-processing/history?result=failed")

        assert response.status_code == 200
        assert rows["failed_action"] in response.text
        assert rows["imported_batman"] not in response.text
        assert rows["imported_detective"] not in response.text

    async def test_result_filter_shows_imported_only(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        rows = await _seed_post_processing_dataset(_db_factory)

        response = await client.get("/htmx/post-processing/history?result=imported")

        assert response.status_code == 200
        assert rows["imported_batman"] in response.text
        assert rows["imported_detective"] in response.text
        assert rows["failed_action"] not in response.text

    async def test_search_matches_title_series_and_issue(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        rows = await _seed_post_processing_dataset(_db_factory)

        by_title = await client.get("/htmx/post-processing/history?search=Archive")
        assert by_title.status_code == 200
        assert rows["failed_action"] in by_title.text
        assert rows["imported_batman"] not in by_title.text

        by_series = await client.get("/htmx/post-processing/history?search=Batman")
        assert by_series.status_code == 200
        assert rows["imported_batman"] in by_series.text
        assert rows["failed_action"] not in by_series.text

        by_issue = await client.get("/htmx/post-processing/history?search=50")
        assert by_issue.status_code == 200
        assert rows["failed_action"] in by_issue.text
        assert rows["imported_batman"] not in by_issue.text

    async def test_client_filter_shows_only_matching_history(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        rows = await _seed_post_processing_dataset(_db_factory)

        response = await client.get("/htmx/post-processing/history?client=transmission")

        assert response.status_code == 200
        assert rows["failed_action"] in response.text
        assert rows["imported_batman"] not in response.text
        assert rows["imported_detective"] not in response.text

    async def test_default_history_sort_uses_effective_completed_timestamp(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        rows = await _seed_post_processing_dataset(_db_factory)

        response = await client.get("/htmx/post-processing/history")
        html = response.text

        assert response.status_code == 200
        assert (
            html.index(rows["failed_action"])
            < html.index(rows["imported_batman"])
            < html.index(rows["imported_detective"])
        )

    async def test_history_supports_sorting_by_table_columns(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        rows = await _seed_post_processing_dataset(_db_factory)

        title_response = await client.get("/htmx/post-processing/history?sort=title")
        title_html = title_response.text
        assert (
            title_html.index(rows["failed_action"])
            < title_html.index(rows["imported_detective"])
            < title_html.index(rows["imported_batman"])
        )

        issue_response = await client.get("/htmx/post-processing/history?sort=issue")
        issue_html = issue_response.text
        assert (
            issue_html.index("Action Comics #50")
            < issue_html.index("Batman #12")
            < issue_html.index("Detective Comics #3")
        )

        result_response = await client.get("/htmx/post-processing/history?sort=result")
        result_html = result_response.text
        assert result_html.index(rows["imported_batman"]) < result_html.index(rows["failed_action"])

        client_response = await client.get("/htmx/post-processing/history?sort=client")
        client_html = client_response.text
        assert (
            client_html.index(rows["imported_detective"])
            < client_html.index(rows["imported_batman"])
            < client_html.index(rows["failed_action"])
        )

        size_response = await client.get("/htmx/post-processing/history?sort=-size")
        size_html = size_response.text
        assert (
            size_html.index(rows["failed_action"])
            < size_html.index(rows["imported_batman"])
            < size_html.index(rows["imported_detective"])
        )

    async def test_failed_rows_render_expandable_error_detail(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        rows = await _seed_post_processing_dataset(_db_factory)

        response = await client.get("/htmx/post-processing/history?result=failed")

        assert response.status_code == 200
        assert rows["failed_action"] in response.text
        assert "Move failed: disk full" in response.text
        assert "Processing Failed" in response.text
        assert 'data-testid="pp-history-block-' in response.text
        assert 'data-testid="pp-history-retry-' in response.text
        assert 'data-testid="pp-history-remove-' in response.text
        assert 'data-testid="pp-history-error-detail-' in response.text
        assert 'class="downloads-error-row table-detail-row"' in response.text
        assert 'class="downloads-error-content"' in response.text

    async def test_history_toolbar_uses_processing_failed_label(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_post_processing_dataset(_db_factory)

        response = await client.get("/post-processing?tab=history")

        assert response.status_code == 200
        assert "Processing Failed" in response.text

    async def test_imported_rows_render_release_title_without_secondary_path_text(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        rows = await _seed_post_processing_dataset(_db_factory)

        response = await client.get("/htmx/post-processing/history?result=imported")

        assert response.status_code == 200
        assert rows["imported_batman"] in response.text
        assert "/data/library/Batman/Deluxe Release.cbz" not in response.text

    async def test_history_empty_state_respects_filters(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_post_processing_dataset(_db_factory)

        response = await client.get("/htmx/post-processing/history?result=failed&search=Missing")

        assert response.status_code == 200
        assert 'data-testid="pp-history-empty"' in response.text
        assert "Try widening the search or clearing one of the active filters." in response.text

    async def test_history_partial_renders_clear_history_action_when_rows_exist(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_post_processing_dataset(_db_factory)

        response = await client.get("/post-processing?tab=history")

        assert response.status_code == 200
        assert 'data-testid="pp-history-clear"' in response.text

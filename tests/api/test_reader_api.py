"""Reader manifest and revisioned page delivery API contract tests."""

from __future__ import annotations

import io
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select

from pullbox.core.events import EventBus, ReaderCompletionChanged, ReaderWantToReadChanged
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.reader import IssueReaderState
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import User
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService
from pullbox.services.reader_content_service import ReaderContentService, ReaderWorkerBusyError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest_plugins = ["tests.conftest_security"]


def _write_cbz(path: Path, *, page_count: int = 2) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        labels = (b"one", b"two", b"three")
        for page_index in range(page_count):
            page = io.BytesIO()
            with Image.new("P", (1, 1), color=page_index) as image:
                image.save(page, format="GIF")
            archive.writestr(
                f"{page_index + 1:03d}.gif",
                page.getvalue() + b"-page-" + labels[page_index],
            )


def _csrf_headers(client: AsyncClient) -> dict[str, str]:
    session_token = client.cookies.get(SESSION_COOKIE_NAME) or ""
    csrf = AuthService.get_csrf_token_from_session(session_token) or ""
    return {"X-CSRF-Token": csrf}


async def _seed_catalog_issue_without_file(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    async with factory() as session:
        series = Series(
            title="Unavailable Reader Series",
            sort_title="unavailable reader series",
            year_start=2026,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        session.add(series)
        await session.flush()
        issue = Issue(
            series_id=series.id,
            issue_number=1,
            title="Unavailable Issue",
            status=IssueStatus.OWNED,
        )
        session.add(issue)
        await session.commit()
        return issue.id


async def _seed_adjacent_reader_issue(
    factory: async_sessionmaker[AsyncSession],
    *,
    current_issue_id: int,
    source: Path,
) -> int:
    async with factory() as session:
        current = await session.get(Issue, current_issue_id)
        assert current is not None
        root = (
            await session.execute(
                select(LibraryRoot)
                .join(LibraryFile)
                .where(LibraryFile.issue_id == current_issue_id)
            )
        ).scalar_one()
        issue = Issue(
            series_id=current.series_id,
            issue_number=2,
            title="Adjacent Issue",
            status=IssueStatus.OWNED,
        )
        session.add(issue)
        await session.flush()
        stat = source.stat()
        session.add(
            LibraryFile(
                file_path=str(source),
                file_name=source.name,
                file_size=stat.st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                issue_id=issue.id,
                match_confidence=MatchConfidence.HIGH,
                library_root_id=root.id,
            )
        )
        await session.commit()
        return issue.id


async def _seed_reader_issue(
    factory: async_sessionmaker[AsyncSession],
    source: Path,
) -> int:
    async with factory() as session:
        series = Series(
            comicvine_id=800_001,
            title="Reader API Series",
            sort_title="reader api series",
            year_start=2026,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        session.add(series)
        await session.flush()
        issue = Issue(
            series_id=series.id,
            comicvine_id=800_002,
            issue_number=1,
            title="Reader API Issue",
            status=IssueStatus.OWNED,
        )
        session.add(issue)
        await session.flush()
        root = LibraryRoot(name="reader-api", path=str(source.parent))
        session.add(root)
        await session.flush()
        stat = source.stat()
        session.add(
            LibraryFile(
                file_path=str(source),
                file_name=source.name,
                file_size=stat.st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                issue_id=issue.id,
                match_confidence=MatchConfidence.HIGH,
                library_root_id=root.id,
            )
        )
        await session.commit()
        return issue.id


@pytest.mark.asyncio
async def test_manifest_and_page_are_private_authenticated_resources(
    authenticated_client: AsyncClient,
    unauthenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    sec_app: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.cbz"
    _write_cbz(source)
    issue_id = await _seed_reader_issue(sec_db, source)
    sec_app.state.reader_content_service = ReaderContentService(cache_dir=tmp_path / "cache")

    denied = await unauthenticated_client.get(f"/api/v1/reader/issues/{issue_id}/manifest")
    manifest_response = await authenticated_client.get(f"/api/v1/reader/issues/{issue_id}/manifest")

    assert denied.status_code == 401
    assert manifest_response.status_code == 200
    assert manifest_response.headers["cache-control"] == (
        "no-store, no-cache, max-age=0, must-revalidate"
    )
    manifest = manifest_response.json()
    assert manifest["page_count"] == 2
    assert manifest["initial_page_index"] == 0
    assert manifest["completion_url"].endswith(f"/issues/{issue_id}/completion")
    assert manifest["want_to_read_url"].endswith(f"/issues/{issue_id}/want-to-read")
    assert manifest["issue_detail_url"] == f"/issues/{issue_id}"
    assert manifest["download_url"] == f"/api/v1/issues/{issue_id}/download-file"
    assert manifest["state"] == {
        "page_index": None,
        "page_count": None,
        "progress_updated_at": None,
        "last_opened_at": None,
        "completed_at": None,
        "completion_updated_at": None,
        "want_to_read": False,
        "want_to_read_updated_at": None,
        "state_version": 0,
    }
    assert manifest["previous_issue"] is None
    assert manifest["next_issue"] is None
    assert "content_revision" not in manifest["state"]
    assert "file_path" not in manifest_response.text
    assert manifest["page_url_template"].endswith(
        "/pages/{page_index}?revision=" + manifest["revision"]
    )

    page_response = await authenticated_client.get(
        manifest["page_url_template"].replace("{page_index}", "1")
    )
    assert page_response.status_code == 200
    assert page_response.content.endswith(b"-page-two")
    assert page_response.headers["content-type"].startswith("image/gif")
    assert page_response.headers["cache-control"] == "private, max-age=3600, immutable"
    assert page_response.headers["x-content-type-options"] == "nosniff"
    assert page_response.headers["content-disposition"] == 'inline; filename="page-2.gif"'

    not_modified = await authenticated_client.get(
        manifest["page_url_template"].replace("{page_index}", "1"),
        headers={"If-None-Match": page_response.headers["etag"]},
    )
    assert not_modified.status_code == 304
    assert not_modified.content == b""


@pytest.mark.asyncio
async def test_reader_api_rejects_stale_revision_without_file_details(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    sec_app: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.cbz"
    _write_cbz(source)
    issue_id = await _seed_reader_issue(sec_db, source)
    sec_app.state.reader_content_service = ReaderContentService(cache_dir=tmp_path / "cache")

    response = await authenticated_client.get(
        f"/api/v1/reader/issues/{issue_id}/pages/0?revision=stale"
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_revision"
    assert str(source) not in response.text


@pytest.mark.asyncio
async def test_progress_is_explicit_private_and_resumed_from_manifest(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    sec_app: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.api.v1 import reader as reader_api

    source = tmp_path / "book.cbz"
    _write_cbz(source)
    issue_id = await _seed_reader_issue(sec_db, source)
    sec_app.state.reader_content_service = ReaderContentService(cache_dir=tmp_path / "cache")
    manifest_response = await authenticated_client.get(f"/api/v1/reader/issues/{issue_id}/manifest")
    manifest = manifest_response.json()
    events: list[ReaderCompletionChanged] = []
    bus = EventBus()
    bus.subscribe(ReaderCompletionChanged, events.append)
    monkeypatch.setattr(reader_api, "get_event_bus", lambda: bus)
    session_token = authenticated_client.cookies.get(SESSION_COOKIE_NAME) or ""
    csrf = AuthService.get_csrf_token_from_session(session_token) or ""
    payload = {
        "revision": manifest["revision"],
        "page_index": 1,
        "page_count": 2,
        "completion_candidate": True,
    }

    csrf_denied = await authenticated_client.put(
        f"/api/v1/reader/issues/{issue_id}/progress",
        json=payload,
    )
    saved = await authenticated_client.put(
        f"/api/v1/reader/issues/{issue_id}/progress",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )
    reread = await authenticated_client.put(
        f"/api/v1/reader/issues/{issue_id}/progress",
        json={
            **payload,
            "page_index": 0,
            "completion_candidate": False,
            "reread_started": True,
        },
        headers={"X-CSRF-Token": csrf},
    )
    resumed = await authenticated_client.get(f"/api/v1/reader/issues/{issue_id}/manifest")

    assert csrf_denied.status_code == 403
    assert saved.status_code == 200
    assert saved.json()["page_index"] == 1
    assert saved.json()["completed_at"] is not None
    assert saved.json()["state"]["state_version"] == 1
    assert saved.json()["state"]["want_to_read"] is False
    assert reread.status_code == 200
    assert reread.json()["completed_at"] is None
    assert reread.json()["page_index"] == 0
    assert "content_revision" not in saved.json()["state"]
    assert resumed.json()["initial_page_index"] == 0
    assert len(events) == 2
    assert events[0].completed is True
    assert events[0].origin == "automatic"
    assert events[1].completed is False
    assert events[1].origin == "reread"


@pytest.mark.asyncio
async def test_manifest_reconciles_replacement_count_and_final_state_without_writing(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    sec_user: object,
    sec_app: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.cbz"
    _write_cbz(source, page_count=3)
    issue_id = await _seed_reader_issue(sec_db, source)
    sec_app.state.reader_content_service = ReaderContentService(cache_dir=tmp_path / "cache")
    assert hasattr(sec_user, "id")
    async with sec_db() as session:
        session.add(
            IssueReaderState(
                user_id=sec_user.id,
                issue_id=issue_id,
                last_page_index=1,
                content_revision="replaced-revision",
                page_count=3,
                progress_updated_at=datetime.now(UTC),
                last_opened_at=datetime.now(UTC),
            )
        )
        await session.commit()

    same_count = await authenticated_client.get(f"/api/v1/reader/issues/{issue_id}/manifest")
    assert same_count.json()["initial_page_index"] == 1

    async with sec_db() as session:
        state = (
            await session.execute(
                select(IssueReaderState).where(IssueReaderState.issue_id == issue_id)
            )
        ).scalar_one()
        state.page_count = 2
        state.last_page_index = 1
        await session.commit()
    changed_count = await authenticated_client.get(f"/api/v1/reader/issues/{issue_id}/manifest")
    assert changed_count.json()["initial_page_index"] == 0

    async with sec_db() as session:
        state = (
            await session.execute(
                select(IssueReaderState).where(IssueReaderState.issue_id == issue_id)
            )
        ).scalar_one()
        assert state.content_revision == "replaced-revision"
        assert state.page_count == 2
        assert state.last_page_index == 1


@pytest.mark.asyncio
async def test_manifest_builds_server_owned_adjacent_issue_links(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    sec_app: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.cbz"
    adjacent_source = tmp_path / "adjacent.cbz"
    _write_cbz(source)
    _write_cbz(adjacent_source)
    issue_id = await _seed_reader_issue(sec_db, source)
    next_issue_id = await _seed_adjacent_reader_issue(
        sec_db,
        current_issue_id=issue_id,
        source=adjacent_source,
    )
    sec_app.state.reader_content_service = ReaderContentService(cache_dir=tmp_path / "cache")

    response = await authenticated_client.get(f"/api/v1/reader/issues/{issue_id}/manifest")

    assert response.status_code == 200
    assert response.json()["previous_issue"] is None
    assert response.json()["next_issue"] == {
        "issue_id": next_issue_id,
        "issue_label": "#2",
        "title": "Adjacent Issue",
        "manifest_url": f"/api/v1/reader/issues/{next_issue_id}/manifest",
        "issue_detail_url": f"/issues/{next_issue_id}",
        "download_url": f"/api/v1/issues/{next_issue_id}/download-file",
    }


@pytest.mark.asyncio
async def test_completion_is_idempotent_emits_after_commit_and_isolates_handler_failure(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    sec_user: object,
    sec_app: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.api.v1 import reader as reader_api

    source = tmp_path / "book.cbz"
    _write_cbz(source)
    issue_id = await _seed_reader_issue(sec_db, source)
    sec_app.state.reader_content_service = ReaderContentService(cache_dir=tmp_path / "cache")
    assert hasattr(sec_user, "id")
    observed: list[tuple[str, bool]] = []
    bus = EventBus()

    async def observe(event: object) -> None:
        async with sec_db() as session:
            state = (
                await session.execute(
                    select(IssueReaderState).where(IssueReaderState.issue_id == issue_id)
                )
            ).scalar_one_or_none()
        observed.append(
            (type(event).__name__, state is not None and state.completed_at is not None)
        )

    def fail_handler(_event: object) -> None:
        raise RuntimeError("subscriber failed")

    monkeypatch.setattr(reader_api, "get_event_bus", lambda: bus, raising=False)

    denied = await authenticated_client.put(
        f"/api/v1/reader/issues/{issue_id}/completion",
        json={"completed": True},
    )
    bus.subscribe(ReaderCompletionChanged, observe)
    bus.subscribe(ReaderCompletionChanged, fail_handler)
    marked = await authenticated_client.put(
        f"/api/v1/reader/issues/{issue_id}/completion",
        json={"completed": True},
        headers=_csrf_headers(authenticated_client),
    )
    repeated = await authenticated_client.put(
        f"/api/v1/reader/issues/{issue_id}/completion",
        json={"completed": True},
        headers=_csrf_headers(authenticated_client),
    )

    assert denied.status_code == 403
    assert marked.status_code == 200
    assert marked.json()["changed"] is True
    assert marked.json()["state"]["completed_at"] is not None
    assert repeated.status_code == 200
    assert repeated.json()["changed"] is False
    assert repeated.json()["state"]["state_version"] == marked.json()["state"]["state_version"]
    assert observed == [("ReaderCompletionChanged", True)]


@pytest.mark.asyncio
async def test_want_to_read_requires_readability_only_when_adding(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    issue_id = await _seed_catalog_issue_without_file(sec_db)
    endpoint = f"/api/v1/reader/issues/{issue_id}/want-to-read"

    rejected = await authenticated_client.put(
        endpoint,
        json={"want_to_read": True},
        headers=_csrf_headers(authenticated_client),
    )
    removed = await authenticated_client.put(
        endpoint,
        json={"want_to_read": False},
        headers=_csrf_headers(authenticated_client),
    )
    missing = await authenticated_client.put(
        "/api/v1/reader/issues/999999/want-to-read",
        json={"want_to_read": False},
        headers=_csrf_headers(authenticated_client),
    )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "issue_not_readable"
    assert removed.status_code == 200
    assert removed.json()["changed"] is True
    assert removed.json()["state"]["want_to_read"] is False
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_want_to_read_is_idempotent_and_emits_only_effective_changes(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    sec_app: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.api.v1 import reader as reader_api

    source = tmp_path / "book.cbz"
    _write_cbz(source)
    issue_id = await _seed_reader_issue(sec_db, source)
    sec_app.state.reader_content_service = ReaderContentService(cache_dir=tmp_path / "cache")
    events: list[ReaderWantToReadChanged] = []
    bus = EventBus()
    bus.subscribe(ReaderWantToReadChanged, events.append)
    monkeypatch.setattr(reader_api, "get_event_bus", lambda: bus)
    endpoint = f"/api/v1/reader/issues/{issue_id}/want-to-read"

    added = await authenticated_client.put(
        endpoint,
        json={"want_to_read": True},
        headers=_csrf_headers(authenticated_client),
    )
    repeated = await authenticated_client.put(
        endpoint,
        json={"want_to_read": True},
        headers=_csrf_headers(authenticated_client),
    )
    removed = await authenticated_client.put(
        endpoint,
        json={"want_to_read": False},
        headers=_csrf_headers(authenticated_client),
    )

    assert added.json()["changed"] is True
    assert repeated.json()["changed"] is False
    assert repeated.json()["state"]["state_version"] == 1
    assert removed.json()["changed"] is True
    assert removed.json()["state"]["state_version"] == 2
    assert [event.enabled for event in events] == [True, False]
    assert all(event.issue_id == issue_id for event in events)


@pytest.mark.asyncio
async def test_manifest_never_projects_another_users_state(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    sec_app: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.cbz"
    _write_cbz(source)
    issue_id = await _seed_reader_issue(sec_db, source)
    sec_app.state.reader_content_service = ReaderContentService(cache_dir=tmp_path / "cache")
    async with sec_db() as session:
        other = User(
            username="other-reader",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(other)
        await session.flush()
        session.add(
            IssueReaderState(
                user_id=other.id,
                issue_id=issue_id,
                completed_at=datetime.now(UTC),
                completion_updated_at=datetime.now(UTC),
                want_to_read=True,
                want_to_read_updated_at=datetime.now(UTC),
            )
        )
        await session.commit()

    response = await authenticated_client.get(f"/api/v1/reader/issues/{issue_id}/manifest")

    assert response.status_code == 200
    assert response.json()["state"]["completed_at"] is None
    assert response.json()["state"]["want_to_read"] is False
    assert response.json()["state"]["state_version"] == 0


@pytest.mark.asyncio
async def test_reader_mutation_schema_is_strict(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    issue_id = await _seed_catalog_issue_without_file(sec_db)

    response = await authenticated_client.put(
        f"/api/v1/reader/issues/{issue_id}/completion",
        json={"completed": "yes"},
        headers=_csrf_headers(authenticated_client),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_failed_commit_emits_no_reader_event(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.api.v1 import reader as reader_api

    issue_id = await _seed_catalog_issue_without_file(sec_db)
    emitted: list[ReaderCompletionChanged] = []
    bus = EventBus()
    bus.subscribe(ReaderCompletionChanged, emitted.append)
    monkeypatch.setattr(reader_api, "get_event_bus", lambda: bus)

    def failing_factory() -> object:
        @asynccontextmanager
        async def session_context() -> AsyncIterator[AsyncSession]:
            async with sec_db() as session:
                session.commit = AsyncMock(side_effect=RuntimeError("commit failed"))  # type: ignore[method-assign]
                yield session

        return session_context()

    monkeypatch.setattr(reader_api, "get_request_session_factory", lambda _request: failing_factory)

    with pytest.raises(RuntimeError, match="commit failed"):
        await authenticated_client.put(
            f"/api/v1/reader/issues/{issue_id}/completion",
            json={"completed": True},
            headers=_csrf_headers(authenticated_client),
        )

    assert emitted == []


@pytest.mark.asyncio
async def test_reader_state_mutations_accept_api_key_without_csrf(
    sec_app: object,
    sec_api_key: str,
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    issue_id = await _seed_catalog_issue_without_file(sec_db)
    transport = ASGITransport(app=sec_app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-API-Key": sec_api_key},
    ) as client:
        response = await client.put(
            f"/api/v1/reader/issues/{issue_id}/completion",
            json={"completed": True},
        )

    assert response.status_code == 200
    assert response.json()["state"]["completed_at"] is not None


@pytest.mark.asyncio
async def test_page_get_and_manifest_do_not_create_progress_state(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    sec_user: object,
    sec_app: object,
    tmp_path: Path,
) -> None:
    from sqlalchemy import select

    from pullbox.models.reader import IssueReaderState

    source = tmp_path / "book.cbz"
    _write_cbz(source)
    issue_id = await _seed_reader_issue(sec_db, source)
    sec_app.state.reader_content_service = ReaderContentService(cache_dir=tmp_path / "cache")
    manifest = (await authenticated_client.get(f"/api/v1/reader/issues/{issue_id}/manifest")).json()

    await authenticated_client.get(manifest["page_url_template"].replace("{page_index}", "1"))

    async with sec_db() as session:
        rows = list((await session.execute(select(IssueReaderState))).scalars().all())
    assert rows == []


@pytest.mark.asyncio
async def test_reader_worker_saturation_is_retryable_without_internal_details(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    sec_app: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.cbz"
    _write_cbz(source)
    issue_id = await _seed_reader_issue(sec_db, source)
    service = ReaderContentService(cache_dir=tmp_path / "cache")
    service.get_manifest = AsyncMock(  # type: ignore[method-assign]
        side_effect=ReaderWorkerBusyError
    )
    sec_app.state.reader_content_service = service

    response = await authenticated_client.get(f"/api/v1/reader/issues/{issue_id}/manifest")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["detail"]["code"] == "reader_busy"


@pytest.mark.asyncio
async def test_reader_can_be_emergency_disabled_without_exposing_routes(
    authenticated_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from pullbox.api.v1 import reader as reader_api

    monkeypatch.setattr(
        reader_api,
        "get_settings",
        lambda: SimpleNamespace(reader_enabled=False),
    )

    response = await authenticated_client.get("/api/v1/reader/issues/1/manifest")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_reader_capabilities_and_safe_cache_clear_are_private(
    authenticated_client: AsyncClient,
    unauthenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    sec_app: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.cbz"
    _write_cbz(source)
    issue_id = await _seed_reader_issue(sec_db, source)
    service = ReaderContentService(cache_dir=tmp_path / "cache")
    sec_app.state.reader_content_service = service
    manifest = (await authenticated_client.get(f"/api/v1/reader/issues/{issue_id}/manifest")).json()
    await authenticated_client.get(manifest["page_url_template"].replace("{page_index}", "0"))

    denied = await unauthenticated_client.get("/api/v1/reader/capabilities")
    capabilities = await authenticated_client.get("/api/v1/reader/capabilities")
    csrf_denied = await authenticated_client.delete("/api/v1/reader/cache")
    session_token = authenticated_client.cookies.get(SESSION_COOKIE_NAME) or ""
    csrf = AuthService.get_csrf_token_from_session(session_token) or ""
    cleared = await authenticated_client.delete(
        "/api/v1/reader/cache",
        headers={"X-CSRF-Token": csrf},
    )

    assert denied.status_code == 401
    assert capabilities.status_code == 200
    payload = capabilities.json()
    assert {item["format"] for item in payload["formats"]} == {
        "cbz",
        "cbr",
        "cb7",
        "cbt",
        "pdf",
    }
    assert all(isinstance(item["available"], bool) for item in payload["formats"])
    assert payload["cache"]["cache_file_count"] == 1
    assert str(tmp_path) not in capabilities.text
    assert csrf_denied.status_code == 403
    assert cleared.status_code == 200
    assert cleared.json()["files_removed"] == 1

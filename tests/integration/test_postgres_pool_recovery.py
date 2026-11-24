"""A full application pool must not log readers out or replay a failed issue edit."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox import database
from pullbox.api import middleware
from pullbox.app import create_app
from pullbox.models import Base, Issue, Series, User
from pullbox.models.issue import IssueStatus
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService
from tests.integration.test_database_migration_target import disposable_server
from tests.integration.test_postgres_transfer_stage import target


@pytest.mark.parametrize("major", [16, 18])
async def test_issue_navigation_and_edit_when_pool_is_full(major, monkeypatch):
    with disposable_server(major) as (_, server):
        _, url, _ = target(server)
        # Shorten the checkout wait only on the isolated test pool.
        engine = create_async_engine(url, pool_size=1, max_overflow=0, pool_timeout=0.2)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory.begin() as session:
                user = User(username="reader", password_hash="unused")
                series = Series(title="Pool Test", sort_title="Pool Test")
                session.add_all([user, series])
                await session.flush()
                issue = Issue(series_id=series.id, issue_number=1, status=IssueStatus.WANTED)
                session.add(issue)
            monkeypatch.setattr(database, "_session_factory", factory)
            monkeypatch.setattr(middleware, "_setup_complete", None)
            token = AuthService.create_session_token(user.id, user.session_version)
            api = f"/api/v1/issues/{issue.id}"
            transport = ASGITransport(app=create_app(), raise_app_exceptions=False)
            async with AsyncClient(
                transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: token}
            ) as client:
                assert (await client.get(api)).json()["status"] == "wanted"
                csrf = AuthService.get_csrf_token_from_session(token)
                assert csrf
                async with engine.connect():
                    async with asyncio.timeout(5):
                        page, read, edit = await asyncio.gather(
                            client.get(f"/issues/{issue.id}", headers={"Accept": "text/html"}),
                            client.get(api),
                            client.put(
                                api,
                                headers={"X-CSRF-Token": csrf},
                                json={"status": "skipped"},
                            ),
                        )
                    assert (page.status_code, read.status_code, edit.status_code) == (503, 503, 503)
                    assert "Retry this page" in page.text and "busy" in page.text
                    for response in (read, edit):
                        assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
                        assert "busy" in response.json()["error"]["message"]
                    assert "Check the status of your last action" in edit.json()["error"]["message"]
                    assert read.headers["retry-after"] and "retry-after" not in edit.headers
                    assert all("no-store" in r.headers["cache-control"] for r in (page, read, edit))
                    assert (await client.get("/ping")).is_success
                    middleware.mark_setup_complete()
                    warm = await client.get(f"/issues/{issue.id}", headers={"Accept": "text/html"})
                    assert warm.status_code == 503 and "Retry this page" in warm.text
                # Do not sign in again or resubmit the write.
                recovered = await client.get(api)
                assert recovered.is_success and recovered.json()["status"] == "wanted"
                assert engine.pool.checkedout() == 0
        finally:
            await engine.dispose()

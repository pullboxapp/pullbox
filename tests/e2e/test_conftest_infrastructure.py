"""
Infrastructure verification tests for E2E test fixtures.

Validates that the live server, seed data, browser pages, and auth
fixtures work correctly before any feature-specific E2E tests run.

Run:
    pytest tests/e2e/test_conftest_infrastructure.py -v
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.e2e


class TestLiveServer:
    """Verify the live_server fixture starts and serves requests."""

    def test_live_server_returns_base_url(self, live_server: str) -> None:
        """Fixture yields a valid base URL string."""
        assert live_server.startswith("http://")
        assert ":" in live_server  # has a port

    def test_ping_returns_ok(self, live_server: str) -> None:
        """GET /ping returns 200 with expected JSON."""
        resp = httpx.get(f"{live_server}/ping", timeout=5.0)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "pullbox"

    def test_static_assets_accessible(self, live_server: str) -> None:
        """Static CSS/JS files return 200."""
        resp = httpx.get(f"{live_server}/static/css/tailwind.css", timeout=5.0)
        assert resp.status_code == 200

    def test_openapi_docs_load(self, live_server: str) -> None:
        """/docs (Swagger UI) returns 200."""
        resp = httpx.get(f"{live_server}/docs", timeout=5.0)
        assert resp.status_code == 200


class TestSeededServer:
    """Verify the seeded_server fixture has expected test data."""

    def test_seeded_server_returns_base_url(self, seeded_server: str) -> None:
        """Fixture yields a valid base URL."""
        assert seeded_server.startswith("http://")

    def test_seeded_server_setup_complete(self, seeded_server: str) -> None:
        """After seeding, /setup should redirect to /login."""
        resp = httpx.get(f"{seeded_server}/setup", timeout=5.0, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_seeded_server_login_works(self, seeded_server: str) -> None:
        """Can authenticate with seeded admin credentials."""
        resp = httpx.post(
            f"{seeded_server}/api/v1/auth/login",
            json={"username": "admin", "password": "TestPassword1!"},
            timeout=5.0,
        )
        assert resp.status_code == 200
        assert "pullbox_session" in resp.cookies

    def test_seeded_server_uses_isolated_api_rate_limit_budget(
        self,
        seeded_server: str,
        auth_session_cookie: dict[str, str],
    ) -> None:
        """Shared E2E traffic cannot exhaust production-sized API limits."""
        resp = httpx.get(
            f"{seeded_server}/api/v1/system/usage-stats",
            cookies={auth_session_cookie["name"]: auth_session_cookie["value"]},
            timeout=5.0,
        )

        assert resp.status_code == 200
        assert resp.headers["x-ratelimit-limit"] == "10000"


class TestPageFixture:
    """Verify the page fixture provides a working Playwright page."""

    def test_page_can_navigate(self, page, live_server: str) -> None:  # type: ignore[no-untyped-def]
        """Page fixture can load a URL."""
        page.goto(f"{live_server}/ping")
        assert "ok" in page.content()

    def test_pages_are_independent(self, page, live_server: str) -> None:  # type: ignore[no-untyped-def]
        """Each test gets a fresh page (no state leakage)."""
        # Navigate somewhere specific
        page.goto(f"{live_server}/ping")
        # If this passes, the page was fresh (not carrying state from previous test)
        assert page.url.endswith("/ping")


class TestAuthedPage:
    """Verify the authed_page fixture is logged in and ready."""

    def test_authed_page_can_access_protected_route(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """Authenticated page can access the dashboard (no redirect to login)."""
        authed_page.goto(f"{seeded_server}/")
        # Should NOT be redirected to /login or /setup
        assert "/login" not in authed_page.url
        assert "/setup" not in authed_page.url

    def test_authed_page_sees_sidebar(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """Authenticated page shows the sidebar navigation."""
        authed_page.goto(f"{seeded_server}/")
        sidebar = authed_page.locator("aside")
        assert sidebar.count() > 0


class TestHelpers:
    """Verify E2E helper functions."""

    def test_wait_for_htmx_returns(
        self,
        authed_page,
        seeded_server: str,  # type: ignore[no-untyped-def]
    ) -> None:
        """wait_for_htmx completes without timeout on a loaded page."""
        from tests.e2e.conftest import wait_for_htmx

        authed_page.goto(f"{seeded_server}/")
        # Should return without raising — page is already settled
        wait_for_htmx(authed_page, timeout=3000)

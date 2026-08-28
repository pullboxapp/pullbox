"""Route-contract tests for the shared app sidebar shell."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-sidebar-shell-ui")

SIDEBAR_COLLAPSE_TOOLTIP_BINDING = (
    "x-bind:data-tip=\"sidebarOpen ? 'Collapse sidebar' : 'Expand sidebar'\""
)
SETTINGS_ICON_PATH = (
    'd="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281'
    "c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456"
    "a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827"
    "c-.293.241-.438.613-.43.992a7 7 0 010 .255c-.008.378.137.75.43.991l1.004.827"
    "c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456"
    "c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 01-.22.128c-.331.183-.581.495-.644.869"
    "l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.212-1.281"
    "c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124"
    "l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431"
    "l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 010-.255c.007-.38-.138-.751-.43-.992"
    "l-1.004-.827a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491"
    "l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869"
    'l.214-1.28zM15 12a3 3 0 11-6 0 3 3 0 016 0z"'
)


@pytest.mark.asyncio
class TestSidebarShellRouteContracts:
    """Verify the shared sidebar shell renders a stable, standardized contract."""

    async def test_series_page_renders_standardized_sidebar_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/series")

        assert response.status_code == 200
        assert 'x-data="appShell()"' in response.text
        assert "data-shell-pending" in response.text
        assert "body[data-shell-pending] { visibility: hidden; }" in response.text
        assert "alpine:initialized" in response.text
        assert "document.documentElement.classList.add('boot-no-transitions')" in response.text
        assert 'id="pullbox-app-stylesheet"' in response.text
        assert 'onload="window.pullboxStylesheetLoaded(this)"' in response.text
        assert 'onerror="window.pullboxStylesheetFailed(this)"' in response.text
        assert "var appStylesReady = false" in response.text
        assert "var stylesheetRetryLimit = 2" in response.text
        assert "var stylesheetLoadTimeoutMs = 4000" in response.text
        assert "window.pullboxArmStylesheetTimeout" in response.text
        assert "--pb-surface-app" in response.text
        assert "data-pullbox-stylesheet-ready" in response.text
        assert "!appStylesReady" in response.text
        assert "document.fonts.ready" in response.text
        assert "window.Alpine.nextTick" in response.text
        assert "armBootTransitionRelease" in response.text
        assert "releaseBootTransitions" in response.text
        assert (
            'id="main-area" class="relative flex h-[100dvh] flex-col overflow-hidden"'
            in response.text
        )
        assert "#main-area { overflow: clip; }" in response.text
        assert 'href="/static/fonts/dm-sans-variable.woff2"' in response.text
        assert 'href="/static/fonts/bricolage-grotesque-800.woff2"' in response.text
        assert 'data-testid="app-sidebar"' in response.text
        assert 'data-testid="sidebar-mobile-backdrop"' in response.text
        assert 'data-testid="sidebar-logo-link"' in response.text
        assert 'data-testid="sidebar-nav"' in response.text
        assert response.text.count('hx-select-oob="#page-footer-dock"') >= 2
        assert 'data-testid="sidebar-footer"' not in response.text
        assert 'data-testid="sidebar-footer-status"' not in response.text
        assert 'data-testid="sidebar-footer-text"' not in response.text
        assert 'data-testid="sidebar-section-library"' in response.text
        assert 'data-testid="sidebar-section-acquisition"' in response.text
        assert 'data-testid="sidebar-section-matching"' in response.text
        assert 'data-testid="sidebar-section-admin"' in response.text
        assert 'data-testid="sidebar-link-dashboard"' in response.text
        assert 'data-testid="sidebar-link-series"' in response.text
        assert 'data-testid="sidebar-link-library"' in response.text
        assert 'data-testid="sidebar-link-reading"' in response.text
        assert 'data-testid="sidebar-link-pull-list"' in response.text
        assert 'data-testid="sidebar-link-whats-new"' in response.text
        assert 'data-testid="sidebar-link-import"' in response.text
        assert 'data-testid="sidebar-link-health"' in response.text
        assert 'data-testid="sidebar-link-settings"' in response.text
        assert 'data-testid="sidebar-collapse-toggle"' in response.text
        assert SIDEBAR_COLLAPSE_TOOLTIP_BINDING in response.text
        assert re.search(
            r'data-testid="sidebar-collapse-toggle"[^>]*data-tip-pos="right"',
            response.text,
        )
        assert ":title=\"sidebarOpen ? 'Collapse sidebar' : 'Expand sidebar'\"" not in response.text
        assert 'data-nav-link="true"' in response.text
        assert 'data-nav-path="/series"' in response.text
        assert 'data-nav-path="/reading"' in response.text
        assert 'data-nav-path="/pull-list"' in response.text
        assert 'data-nav-path="/whats-new"' in response.text
        assert 'data-nav-match="prefix"' in response.text
        assert 'data-nav-match="exact"' in response.text
        assert """:data-tip='showSidebarLabels ? null : "What\\u0027s New"'""" in response.text
        assert ":data-tip=\"showSidebarLabels ? null : 'What's New'\"" not in response.text
        assert response.text.index('data-testid="sidebar-link-library"') < response.text.index(
            'data-testid="sidebar-link-pull-list"'
        )
        assert response.text.index('data-testid="sidebar-link-whats-new"') < response.text.index(
            'data-testid="sidebar-link-downloads"'
        )
        assert 'data-testid="sidebar-badge-whats-new"' not in response.text
        assert 'aria-current="page"' in response.text
        assert 'data-series-index-link="true"' in response.text
        assert 'data-testid="sidebar-badge-intervention"' in response.text
        assert 'data-testid="sidebar-badge-health"' in response.text
        assert 'data-sidebar-badge="true"' in response.text
        assert 'data-sidebar-badge-endpoint="/htmx/intervention/count"' in response.text
        assert 'data-sidebar-badge-endpoint="/health/badge"' in response.text
        assert 'data-sidebar-badge-trigger="every 10s"' in response.text
        assert 'data-testid="header-support-link"' in response.text
        assert 'data-testid="header-donations-button"' in response.text
        assert 'aria-label="Support Pullbox"' in response.text
        assert 'data-testid="donations-modal"' in response.text
        assert 'data-testid="donations-buymeacoffee-link"' in response.text
        assert 'href="https://buymeacoffee.com/rcogrr50fg"' in response.text
        assert 'src="/static/img/donations/buy-me-a-coffee-qr.png"' in response.text
        assert 'data-testid="donations-liberapay-link"' in response.text
        assert 'href="https://liberapay.com/Pullbox/"' in response.text
        assert 'src="/static/img/donations/liberapay-qr.png"' in response.text
        assert 'href="/system?tab=support"' in response.text
        assert 'aria-label="Support"' in response.text
        assert (
            'class="app-header-icon-tip inline-flex h-9 w-9 items-center '
            "justify-center rounded-lg p-0"
        ) in response.text
        assert re.search(
            r'aria-label="User menu"[^>]*font-sans[^>]*font-medium',
            response.text,
        )
        assert (
            'd="M9.25 8.75c0-1.519 1.231-2.75 2.75-2.75s2.75 1.231 2.75 2.75'
            'c0 1.184-.642 2.073-1.63 2.679-.973.597-1.62 1.203-1.62 2.446v.375"'
        ) in response.text
        assert 'd="M12 17.25h.01"' in response.text
        assert (
            'style="width:1.75rem;height:1.75rem;" fill="none" stroke="currentColor" '
            'stroke-width="1.5"'
        ) in response.text
        assert re.search(
            r'data-testid="header-support-link".*?<svg[^>]*style="width:1\.75rem;height:1\.75rem;"',
            response.text,
            re.DOTALL,
        )
        assert SETTINGS_ICON_PATH in response.text
        assert "window.__autoSearching" not in response.text
        assert 'x-data="{ sidebarOpen:' not in response.text

    async def test_reader_gate_hides_reading_sidebar_entry(
        self,
        authenticated_client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.ui import routes

        monkeypatch.setattr(
            routes,
            "get_settings",
            lambda: SimpleNamespace(reader_enabled=False),
            raising=False,
        )

        response = await authenticated_client.get("/series")

        assert response.status_code == 200
        assert 'data-testid="sidebar-link-reading"' not in response.text
        assert 'data-nav-path="/reading"' not in response.text

    async def test_app_shell_fonts_use_stable_urls_and_swap_loading(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/series")

        assert response.status_code == 200
        assert 'href="/static/fonts/dm-sans-variable.woff2?v=' not in response.text
        assert 'href="/static/fonts/bricolage-grotesque-800.woff2?v=' not in response.text
        assert 'href="/static/fonts/syne-700.ttf?v=' not in response.text
        assert 'href="/static/fonts/jetbrains-mono-700.ttf?v=' not in response.text

        input_css = Path("src/pullbox/ui/static/css/input.css").read_text(encoding="utf-8")
        assert "font-display: optional;" not in input_css
        assert input_css.count("font-display: swap;") >= 11

    async def test_series_link_renders_active_state_on_series_page(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/series")

        assert response.status_code == 200
        assert 'data-testid="sidebar-link-series"' in response.text
        assert "app-sidebar-link-active text-pb-interactive" in response.text
        assert "bg-pb-interactive-dim text-pb-interactive" not in response.text

    async def test_series_page_renders_preloaded_sidebar_badge_counts(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        with patch(
            "pullbox.api.deps._build_sidebar_context",
            new_callable=AsyncMock,
            return_value={
                "pending_match_count": 4,
                "health_degraded": 1,
                "health_unhealthy": 1,
            },
        ):
            response = await authenticated_client.get("/series")

        assert response.status_code == 200
        assert re.search(
            r'data-testid="sidebar-badge-intervention".*?data-sidebar-count="4".*?>\s*4\s*<',
            response.text,
            re.DOTALL,
        )
        assert re.search(
            r'data-testid="sidebar-badge-health".*?data-sidebar-count="2".*?>\s*2\s*<',
            response.text,
            re.DOTALL,
        )

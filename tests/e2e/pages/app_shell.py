"""Shared app shell page object for sidebar-focused tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.e2e.pages.base import BasePage

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page


class AppShellPage(BasePage):
    """Page object for the persistent app shell and sidebar."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str) -> None:
        self.navigate(path)
        self.sidebar.wait_for(state="visible", timeout=5000)
        self.header.wait_for(state="visible", timeout=5000)

    @property
    def sidebar(self) -> Locator:
        return self.page.locator("[data-testid='app-sidebar']").first

    @property
    def nav(self) -> Locator:
        return self.page.locator("[data-testid='sidebar-nav']").first

    @property
    def header(self) -> Locator:
        return self.page.locator("[data-testid='app-header']").first

    @property
    def add_button(self) -> Locator:
        return self.page.locator("[data-testid='header-add-action']").first

    @property
    def collapse_toggle(self) -> Locator:
        return self.page.locator("[data-testid='sidebar-collapse-toggle']").first

    @property
    def logo_link(self) -> Locator:
        return self.page.locator("[data-testid='sidebar-logo-link']").first

    @property
    def mobile_backdrop(self) -> Locator:
        return self.page.locator("[data-testid='sidebar-mobile-backdrop']").first

    def link(self, key: str) -> Locator:
        return self.page.locator(f"[data-testid='sidebar-link-{key}']").first

    def badge(self, key: str) -> Locator:
        return self.page.locator(f"[data-testid='sidebar-badge-{key}']").first

    def section(self, key: str) -> Locator:
        return self.page.locator(f"[data-testid='sidebar-section-{key}']").first

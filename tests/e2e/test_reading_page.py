"""Focused browser coverage for the private Reading workspace."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.usefixtures("seeded_reader_state_guard"),
]


def _goto_reading(page: Page, base_url: str, *, view: str = "continue") -> None:
    page.goto(f"{base_url}/reading?view={view}")
    page.locator("[data-testid='reading-page']").wait_for(state="visible")


class TestReadingPage:
    def test_workspace_is_keyboard_reachable_and_responsive(
        self,
        authed_page: Page,
        seeded_server: str,
    ) -> None:
        _goto_reading(authed_page, seeded_server)

        heading = authed_page.locator("[data-testid='reading-title']")
        assert heading.is_visible()
        assert heading.get_attribute("role") is None
        assert heading.evaluate("element => element.tagName") == "H1"
        assert authed_page.locator("[data-testid='reading-card']").count() == 1
        assert authed_page.get_by_text("Page 2 of 3 · 66%", exact=True).is_visible()
        assert (
            authed_page.locator("[data-testid='sidebar-link-reading']").get_attribute(
                "aria-current"
            )
            == "page"
        )

        want_tab = authed_page.locator("[data-testid='reading-view-want-to-read']")
        want_tab.focus()
        assert want_tab.evaluate("element => element === document.activeElement") is True

        authed_page.set_viewport_size({"width": 320, "height": 720})
        assert authed_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert authed_page.locator("[data-testid='reading-card']").bounding_box() is not None

    def test_queue_mutation_refreshes_the_reading_fragment(
        self,
        authed_page: Page,
        seeded_server: str,
    ) -> None:
        _goto_reading(authed_page, seeded_server)

        primary_before = authed_page.locator("[data-testid='reading-card']").get_by_role(
            "link", name="Continue", exact=True
        )
        primary_before_box = primary_before.bounding_box()
        assert primary_before_box is not None
        add_button = authed_page.get_by_role("button", name="Want to Read")
        add_button.click()
        add_button.wait_for(state="detached")
        primary_after_box = (
            authed_page.locator("[data-testid='reading-card']")
            .get_by_role("link", name="Continue", exact=True)
            .bounding_box()
        )
        assert primary_after_box is not None
        assert abs(primary_after_box["y"] - primary_before_box["y"]) < 1
        continue_card_box = authed_page.locator("[data-testid='reading-card']").bounding_box()
        continue_actions_box = authed_page.locator(
            "[data-testid='reading-card'] .reading-card-actions"
        ).bounding_box()
        assert continue_card_box is not None
        assert continue_actions_box is not None

        _goto_reading(authed_page, seeded_server, view="want-to-read")
        remove_button = authed_page.get_by_role("button", name="Remove", exact=True)
        assert remove_button.is_visible()
        want_card_box = authed_page.locator("[data-testid='reading-card']").bounding_box()
        want_actions_box = authed_page.locator(
            "[data-testid='reading-card'] .reading-card-actions"
        ).bounding_box()
        assert want_card_box is not None
        assert want_actions_box is not None
        continue_action_offset = continue_actions_box["y"] - continue_card_box["y"]
        want_action_offset = want_actions_box["y"] - want_card_box["y"]
        assert abs(continue_card_box["height"] - want_card_box["height"]) < 1
        assert abs(continue_action_offset - want_action_offset) < 1, (
            continue_card_box,
            continue_actions_box,
            want_card_box,
            want_actions_box,
        )
        assert continue_action_offset >= continue_card_box["height"] * 0.5
        remove_button.click()

        authed_page.get_by_text("Your reading queue is clear.", exact=True).wait_for(
            state="visible"
        )
        assert authed_page.locator("[data-testid='reading-card']").count() == 0

    def test_failed_mutation_keeps_the_card_and_reports_recovery_copy(
        self,
        authed_page: Page,
        seeded_server: str,
    ) -> None:
        _goto_reading(authed_page, seeded_server)
        authed_page.route(
            "**/api/v1/reader/issues/*/completion",
            lambda route: route.fulfill(status=500, content_type="application/json", body="{}"),
        )

        authed_page.get_by_role("button", name="Mark read").click()

        error = authed_page.get_by_text("That reading update didn’t save. Try again.", exact=True)
        error.wait_for(state="visible")
        assert error.get_attribute("role") == "alert"
        assert authed_page.locator("[data-testid='reading-card']").count() == 1

    def test_read_actions_share_baseline_and_keep_labels_on_one_line(
        self,
        authed_page: Page,
        seeded_server: str,
    ) -> None:
        _goto_reading(authed_page, seeded_server)
        authed_page.get_by_role("button", name="Mark read", exact=True).click()
        authed_page.get_by_text("Nothing to pick up yet.", exact=True).wait_for(state="visible")

        _goto_reading(authed_page, seeded_server, view="read")
        card = authed_page.locator("[data-testid='reading-card']")
        actions = card.locator(".reading-card-actions")
        reread = card.get_by_role("link", name="Reread", exact=True)
        mark_unread = card.get_by_role("button", name="Mark unread", exact=True)
        want_to_read = card.get_by_role("button", name="Want to Read", exact=True)
        card_box = card.bounding_box()
        actions_box = actions.bounding_box()
        reread_box = reread.bounding_box()
        mark_unread_box = mark_unread.bounding_box()
        want_to_read_box = want_to_read.bounding_box()

        assert card_box is not None
        assert actions_box is not None
        assert reread_box is not None
        assert mark_unread_box is not None
        assert want_to_read_box is not None
        assert abs(reread_box["y"] - mark_unread_box["y"]) < 1
        assert abs(reread_box["height"] - mark_unread_box["height"]) < 1
        assert abs(reread_box["x"] - want_to_read_box["x"]) < 1
        assert (
            abs(
                mark_unread_box["x"]
                + mark_unread_box["width"]
                - want_to_read_box["x"]
                - want_to_read_box["width"]
            )
            < 1
        )
        assert actions_box["y"] - card_box["y"] >= card_box["height"] * 0.5
        assert reread.evaluate("element => getComputedStyle(element).whiteSpace") == "nowrap"
        assert mark_unread.evaluate("element => getComputedStyle(element).whiteSpace") == "nowrap"

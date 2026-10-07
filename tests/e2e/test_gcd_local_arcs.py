"""GCD dump-backed arc review reuses the existing accessible browser controls."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from playwright.sync_api import expect
from sqlalchemy import delete, select

from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.database import get_session_factory
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.providers.metadata.gcd_local_database import validate_candidate
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.unit.test_gcd_local_arcs import arc_dump
from tests.unit.test_metadata_discovery import runtime

pytestmark = pytest.mark.e2e


@pytest.fixture
def gcd_arcs(seeded_server, tmp_path, monkeypatch):
    path = arc_dump(tmp_path / "gcd.db")
    snapshot = _run_async_blocking(validate_candidate(str(path)))
    native = replace(runtime(Source.GCD_LOCAL, revision=1), gcd_snapshot=snapshot)

    async def setup():
        async with get_session_factory().begin() as session:
            config = await session.scalar(
                select(MetadataSourceConfig).where(
                    MetadataSourceConfig.source == Source.GCD_LOCAL.value
                )
            )
            previous = None
            if config is None:
                config = MetadataSourceConfig(source=Source.GCD_LOCAL.value)
                session.add(config)
            else:
                previous = {
                    name: getattr(config, name)
                    for name in ("enabled", "priority", "revision", "settings")
                }
            config.enabled, config.priority, config.revision = True, 4, 1
            config.settings = {"database_path": str(path)}
            return previous

    async def cleanup():
        async with get_session_factory().begin() as session:
            if previous is None:
                await session.execute(
                    delete(MetadataSourceConfig).where(
                        MetadataSourceConfig.source == Source.GCD_LOCAL.value
                    )
                )
            else:
                config = await session.scalar(
                    select(MetadataSourceConfig).where(
                        MetadataSourceConfig.source == Source.GCD_LOCAL.value
                    )
                )
                for name, value in previous.items():
                    setattr(config, name, value)

    previous = _run_async_blocking(setup())
    monkeypatch.setattr(
        "pullbox.ui.metadata_arc_search.load_source_runtime", AsyncMock(return_value=[native])
    )
    monkeypatch.setattr(
        "pullbox.services.metadata_arc_commands.load_source_runtime",
        AsyncMock(return_value=[native]),
    )
    try:
        yield
    finally:
        _run_async_blocking(cleanup())


@pytest.mark.parametrize("theme,width", [("light", 1280), ("dark", 1280), ("light", 390)])
def test_gcd_search_review_reorder_skip_and_caveat(
    authed_page, seeded_server, gcd_arcs, theme, width
):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{seeded_server}/story-arcs/add?q=Civil+War&source=gcd_local")
    page.evaluate("theme => applyTheme(theme)", theme)
    expect(page.get_by_role("link", name="Preview Civil War [Marvel]", exact=True)).to_be_visible()
    assert_no_axe_violations(page, name=f"gcd-arc-search-{theme}-{width}", include=["#content"])
    page.get_by_role("link", name="Preview Civil War [Marvel]", exact=True).click()
    page.wait_for_url("**/story-arcs/catalog/gcd_local/4")
    expect(
        page.get_by_text("GCD publication order is a starting point", exact=False)
    ).to_be_visible()
    form = page.get_by_test_id("story-arc-catalog-add-form")
    expect(form.locator("[data-provider-issue-id]")).to_have_count(3)
    expect(form.locator("[data-provider-issue-id]").first).to_have_attribute(
        "data-provider-issue-id", "15"
    )
    form.get_by_role("button", name="Move Other series #1 down", exact=True).press("Enter")
    expect(form.locator("[data-provider-issue-id]").first).to_have_attribute(
        "data-provider-issue-id", "10"
    )
    form.get_by_role("checkbox", name="Skip #13b in this arc", exact=True).check()
    expect(form.get_by_role("checkbox", name="Skip #13b in this arc", exact=True)).to_be_checked()
    expect(form.get_by_role("button", name="Add Story Arc", exact=True)).to_be_enabled()
    page.emulate_media(reduced_motion="reduce")
    assert_no_axe_violations(page, name=f"gcd-arc-review-{theme}-{width}", include=["#content"])
    assert page.locator("#content").evaluate(
        "element => element.scrollWidth <= element.clientWidth"
    )

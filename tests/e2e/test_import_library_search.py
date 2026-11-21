"""Search/add in review leaves file decisions explicit and preserves the shell."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from pullbox.core.metadata_identity import MetadataSource as Source
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.e2e.test_metron_import_review import _add_verified_series
from tests.e2e.test_source_series_add import preview
from tests.ui.test_import_library_search import discovery
from tests.ui.test_import_library_selection import seed_library_choice

pytestmark = pytest.mark.e2e


async def _seed(root, namespace, theme, offset=0):
    from pullbox.database import get_session_factory

    identity_base = 9400000 + offset + ["light", "dark", "tron"].index(theme) * 10000
    identity_base += ["metron", "gcd"].index(namespace) * 1000
    async with get_session_factory()() as session:
        job_id, _, series_id, *_ = await seed_library_choice(
            session, root, namespace, identity_base, admit=False
        )
        return job_id, series_id


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
def test_search_preview_add_and_explicit_choices(
    authed_page, seeded_server, tmp_path, monkeypatch, namespace, theme, browser_name
):
    page = authed_page
    job_id, _ = _run_async_blocking(_seed(tmp_path, namespace, theme))
    source = Source.METRON_API if namespace == "metron" else Source.GCD_LOCAL
    identifier = str(49000001 + ["light", "dark", "tron"].index(theme))
    search_calls = []

    async def search(query, *args, **kwargs):
        search_calls.append(query)
        snapshot = discovery(source, identifier)
        snapshot.results = snapshot.results[:1]
        return snapshot

    monkeypatch.setattr("pullbox.ui.series_routes.search_snapshot", search)
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    errors, held, adds = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/v1/metadata/series/preview", lambda route: held.append(route))

    def add(route):
        adds.append(route.request.post_data_json)
        assert route.request.headers.get("x-csrf-token")
        series_id = _run_async_blocking(_add_verified_series(identifier, namespace))
        route.fulfill(status=201, json={"id": series_id, "title": "Harbor Lights"})

    page.route("**/api/v1/series", add)
    page.goto(f"{seeded_server}/import?tab=collection&resume_job_id={job_id}&resume_step=3")
    page.locator("#import-step-review-shell").evaluate("el => window.librarySearchShell = el")
    page.get_by_test_id("import-review-choose-library").click()
    page.get_by_role("button", name="Search Metron / GCD", exact=True).click()
    search_modal = page.get_by_test_id("import-library-series-search")
    expect(search_modal.get_by_role("dialog", name="Search Metron / GCD")).to_be_visible()
    assert not search_calls
    search_modal.get_by_role("searchbox").fill("Harbor")
    search_modal.get_by_test_id("import-library-search-source").get_by_role("button").click()
    page.get_by_role(
        "option", name="Metron" if namespace == "metron" else "GCD local", exact=True
    ).click()
    search_modal.get_by_role("button", name="Search sources", exact=True).click()
    trigger = search_modal.get_by_role("button", name="Preview & add", exact=True)
    expect(trigger).to_be_visible()
    assert search_calls[-1].sources == [source]
    assert_no_axe_violations(page, name=f"native-import-search-{namespace}-{theme}")
    screenshots = Path(__file__).resolve().parents[2] / "test-results/library-search"
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots / f"{browser_name}-{namespace}-{theme}-search.png"))
    trigger.click()
    dialog = search_modal.get_by_test_id("add-series-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_test_id("add-series-preview-status")).to_be_visible()
    expect(dialog.get_by_role("button", name="Add series", exact=True)).to_be_disabled()
    expect(dialog).to_contain_text("will not remove the added series")
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(trigger).to_be_focused()
    for route in held:
        route.fulfill(json=preview(source.value, identifier=identifier))
    held.clear()
    expect(page.get_by_test_id("import-review-lane-ready")).to_contain_text("(0)")
    assert not adds
    with page.expect_request("**/api/v1/metadata/series/preview"):
        trigger.click()
    expect(dialog.get_by_test_id("add-series-preview-status")).to_be_visible()
    held[0].fulfill(json=preview(source.value, identifier=identifier))
    expect(dialog.get_by_role("button", name="Add series", exact=True)).to_be_enabled()
    assert_no_axe_violations(page, name=f"native-import-add-{namespace}-{theme}")
    page.screenshot(path=str(screenshots / f"{browser_name}-{namespace}-{theme}-preview.png"))
    dialog.get_by_role("button", name="Add series", exact=True).click()
    expect(dialog).to_be_hidden()
    expect(search_modal.get_by_role("button", name="Choose added series")).to_be_visible()
    expect(page.get_by_test_id("import-review-lane-ready")).to_contain_text("(0)")
    assert len(adds) == 1
    assert adds[0]["source"] == source.value and adds[0]["external_id"] == identifier
    search_modal.get_by_role("button", name="Choose added series").click()
    choices = page.get_by_test_id("import-library-series-choices")
    expect(choices).to_be_visible()
    expect(choices.get_by_role("button", name="Use series", exact=True)).to_have_count(1)

    # Choose the new native catalog, not another same-title series already in the fixture.
    async def owner():
        from sqlalchemy import select

        from pullbox.database import get_session_factory
        from pullbox.models.metadata_identity import SeriesExternalIdentity

        async with get_session_factory()() as session:
            return await session.scalar(
                select(SeriesExternalIdentity.series_id).where(
                    SeriesExternalIdentity.identity_namespace == source.identity_namespace,
                    SeriesExternalIdentity.external_id == identifier,
                )
            )

    series_id = _run_async_blocking(owner())
    choices.locator(f'form:has(input[name="library_series_id"][value="{series_id}"])').get_by_role(
        "button", name="Use series"
    ).click()
    expect(choices).not_to_be_visible()
    expect(page.get_by_test_id("import-review-lane-ready")).to_contain_text("(0)")
    page.get_by_test_id("import-review-primary-action").click()
    page.get_by_test_id("import-review-choose-issue").click()
    page.get_by_test_id("import-review-use-issue").click()
    expect(page.get_by_test_id("import-review-lane-ready")).to_contain_text("(1)")
    assert page.evaluate(
        "window.librarySearchShell === document.getElementById('import-step-review-shell')"
    )
    page.screenshot(path=str(screenshots / f"{browser_name}-{namespace}-{theme}.png"))
    assert not errors


def test_cancel_search_aborts_late_response(authed_page, seeded_server, tmp_path):
    page = authed_page
    job_id, _ = _run_async_blocking(_seed(tmp_path, "metron", "light", offset=100000))
    page.goto(f"{seeded_server}/import?tab=collection&resume_job_id={job_id}&resume_step=3")
    page.get_by_test_id("import-review-choose-library").click()
    page.get_by_role("button", name="Search Metron / GCD", exact=True).click()
    modal = page.get_by_test_id("import-library-series-search")
    held = []
    page.route("**/review-library-search?*", lambda route: held.append(route))
    modal.get_by_role("searchbox").fill("Waiting")
    with page.expect_request("**/review-library-search?*"):
        modal.get_by_role("button", name="Search sources").click()
    expect(modal.locator("#import-library-search-progress")).to_have_css("opacity", "1")
    with page.expect_event(
        "requestfailed", predicate=lambda request: "review-library-search?" in request.url
    ):
        modal.get_by_role("button", name="Cancel", exact=True).click()
    expect(modal).to_have_count(0)
    held[0].fulfill(body='<div id="late-search">Late search</div>', content_type="text/html")
    expect(page.locator("#late-search")).to_have_count(0)
    expect(page.get_by_test_id("import-review-lane-ready")).to_contain_text("(0)")

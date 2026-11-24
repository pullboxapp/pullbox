"""Find a missing native series in Follow-up without restarting the import."""

from pathlib import Path

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from pullbox.core.metadata_identity import MetadataSource
from pullbox.models.import_job import ImportedFile, ImportedSeries
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from tests.e2e.accessibility import assert_no_axe_violations
from tests.e2e.conftest import _run_async_blocking
from tests.e2e.test_metron_import_review import _add_verified_series
from tests.e2e.test_native_orphan_recovery import _assert_saved
from tests.e2e.test_source_series_add import preview
from tests.ui.test_import_library_search import discovery
from tests.ui.test_native_follow_up_series import completed_group

pytestmark = pytest.mark.e2e


async def _seed(root, namespace, theme, browser_name, add_missing):
    from pullbox.database import get_session_factory

    base = (
        183_000_000  # Separate from Mylar's skip/unmapped fixture range.
        + ["chromium", "firefox", "webkit"].index(browser_name) * 100000
        + ["light", "dark", "tron"].index(theme) * 10000
        + ["metron", "gcd"].index(namespace) * 1000
        + int(add_missing) * 100
    )
    async with get_session_factory()() as session:
        values, parent = await completed_group(
            session, root, namespace, identity_base=base, admit=False
        )
        series = await session.get(Series, values[2])
        series.title = series.sort_title = "Follow-up Lights"
        await session.commit()
    return values, parent, str(base + 22)


async def _add_series(identifier, namespace):
    from pullbox.database import get_session_factory

    owner = await _add_verified_series(identifier, namespace)
    async with get_session_factory()() as session:
        series = await session.get(Series, owner)
        series.title = series.sort_title = "Follow-up Lights"
        await session.commit()
    return owner


async def _assert_no_issue_choice(parent_id, file_id, series_id):
    from pullbox.database import get_session_factory

    async with get_session_factory()() as session:
        assert (await session.get(ImportedSeries, parent_id)).series_id == series_id
        file = await session.get(ImportedFile, file_id)
        assert file.matched_issue_id is None and not file.include_in_import


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("theme", ["light", "dark", "tron"])
@pytest.mark.parametrize("add_missing", [False, True], ids=["existing", "search-add"])
def test_find_native_series_then_explicitly_recover_files(
    authed_page, seeded_server, tmp_path, monkeypatch, namespace, theme, browser_name, add_missing
):
    values, parent_id, identifier = _run_async_blocking(
        _seed(tmp_path, namespace, theme, browser_name, add_missing)
    )
    job_id, file_id, series_id, issue_id, comic, original, service = values

    async def build_service(session):
        return service

    monkeypatch.setattr("pullbox.composition.services.build_import_service", build_service)
    page = authed_page
    page.add_init_script(f"localStorage.setItem('pullbox-theme', '{theme}')")
    errors, searches, adds = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    source = MetadataSource.METRON_API if namespace == "metron" else MetadataSource.GCD_LOCAL

    async def search(query, *args, **kwargs):
        searches.append(query)
        snapshot = discovery(source, identifier)
        snapshot.results = snapshot.results[:1]
        snapshot.results[0].title = "Follow-up Lights"
        return snapshot

    monkeypatch.setattr("pullbox.ui.series_routes.search_snapshot", search)
    page.goto(f"{seeded_server}/import?tab=follow-up&job_id={job_id}")
    page.locator("#cv-search-modal").evaluate("el => window.followUpModalHost = el")
    trigger = page.get_by_test_id(f"import-orphaned-search-{parent_id}")
    trigger.click()
    choices = page.get_by_test_id("import-library-series-choices")
    expect(choices).to_be_visible()
    assert not searches
    # Cancel does not select a series or any file.
    choices.get_by_role("button", name="Cancel", exact=True).click()
    expect(choices).not_to_be_visible()
    expect(trigger).to_be_focused()
    _run_async_blocking(_assert_no_issue_choice(parent_id, file_id, None))
    trigger.click()
    expect(choices).to_be_visible()
    if add_missing:
        page.route(
            "**/api/v1/metadata/series/preview",
            lambda route: route.fulfill(json=preview(source.value, identifier=identifier)),
        )

        def add(route):
            adds.append(route.request.post_data_json)
            assert route.request.headers.get("x-csrf-token")
            owner = _run_async_blocking(_add_series(identifier, namespace))
            route.fulfill(status=201, json={"id": owner, "title": "Follow-up Lights"})

        page.route("**/api/v1/series", add)
        choices.get_by_role("button", name="Search Metron / GCD", exact=True).click()
        search_modal = page.get_by_test_id("import-library-series-search")
        expect(search_modal.get_by_role("dialog", name="Search Metron / GCD")).to_be_visible()
        assert not searches
        search_modal.get_by_role("button", name="Back to library choices", exact=True).click()
        expect(choices).to_be_visible()
        choices.get_by_role("button", name="Search Metron / GCD", exact=True).click()
        search_modal.get_by_role("searchbox").fill("Follow-up Lights")
        search_modal.get_by_role("button", name="Search sources", exact=True).click()
        search_modal.get_by_role("button", name="Preview & add", exact=True).click()
        dialog = search_modal.get_by_test_id("add-series-dialog")
        expect(dialog.get_by_role("button", name="Add series", exact=True)).to_be_enabled()
        expect(dialog).to_contain_text("will not remove the added series")
        dialog.get_by_role("button", name="Add series", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(search_modal.get_by_role("button", name="Choose added series")).to_be_visible()
        _run_async_blocking(_assert_no_issue_choice(parent_id, file_id, None))
        assert len(adds) == 1 and adds[0]["source"] == source.value
        search_modal.get_by_role("button", name="Choose added series").click()
        expect(choices.get_by_role("button", name="Use series", exact=True)).to_have_count(1)
        series_id = int(choices.locator('input[name="library_series_id"]').input_value())

        async def new_issue():
            from pullbox.database import get_session_factory

            async with get_session_factory()() as session:
                return await session.scalar(select(Issue.id).where(Issue.series_id == series_id))

        issue_id = _run_async_blocking(new_issue())
    selected = choices.locator(f'form:has(input[name="library_series_id"][value="{series_id}"])')
    # Existing catalogs may be beyond the first library page; use the real search field.
    if not selected.count():
        choices.get_by_role("searchbox").fill("Follow-up Lights")
        choices.get_by_role("button", name="Search library", exact=True).click()
        while not selected.count():
            choices.get_by_role("button", name="Next", exact=True).click()
    selected.get_by_role("button", name="Use series", exact=True).click()
    recovery = page.get_by_test_id("import-orphaned-recovery-modal")
    expect(recovery).to_be_visible()
    _run_async_blocking(_assert_no_issue_choice(parent_id, file_id, series_id))
    recovery.get_by_test_id(f"orphaned-recovery-issue-trigger-{file_id}").click()
    expect(recovery.get_by_test_id("import-review-use-issue")).to_have_count(1)
    assert_no_axe_violations(
        page,
        name=f"native-series-follow-up-{namespace}-{theme}-{add_missing}",
        include=['[data-testid="import-orphaned-recovery-modal"]'],
    )
    if theme == "light" and namespace == "metron":
        page.set_viewport_size({"width": 390, "height": 844})
        expect(recovery.get_by_test_id("orphaned-recovery-save")).to_be_in_viewport(ratio=1)
    screenshots = Path(__file__).resolve().parents[2] / "test-results/native-follow-up-series"
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots / f"{browser_name}-{namespace}-{theme}-{add_missing}.png"))
    recovery.get_by_test_id("import-review-use-issue").click()
    _run_async_blocking(_assert_saved(file_id, issue_id, comic, original))
    recovery.get_by_test_id("orphaned-recovery-save").click()
    expect(recovery).not_to_be_visible(timeout=20000)
    _run_async_blocking(_assert_saved(file_id, issue_id, comic, original, imported=True))
    expect(page.get_by_test_id(f"import-orphaned-recover-{parent_id}")).not_to_be_visible()
    assert page.evaluate("window.followUpModalHost === document.getElementById('cv-search-modal')")
    assert service._metadata_service.mock_calls == []
    assert not errors

"""Browser contracts for the decision-oriented import review."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import expect

from pullbox.ui.formatters import format_issue_number
from tests.e2e.pages.import_page import ImportPage
from tests.e2e.test_import_collection_page import TestImportCollectionTab as _ImportBaseline


def test_series_file_inventory_preserves_review_and_focus(authed_page, seeded_server, browser_name):
    from pullbox.database import get_session_factory
    from tests.e2e.conftest import _run_async_blocking
    from tests.ui.test_import_review_files_inventory import _seed_inventory

    job_id, series_id = _run_async_blocking(_seed_inventory(get_session_factory()))
    page = authed_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/import?tab=collection&resume_job_id={job_id}&resume_step=3")
    page.get_by_test_id("import-review-lane-decide").click()
    page.get_by_test_id("import-review-reason-needs_series").click()
    row = page.locator(f'[data-import-review-series-row="{series_id}"]')
    expander = row.locator("td:last-child > [data-import-review-expand-action]")
    expander.click()
    expect(row.get_by_text("Files in this folder", exact=True)).to_have_count(0)
    expect(row.get_by_test_id("import-review-series-file-details")).to_have_count(0)
    page.evaluate("""() => {
        window.inventoryShell = document.getElementById('import-step-review-shell');
        window.inventoryUrl = window.location.href;
        window.inventoryScroll = document.getElementById('content')?.scrollTop || 0;
    }""")
    trigger = row.get_by_test_id("import-review-more-actions")
    trigger.click()
    menu = page.locator("[popover]:popover-open")
    expect(menu.get_by_role("button")).to_have_text(["View files"])
    menu.get_by_role("button", name="View files", exact=True).click()
    modal = page.get_by_test_id("import-review-files-modal")
    dialog = modal.get_by_role("dialog", name="Files recorded for Unknown Series")
    expect(dialog).to_be_visible()
    expect(dialog).to_be_focused()
    expect(dialog.locator("[data-import-review-inventory-file]")).to_have_count(25)
    expect(dialog).to_contain_text("Missing reference")
    page.evaluate(
        "window.inventoryDialog = document.querySelector('[data-testid=import-review-files-modal]')"
    )
    dialog.get_by_test_id("series-pagination-next").click()
    expect(dialog.locator("[data-import-review-inventory-file]")).to_have_count(2)
    assert page.evaluate(
        "window.inventoryDialog === document.querySelector('[data-testid=import-review-files-modal]')"
    )
    expect(dialog).to_be_focused()
    close = dialog.get_by_role("button", name="Close", exact=True)
    assert close.evaluate(
        "el => parseFloat(getComputedStyle(el).borderTopWidth) >= 1 && el.getBoundingClientRect().height >= 28"
    )
    close.focus()
    page.keyboard.press("Tab")
    expect(dialog.get_by_test_id("series-pagination-prev")).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(close).to_be_focused()
    for theme in ("light", "dark"):
        page.evaluate("theme => applyTheme(theme)", theme)
        page.add_script_tag(path="node_modules/axe-core/axe.min.js")
        assert (
            page.evaluate("""async () => (await axe.run('[data-testid=import-review-files-modal]', {
            runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] }
        })).violations.map(v => v.id)""")
            == []
        )
    close.click()
    expect(modal).to_have_count(0)
    expect(trigger).to_be_focused()
    expect(expander).to_have_attribute("aria-expanded", "true")
    assert page.evaluate("""() => window.inventoryShell === document.getElementById('import-step-review-shell') &&
        window.inventoryUrl === window.location.href &&
        window.inventoryScroll === (document.getElementById('content')?.scrollTop || 0)""")
    page.set_viewport_size({"width": 390, "height": 844})
    trigger.click()
    page.locator("[popover]:popover-open").get_by_role("button", name="View files").click()
    expect(modal).to_be_visible()
    assert dialog.evaluate("el => el.getBoundingClientRect().right <= window.innerWidth")
    page.keyboard.press("Escape")
    expect(modal).to_have_count(0)
    expect(trigger).to_be_focused()
    assert errors == []


def test_one_page_review_shortcuts_and_file_decisions_preserve_the_row(
    authed_page, seeded_server, browser_name
):
    from pullbox.database import get_session_factory
    from tests.e2e.conftest import _run_async_blocking
    from tests.ui.test_import_one_page_review import _seed_one_page_job

    seeded = _run_async_blocking(_seed_one_page_job(get_session_factory()))
    page = authed_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(
        f"{seeded_server}/import?tab=collection&resume_job_id={seeded['job_id']}&resume_step=3"
    )
    page.get_by_test_id("import-review-lane-decide").click()
    page.get_by_test_id("import-review-reason-single_page_comic").click()
    summary = page.get_by_test_id("import-review-safety-category-summary")
    expect(summary).to_contain_text("2 files in 1 series")
    expect(page.get_by_role("button", name="Select all ready", exact=True)).to_have_count(0)
    expect(page.get_by_test_id("import-review-one-page-review")).to_have_count(0)
    page.locator("td:last-child > [data-import-review-one-page-expand]").click()
    detail = page.get_by_test_id("import-review-one-page-files")
    expect(detail).to_be_visible()
    expect(detail.get_by_role("button", name="Allow", exact=True)).to_have_count(2)
    expect(detail.get_by_role("button", name="View File", exact=True)).to_have_count(2)
    expect(page.get_by_test_id("import-review-one-page-allow")).to_contain_text("Allow All")
    expect(page.get_by_test_id("import-review-more-actions")).to_have_count(0)
    expect(page.get_by_test_id("import-review-one-page-more")).to_have_count(0)
    expect(page.get_by_role("button", name="Change series", exact=True)).to_have_count(0)
    table = page.get_by_test_id("import-review-workspace-table")
    expect(table.get_by_role("columnheader")).to_have_text(
        ["Series", "Files", "What needs attention", "Action", "Details"]
    )
    detection = page.get_by_test_id("import-review-one-page-detection").bounding_box()
    actions = page.get_by_test_id("import-review-one-page-category-actions").bounding_box()
    helper = page.get_by_test_id("import-review-one-page-helper").bounding_box()
    skip = page.get_by_test_id(
        "import-review-safety-bulk-skip-preview-single_page_comic"
    ).bounding_box()
    assert detection and actions and helper and skip
    assert actions["y"] > detection["y"] + detection["height"]
    assert helper["y"] + helper["height"] < skip["y"]
    assert abs(helper["x"] - skip["x"]) < 2
    assert abs(detection["x"] - skip["x"]) < 2
    expander = page.locator("td:last-child > [data-import-review-one-page-expand]")
    expect(expander.locator("xpath=..")).not_to_contain_text("Allow All")
    page.evaluate("""() => {
        window.onePageShell = document.getElementById('import-step-review-shell');
        window.onePageFiles = document.querySelector('[data-testid="import-review-one-page-files"]');
    }""")
    page.get_by_test_id("import-review-safety-bulk-skip-preview-single_page_comic").click()
    preview = page.get_by_test_id("import-review-safety-bulk-confirmation")
    expect(preview).to_be_visible()
    expect(preview).to_contain_text("2 one-page archives")
    assert errors == []
    assert (
        page.evaluate(
            "captureImportReviewViewport(document.getElementById('import-step-review-shell')).expandedRows.length"
        )
        == 1
    )
    preview.get_by_role("button", name="Keep them in review", exact=True).click()
    expect(preview).not_to_be_visible()
    assert errors == []
    expect(detail).to_be_visible()
    output = Path(__file__).resolve().parents[2] / "test-results/review-workspace"
    output.mkdir(parents=True, exist_ok=True)
    for theme in ("dark", "light"):
        page.evaluate("theme => applyTheme(theme)", theme)
        page.add_script_tag(path="node_modules/axe-core/axe.min.js")
        assert (
            page.evaluate("""async () => (await axe.run('#import-step-review-shell', {
            runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] }
        })).violations.map(v => v.id)""")
            == []
        )
        page.screenshot(path=str(output / f"{browser_name}-one-page-{theme}.png"), full_page=True)
    detail.get_by_role("button", name="Skip", exact=True).first.click()
    expect(detail.get_by_role("button", name="Skip", exact=True)).to_have_count(1)
    expect(detail.locator("[data-import-review-file-outcome]")).to_have_text("Skipped")
    expect(detail).to_be_visible()
    expect(page.get_by_test_id("import-review-one-page-allow")).to_contain_text("Allow All")
    assert page.evaluate("""() =>
        window.onePageShell === document.getElementById('import-step-review-shell') &&
        window.onePageFiles === document.querySelector('[data-testid="import-review-one-page-files"]')
    """)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_test_id("import-review-one-page-skip").get_by_role(
        "button", name="Skip All"
    ).click()
    expect(detail).to_have_count(0)
    expect(page.get_by_test_id("import-review-workspace-table")).to_contain_text(
        "Nothing to review"
    )
    assert errors == []


def test_one_page_view_file_uses_reader_and_returns_to_review(
    authed_page, seeded_server, tmp_path, browser_name
):
    from pullbox.core.library_file_ownership import build_file_identity_signature
    from pullbox.database import get_session_factory
    from pullbox.models.import_job import ImportedFile, ImportJob
    from tests.api.test_reader_api import _write_cbz
    from tests.e2e.conftest import _run_async_blocking
    from tests.ui.test_import_one_page_review import _seed_one_page_job

    async def seed():
        factory = get_session_factory()
        seeded = await _seed_one_page_job(factory)
        async with factory() as session:
            (await session.get(ImportJob, seeded["job_id"])).source_path = str(tmp_path)
            for index, file_id in enumerate(seeded["file_ids"][:2]):
                source = tmp_path / f"Preview {index}.cbz"
                if index:
                    source.write_bytes(b"PK\x03\x04broken archive")
                else:
                    _write_cbz(source, page_count=1)
                file = await session.get(ImportedFile, file_id)
                file.file_path = str(source)
                file.file_name = source.name
                file.source_signature = build_file_identity_signature(source)
            await session.commit()
        return seeded

    seeded = _run_async_blocking(seed())
    page = authed_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(
        f"{seeded_server}/import?tab=collection&resume_job_id={seeded['job_id']}&resume_step=3"
    )
    page.get_by_test_id("import-review-lane-decide").click()
    page.get_by_test_id("import-review-reason-single_page_comic").click()
    page.locator("td:last-child > [data-import-review-one-page-expand]").click()
    detail = page.get_by_test_id("import-review-one-page-files")
    view = detail.get_by_role("button", name="View File", exact=True).first
    page.evaluate(
        "window.previewRowBefore = document.querySelector('[data-testid=import-review-one-page-files]')"
    )
    view.click()
    dialog = page.get_by_test_id("comic-reader-dialog")
    expect(dialog).to_be_visible()
    expect(page.get_by_test_id("comic-reader-page")).to_be_visible()
    expect(dialog.get_by_test_id("comic-reader-active-download")).to_have_count(0)
    expect(dialog.get_by_test_id("comic-reader-mark-unread")).to_have_count(0)
    page.add_script_tag(path="node_modules/axe-core/axe.min.js")
    assert (
        page.evaluate("""async () => (await axe.run('[data-testid=comic-reader-dialog]', {
        runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] }
    })).violations.map(v => v.id)""")
        == []
    )
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(view).to_be_focused()
    expect(detail).to_be_visible()
    assert page.evaluate(
        "window.previewRowBefore === document.querySelector('[data-testid=import-review-one-page-files]')"
    )
    detail.get_by_role("button", name="View File", exact=True).nth(1).click()
    error = page.get_by_test_id("comic-reader-error")
    expect(error).to_be_visible()
    expect(error).to_contain_text("damaged")
    expect(error).to_contain_text("Skip")
    output = Path(__file__).resolve().parents[2] / "test-results/review-workspace"
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"{browser_name}-preview-error.png"))
    error.get_by_role("button", name="Close", exact=True).click()
    expect(detail).to_be_visible()
    detail.get_by_role("button", name="Skip", exact=True).nth(1).click()
    expect(detail.locator("[data-import-review-file-outcome]")).to_have_text("Skipped")
    expect(detail.get_by_role("button", name="Allow", exact=True)).to_have_count(1)
    assert errors == []


def test_review_file_modal_submits_without_boosted_navigation(authed_page, seeded_server):
    page = authed_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    _ImportBaseline()._goto_review_step(ImportPage(page, seeded_server), page, seeded_server)
    templates = Environment(
        loader=FileSystemLoader("src/pullbox/ui/templates"), autoescape=select_autoescape()
    )
    templates.filters["issue_num"] = format_issue_number
    html = templates.get_template("partials/import_review_file_action.html").render(
        job={"id": 1},
        file={"id": 1, "file_name": "Recheck source.cbz"},
        token="test-preview",
        action="source",
        source_action="recheck",
        candidate=None,
    )
    page.route(
        "**/import/1/files/1/source",
        lambda route: route.fulfill(json={"message": "Source verification queued."}),
    )
    page.evaluate(
        """html => {
        window.reviewShellBeforeSubmit = document.getElementById('import-step-review-shell');
        window.reviewBoostedSubmits = 0;
        document.addEventListener('htmx:beforeRequest', event => {
            if (event.detail.requestConfig.boosted) window.reviewBoostedSubmits++;
        });
        const host = document.getElementById('cv-search-modal');
        host.innerHTML = html;
        htmx.process(host);
    }""",
        html,
    )
    modal = page.get_by_test_id("import-review-file-action")
    expect(modal).to_be_visible()
    modal.get_by_role("button", name="Recheck source", exact=True).click()
    expect(modal).not_to_be_visible()
    expect(page.get_by_test_id("import-review-lane-ready")).to_be_visible()
    assert page.evaluate("window.reviewBoostedSubmits") == 0
    assert page.evaluate(
        "window.reviewShellBeforeSubmit === document.getElementById('import-step-review-shell')"
    )
    assert errors == []


def test_review_lanes_and_refresh_preserve_controls(authed_page, seeded_server, browser_name):
    page = authed_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    _ImportBaseline()._goto_review_step(ImportPage(page, seeded_server), page, seeded_server)
    outcome = page.evaluate("""() => importCvSearchModalData({ jobId: 1, seriesId: 1 })
        .buildOverrideOutcome({ status: 'no_match', cv_id: 123, files_no_match: 1 }, 'ready', {})""")
    assert "destinationView" not in outcome
    assert "Needs Issue Match" not in outcome["message"]
    page.evaluate("""() => {
        window.reviewShell = document.getElementById('import-step-review-shell');
        window.reviewCancel = document.querySelector('[data-testid="import-review-cancel"]');
        window.reviewButton = document.querySelector('[data-import-review-import-button]');
    }""")
    page.get_by_test_id("import-review-lane-ready").click()
    expect(page.locator('input[name="review_status_filter"]')).to_have_value("ready")
    page.locator("[data-import-review-selectable]").first.check()
    expect(page.locator("[data-import-review-import-button]")).to_be_enabled()
    action = page.locator("td:last-child > [data-import-review-expand-action]").first
    action.click()
    expect(action).to_have_attribute("aria-expanded", "true")
    menu_trigger = page.get_by_test_id("import-review-more-actions").first
    menu_trigger.click()
    menu = page.locator("[popover]:popover-open")
    expect(menu).to_be_visible()
    menu.get_by_role("button", name="Skip series", exact=True).click()
    skip_dialog = page.get_by_role("dialog", name="Skip this series?")
    expect(skip_dialog).to_be_visible()
    skip_dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(skip_dialog).not_to_be_visible()
    expect(action).to_have_attribute("aria-expanded", "true")
    page.evaluate("""async () => {
        await Alpine.$data(window.reviewShell).refreshSeriesReview();
    }""")
    expect(action).to_have_attribute("aria-expanded", "true")
    assert page.evaluate("""() =>
        window.reviewShell === document.getElementById('import-step-review-shell') &&
        window.reviewCancel === document.querySelector('[data-testid="import-review-cancel"]') &&
        window.reviewButton === document.querySelector('[data-import-review-import-button]')
    """)
    page.locator("[data-import-review-import-button]").click()
    expect(page.get_by_test_id("import-review-gate")).to_be_visible()
    page.get_by_role("button", name="Back to review", exact=True).click()
    expect(page.get_by_test_id("import-review-gate")).not_to_be_visible()
    expect(page.locator("[data-import-review-import-button]")).to_be_focused()
    assert page.get_by_role("button", name="Select all ready", exact=True).evaluate(
        "el => parseFloat(getComputedStyle(el).borderTopWidth) >= 1"
    )
    assert action.evaluate("el => el.getBoundingClientRect().height >= 28")
    assert page.evaluate("""async () => {
        const shell = document.getElementById('import-step-review-shell');
        const data = Alpine.$data(shell);
        const html = await (await fetch(data.buildReviewUrl())).text();
        const documentCopy = new DOMParser().parseFromString(html, 'text/html');
        documentCopy.querySelector('#import-review-state').setAttribute('data-requires-preferred-root', 'true');
        applyImportReviewWorkspace(documentCopy.body.innerHTML, shell);
        const requiresRoot = data.splitSeriesRequiresPreferredRoot;
        applyImportReviewWorkspace(html, shell);
        return requiresRoot && !data.splitSeriesRequiresPreferredRoot;
    }""")
    page.add_script_tag(path="node_modules/axe-core/axe.min.js")
    accessibility = page.evaluate("""async () => {
        const result = await axe.run('#import-step-review-shell', {
            runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] }
        });
        return result.violations.map(v => ({ id: v.id, nodes: v.nodes.map(n => n.target) }));
    }""")
    assert accessibility == []
    output = Path(__file__).resolve().parents[2] / "test-results/review-workspace"
    output.mkdir(parents=True, exist_ok=True)
    for theme in ("dark", "light"):
        page.evaluate("theme => applyTheme(theme)", theme)
        expect(action).to_have_attribute("aria-expanded", "true")
        page.screenshot(path=str(output / f"{browser_name}-{theme}.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator("[data-import-review-import-button]").click()
    gate = page.get_by_test_id("import-review-gate")
    expect(gate).to_be_visible()
    expect(gate).to_contain_text("1 ready file from 1 selected series will import.")
    expect(gate).to_contain_text("1 file still needs attention.")
    expect(gate.get_by_role("button", name="Back to review", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(output / f"{browser_name}-mobile-gate.png"), full_page=True)
    page.keyboard.press("Escape")
    expect(gate).not_to_be_visible()
    assert errors == []


def test_inline_issue_choice_submits_only_its_file_without_navigation(authed_page, seeded_server):
    page = authed_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    _ImportBaseline()._goto_review_step(ImportPage(page, seeded_server), page, seeded_server)
    templates = Environment(
        loader=FileSystemLoader("src/pullbox/ui/templates"), autoescape=select_autoescape()
    )
    templates.filters["issue_num"] = format_issue_number
    html = templates.get_template("partials/import_review_issue_choices.html").render(
        job={"id": 1},
        file={"id": 1, "file_name": "Needs issue.cbz"},
        series={"provider_id": "20", "title": "Test series"},
        issues=[{"provider_id": "201", "issue_number": 1, "title": "First issue"}],
        token="scoped-test-token",
        query="",
        issue_page=1,
        issue_page_count=1,
    )
    submissions = []

    def assign(route):
        submissions.append(route.request.post_data)
        route.fulfill(json={"message": "Issue assigned."})

    page.route("**/import/1/files/1/assign", assign)
    page.evaluate(
        """html => {
        window.inlineReviewShell = document.getElementById('import-step-review-shell');
        const host = document.querySelector('#import-review-lane-panel');
        host.insertAdjacentHTML('afterbegin', html);
        htmx.process(host);
    }""",
        html,
    )
    panel = page.get_by_test_id("import-review-issue-choices")
    expect(panel).to_be_visible()
    panel.get_by_role("button", name="Use this", exact=True).click()
    expect(panel).not_to_be_visible()
    assert len(submissions) == 1
    assert 'name="issue_cv_id"\r\n\r\n201' in submissions[0]
    assert 'name="token"\r\n\r\nscoped-test-token' in submissions[0]
    assert page.evaluate(
        "window.inlineReviewShell === document.getElementById('import-step-review-shell')"
    )
    assert errors == []

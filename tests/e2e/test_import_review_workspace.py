"""Browser contracts for the decision-oriented import review."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import expect

from pullbox.ui.formatters import format_issue_number
from tests.e2e.pages.import_page import ImportPage
from tests.e2e.test_import_collection_page import TestImportCollectionTab as _ImportBaseline


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
    expect(page.get_by_role("button", name="Continue to import", exact=True)).to_be_enabled()
    action = page.get_by_test_id("import-review-primary-action").first
    action.click()
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
    page.get_by_role("button", name="Continue to import", exact=True).click()
    expect(page.get_by_test_id("import-review-gate")).to_be_visible()
    page.get_by_role("button", name="Back to review", exact=True).click()
    expect(page.get_by_test_id("import-review-gate")).not_to_be_visible()
    expect(page.get_by_role("button", name="Continue to import", exact=True)).to_be_focused()
    assert page.get_by_role("button", name="Select all ready", exact=True).evaluate(
        "el => parseFloat(getComputedStyle(el).borderTopWidth) >= 1"
    )
    assert action.evaluate("el => el.getBoundingClientRect().height >= 36")
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
    output = Path("test-results/review-workspace")
    output.mkdir(parents=True, exist_ok=True)
    for theme in ("dark", "light"):
        page.evaluate("theme => applyTheme(theme)", theme)
        expect(action).to_have_attribute("aria-expanded", "true")
        page.screenshot(path=str(output / f"{browser_name}-{theme}.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("button", name="Continue to import", exact=True).click()
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

"""Opt-in nested repair preview uses the existing import review shell."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_nested_repair_preview_both_themes(authed_page, seeded_server, browser_name):
    from pullbox.database import get_session_factory
    from pullbox.models.import_job import ImportedFile
    from pullbox.services.import_safety_diagnostics import build_import_safety_diagnostics
    from tests.e2e.conftest import _run_async_blocking
    from tests.ui.test_import_safety_bulk_ui import _seed_bulk_safety_job

    async def seed():
        factory = get_session_factory()
        seeded = await _seed_bulk_safety_job(factory)
        async with factory() as session:
            file = await session.get(ImportedFile, seeded["file_ids"][0])
            file.source_signature = {"schema_version": 1}
            file.diagnostics = {
                "safety_block": build_import_safety_diagnostics(
                    "nested_comic_archive", code="nested_comic_archive"
                ),
                "source_metadata": {
                    "nested_comic": {
                        "eligible": True,
                        "inner_name": "Batman 001.cbr",
                        "inner_format": "cbr",
                        "page_count": 35,
                        "metadata_sources": ["outer"],
                    }
                },
            }
            await session.commit()
        return seeded["job_id"]

    job_id = _run_async_blocking(seed())
    page = authed_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{seeded_server}/import?tab=collection&resume_job_id={job_id}&resume_step=3")
    # The seeded group also contains large-file decisions, so its primary lane
    # remains Needs a decision even though the nested file needs source repair.
    page.get_by_test_id("import-review-lane-decide").click()
    expect(
        page.locator("#import-step-review-shell input[name='review_status_filter']")
    ).to_have_value("decide")
    page.get_by_test_id("import-review-reason-nested_comic_archive").click()
    expect(page.get_by_test_id("import-review-reason-nested_comic_archive")).to_have_attribute(
        "aria-pressed", "true"
    )
    page.get_by_role("button", name="Review nested repairs").click()
    preview = page.get_by_test_id("nested-repair-preview")
    expect(preview).to_be_visible()
    expect(preview).to_contain_text("1 eligible file will")
    expect(preview).to_contain_text("Rollback removes the imported copies, not the originals.")
    approve = preview.get_by_role("button", name="Approve 1 repair")
    approve.focus()
    expect(approve).to_be_focused()
    page.add_script_tag(path="node_modules/axe-core/axe.min.js")
    output = Path(__file__).resolve().parents[2] / "output/playwright"
    output.mkdir(parents=True, exist_ok=True)
    for theme in ("dark", "light"):
        page.evaluate("theme => applyTheme(theme)", theme)
        assert (
            page.evaluate("""async () => (await axe.run('#import-step-review-shell', {
            runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa']}
        })).violations.map(v => ({id:v.id, nodes:v.nodes.map(n => n.target)}))""")
            == []
        )
        page.screenshot(
            path=str(output / f"{browser_name}-nested-repair-{theme}.png"), full_page=True
        )
    assert errors == []

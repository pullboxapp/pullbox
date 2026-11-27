"""A fresh folder check is read-only, including after losing a write response."""

import json

import pytest
from playwright.sync_api import expect

from pullbox.database import get_session_factory
from pullbox.models import LibraryRoot, Series
from pullbox.models.metadata_identity import SeriesExternalIdentity
from tests.e2e.conftest import _run_async_blocking

pytestmark = pytest.mark.e2e


@pytest.fixture
def managed_series(tmp_path, seeded_server):
    folder = tmp_path / "Planet Atmos"
    folder.mkdir()
    sidecar = folder / "series.json"
    sidecar.write_text('{"custom":"keep me"}')

    async def seed():
        async with get_session_factory().begin() as session:
            root = LibraryRoot(name="Sidecar test", path=str(tmp_path))
            session.add(root)
            await session.flush()
            series = Series(
                title="Planet Atmos",
                sort_title="planet atmos",
                path=str(folder),
                library_root_id=root.id,
            )
            session.add(series)
            await session.flush()
            series.comicvine_id = 91800000 + series.id
            session.add(
                SeriesExternalIdentity(
                    series_id=series.id,
                    identity_namespace="comicvine",
                    external_id=str(series.comicvine_id),
                    verification_state="verified",
                    evidence_kind="user_selection",
                )
            )
            return series.id

    return _run_async_blocking(seed()), sidecar


def _open(page, server, series_id):
    page.goto(f"{server}/series/{series_id}")
    page.get_by_role("button", name="Write series metadata", exact=True).click()
    dialog = page.get_by_role("dialog", name="Write series metadata")
    expect(dialog.get_by_role("button", name="Write series.json")).to_be_enabled()
    return dialog


@pytest.mark.parametrize("bad_preview", ["unavailable", "missing-review"])
def test_failed_sidecar_repreview_cannot_submit_an_older_review(
    authed_page, seeded_server, managed_series, bad_preview
):
    series_id, sidecar = managed_series
    page = authed_page
    dialog = _open(page, seeded_server, series_id)
    pending = []
    page.route("**/sidecar/preview", lambda route: pending.append(route), times=1)
    again = dialog.get_by_role("button", name="Preview again")
    with page.expect_request("**/sidecar/preview"):
        again.click()
    expect(dialog.get_by_text("Preparing series metadata preview...", exact=True)).to_be_visible()
    if bad_preview == "unavailable":
        pending.pop().fulfill(status=503, json={"detail": "Folder check unavailable."})
    else:
        pending.pop().fulfill(json={"ready": True})
    expect(dialog.get_by_role("alert")).to_be_visible()
    write = dialog.get_by_role("button", name="Write series.json")
    expect(write).to_be_disabled()
    expect(dialog.get_by_test_id("series-sidecar-targets")).to_be_hidden()
    again.click()
    expect(write).to_be_enabled()
    assert json.loads(sidecar.read_bytes()) == {"custom": "keep me"}


@pytest.mark.parametrize("outcome", ["lost-after-write", "not-sent", "incomplete-receipt"])
def test_lost_sidecar_write_is_checked_without_rewriting(
    authed_page, seeded_server, managed_series, outcome
):
    series_id, sidecar = managed_series
    page = authed_page
    dialog = _open(page, seeded_server, series_id)
    attempts = []
    reached_server = outcome != "not-sent"

    def lose_response(route):
        attempts.append(route.request.post_data_json)
        if reached_server:
            response = route.fetch()
            assert response.ok, response.status
        if outcome == "incomplete-receipt":
            route.fulfill(json={})
        else:
            route.abort("failed")

    page.route("**/sidecar/write", lose_response)
    write = dialog.get_by_role("button", name="Write series.json")
    write.click()
    expect(dialog.get_by_role("alert")).to_contain_text("could not confirm")
    expect(write).to_be_disabled()
    before = (sidecar.stat().st_ino, sidecar.stat().st_mtime_ns, sidecar.read_bytes())
    assert ("pullbox" in json.loads(before[2])) == reached_server
    assert json.loads(before[2])["custom"] == "keep me"
    check = dialog.get_by_role("button", name="Check series.json folders")
    expect(check).to_be_focused()
    if outcome == "not-sent":
        page.route("**/sidecar/preview", lambda route: route.fulfill(status=503), times=1)
        check.click()
        expect(dialog.get_by_role("alert")).to_be_visible()
        expect(write).to_be_disabled()
    check.click()
    expect(dialog.get_by_role("status")).to_contain_text("This check did not write any files")
    expect(
        dialog.get_by_text("Up to date" if reached_server else "Update series.json", exact=True)
    ).to_be_visible()
    if reached_server:
        expect(write).to_be_disabled()
    else:
        expect(write).to_be_enabled()
    assert (sidecar.stat().st_ino, sidecar.stat().st_mtime_ns, sidecar.read_bytes()) == before
    assert len(attempts) == 1


def test_changed_sidecar_preserves_the_server_refusal(authed_page, seeded_server, managed_series):
    series_id, sidecar = managed_series
    dialog = _open(authed_page, seeded_server, series_id)
    sidecar.write_text('{"custom":"changed since preview"}')
    write = dialog.get_by_role("button", name="Write series.json")
    write.click()
    expect(dialog.get_by_role("alert")).to_contain_text("changed after preview")
    expect(write).to_be_disabled()
    expect(dialog.get_by_role("button", name="Check series.json folders")).to_have_count(0)
    dialog.get_by_role("button", name="Preview again").click()
    expect(write).to_be_enabled()
    assert json.loads(sidecar.read_bytes()) == {"custom": "changed since preview"}

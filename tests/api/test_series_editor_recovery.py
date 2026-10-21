"""Sidecar retries retain their exact scope and never reapply library corrections."""

from datetime import UTC, datetime

from pullbox.models import Issue, LibraryFile, Series
from pullbox.models.library import FileFormat
from pullbox.services import series_sidecar
from tests.api.test_metadata_sources_api import csrf
from tests.api.test_series_metadata_editor import (
    approve,
    endpoint,
)
from tests.api.test_series_metadata_editor import (
    metadata_queue as _metadata_queue,
)
from tests.api.test_series_metadata_editor import (
    sec_db as _sec_db,
)
from tests.api.test_series_metadata_editor import (
    sidecar_target as _sidecar_target,
)

pytest_plugins = ["conftest_security"]
metadata_queue = _metadata_queue
sec_db = _sec_db
sidecar_target = _sidecar_target


async def retry(client, series_id, job_id):
    response = await client.post(
        endpoint(series_id) + "/retry", json={"job_id": job_id}, headers=csrf(client)
    )
    assert response.status_code == 202, response.text
    return response.json()


async def test_split_folder_retry_does_not_rewrite_completed_targets(
    authenticated_client, sidecar_target, sec_db, metadata_queue, monkeypatch
):
    series_id, folder, root = sidecar_target
    second = root / "Second"
    second.mkdir()
    comic = second / "Issue 002.cbz"
    comic.write_bytes(b"Original comic unchanged")
    async with sec_db.begin() as session:
        series = await session.get(Series, series_id)
        issue = Issue(series_id=series_id, issue_number=2)
        session.add(issue)
        await session.flush()
        session.add(
            LibraryFile(
                issue_id=issue.id,
                library_root_id=series.library_root_id,
                file_path=str(comic),
                file_name=comic.name,
                file_size=comic.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
            )
        )
    original = series_sidecar._publish

    def failure(target, descriptor, stage):
        if target.location.directory == str(second):
            raise OSError("Simulated unavailable storage")
        return original(target, descriptor, stage)

    monkeypatch.setattr(series_sidecar, "_publish", failure)
    receipt = await approve(
        authenticated_client,
        series_id,
        {
            "edits": {"title": {"action": "set", "value": "Reviewed title"}},
        },
    )
    await metadata_queue.dispatch_next()
    report = (await authenticated_client.get(endpoint(series_id) + "/job")).json()
    assert report["library_saved"] and report["partial"] and report["retryable"], report
    assert {target["state"] for target in report["targets"]} == {"COMPLETED", "FAILED"}
    completed = (folder / "series.json").read_bytes(), (folder / "series.json").stat()
    monkeypatch.setattr(series_sidecar, "_publish", original)
    await retry(authenticated_client, series_id, receipt["job_id"])
    await metadata_queue.dispatch_next()
    final = (await authenticated_client.get(endpoint(series_id) + "/job")).json()
    assert not final["partial"] and not final["retryable"], final
    assert final["job"]["state"] == "COMPLETED"
    assert ((folder / "series.json").read_bytes(), (folder / "series.json").stat()) == completed
    assert (second / "series.json").exists()
    assert comic.read_bytes() == b"Original comic unchanged"


async def test_lost_publication_receipt_replays_without_rewriting(
    authenticated_client, sidecar_target, metadata_queue, monkeypatch
):
    series_id, folder, _ = sidecar_target
    original = series_sidecar._publish

    def lost_receipt(target, descriptor, stage):
        original(target, descriptor, stage)
        raise OSError("Simulated lost publication acknowledgement")

    monkeypatch.setattr(series_sidecar, "_publish", lost_receipt)
    receipt = await approve(
        authenticated_client,
        series_id,
        {
            "edits": {"description": {"action": "set", "value": "Reviewed summary"}},
        },
    )
    await metadata_queue.dispatch_next()
    sidecar = folder / "series.json"
    published = sidecar.read_bytes(), sidecar.stat()
    report = (await authenticated_client.get(endpoint(series_id) + "/job")).json()
    assert report["partial"] and report["retryable"], report
    monkeypatch.setattr(series_sidecar, "_publish", original)
    await retry(authenticated_client, series_id, receipt["job_id"])
    await metadata_queue.dispatch_next()
    final = (await authenticated_client.get(endpoint(series_id) + "/job")).json()
    assert final["job"]["state"] == "COMPLETED", final
    assert (sidecar.read_bytes(), sidecar.stat()) == published


async def test_cancelled_queued_sidecar_retains_saved_library_and_can_retry(
    authenticated_client, sidecar_target, sec_db, metadata_queue
):
    series_id, folder, _ = sidecar_target
    receipt = await approve(
        authenticated_client,
        series_id,
        {
            "edits": {"title": {"action": "set", "value": "Saved before cancellation"}},
        },
    )
    async with sec_db() as session:
        await metadata_queue.cancel_job(session, receipt["job_id"])
        await session.commit()
        assert (await session.get(Series, series_id)).title == "Saved before cancellation"
    report = (await authenticated_client.get(endpoint(series_id) + "/job")).json()
    assert report["job"]["state"] == "CANCELLED", report
    assert report["library_saved"] and report["retryable"], report
    assert not (folder / "series.json").exists()
    await retry(authenticated_client, series_id, receipt["job_id"])
    await metadata_queue.dispatch_next()
    assert (folder / "series.json").exists()

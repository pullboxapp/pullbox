"""The optional file inventory supports series identification without decisions."""

import re
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest_plugins = ["conftest_security"]


async def _seed_inventory(sec_db, count=27):
    async with sec_db() as session:
        job = ImportJob(
            source_path="/library",
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.REVIEW,
        )
        session.add(job)
        await session.flush()
        series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Unknown Series",
            raw_year=2024,
            status=ImportSeriesStatus.NO_MATCH,
            source_folder="/library/Unknown Series",
            files_total=count,
            files_no_match=count,
            file_count=count,
            diagnostics={"kind": "series_no_match", "top_candidates": []},
        )
        session.add(series)
        await session.flush()
        for index in range(count):
            session.add(
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=series.id,
                    file_name=f"Unknown Series {index:03}.cbz",
                    file_path=f"/library/Unknown Series/Unknown Series {index:03}.cbz",
                    file_size=1024 * (index + 1),
                    file_format="cbz",
                    status=ImportedFileStatus.SAFETY_BLOCKED
                    if index == 0
                    else ImportedFileStatus.NO_MATCH,
                    diagnostics={
                        "safety_block": {
                            "category": "source_missing",
                            "code": "source_missing",
                            "reason": "Recorded file not found",
                        }
                    }
                    if index == 0
                    else {},
                )
            )
        await session.commit()
        return job.id, series.id


async def test_series_match_details_and_menu_stay_series_focused(authenticated_client, sec_db):
    job_id, series_id = await _seed_inventory(sec_db)
    response = await authenticated_client.get(
        f"/import/{job_id}/review-partial?status=decide&reason=needs_series"
    )
    assert response.status_code == 200
    row = response.text.split(f'data-import-review-series-row="{series_id}"', 1)[1].split(
        "</tbody>", 1
    )[0]
    assert "Files in this folder" not in row
    assert 'data-testid="import-review-series-file-details"' not in row
    assert "ComicVine candidates" in row
    menu = re.search(r"<div popover.*?</div>", row, re.S).group()
    assert re.findall(r"<button\b[^>]*>(.*?)</button>", menu, re.S) == ["View files"]
    assert f'hx-get="/import/{job_id}/series/{series_id}/files"' in menu
    assert "toggleImportReviewRow" not in menu
    assert re.search(
        r'data-testid="import-review-primary-action"[^>]*>Search ComicVine</button>', row
    )
    assert "Skip series" in row
    assert 'data-testid="import-review-recheck-source"' not in row


async def test_inventory_is_paginated_read_only_and_labels_missing_references(
    authenticated_client, sec_db
):
    job_id, series_id = await _seed_inventory(sec_db)
    async with sec_db() as session:
        before = [
            (f.id, f.status, f.file_path, f.diagnostics)
            for f in (await session.scalars(select(ImportedFile).order_by(ImportedFile.id))).all()
        ]
    url = f"/import/{job_id}/series/{series_id}/files"
    response = await authenticated_client.get(url)
    assert response.status_code == 200
    html = response.text
    assert 'role="dialog" aria-modal="true"' in html
    assert "/library/Unknown Series" in html
    assert "Recorded during the scan" in html
    assert "Missing reference" in html
    assert "1.0 KB" in html
    assert html.count('data-import-review-inventory-file="') == 25
    assert "Unknown Series 025.cbz" not in html
    assert not re.search(r"hx-(post|put)=|<(input|select|form)\b", html)
    assert 'id="pagination-' not in html
    assert re.search(r'data-page-url="[^"]*files_page=2"[^>]*hx-push-url="false"', html)
    response = await authenticated_client.get(url + "?files_page=2")
    assert response.status_code == 200
    page_two = response.text
    assert page_two.count('data-import-review-inventory-file="') == 2
    assert "Unknown Series 025.cbz" in page_two
    assert "Unknown Series 000.cbz" not in page_two
    async with sec_db() as session:
        after = [
            (f.id, f.status, f.file_path, f.diagnostics)
            for f in (await session.scalars(select(ImportedFile).order_by(ImportedFile.id))).all()
        ]
        assert after == before
        assert (await session.get(ImportJob, job_id)).status == ImportJobStatus.REVIEW


async def test_inventory_rejects_series_from_another_job(authenticated_client, sec_db):
    job_id, series_id = await _seed_inventory(sec_db)
    other_job, other_series = await _seed_inventory(sec_db)
    for job, series in ((job_id, other_series), (other_job, series_id), (job_id, 999999)):
        assert (
            await authenticated_client.get(f"/import/{job}/series/{series}/files")
        ).status_code == 404


async def test_inventory_requires_authentication(unauthenticated_client, sec_db):
    job_id, series_id = await _seed_inventory(sec_db)
    response = await unauthenticated_client.get(
        f"/import/{job_id}/series/{series_id}/files", follow_redirects=False
    )
    assert response.status_code in (302, 303, 401, 403)


@pytest.mark.parametrize("count", [0, 1, 27])
async def test_inventory_bounds_pages_and_handles_empty_groups(authenticated_client, sec_db, count):
    job_id, series_id = await _seed_inventory(sec_db, count=count)
    url = f"/import/{job_id}/series/{series_id}/files"
    assert (await authenticated_client.get(url + "?files_page=0")).status_code == 422
    response = await authenticated_client.get(url + "?files_page=99999")
    assert response.status_code == 200
    html = response.text
    assert html.count('data-import-review-inventory-file="') == (2 if count == 27 else count)
    if not count:
        assert "No files were recorded for this series." in html


async def test_inventory_escapes_names_and_shows_other_source_folders(authenticated_client, sec_db):
    job_id, series_id = await _seed_inventory(sec_db, count=1)
    async with sec_db() as session:
        file = await session.scalar(select(ImportedFile))
        file.file_name = "<script>alert('file')</script>.cbz"
        file.file_path = "C:\\Mixed Folder\\comic.cbz"
        await session.commit()
    response = await authenticated_client.get(f"/import/{job_id}/series/{series_id}/files")
    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "C:/Mixed Folder" in response.text

"""Prototype v2 contracts without changing saved import decisions."""

import re

from tests.ui.test_import_collection_shell_ui_routes import _seed_import_review_job

pytest_plugins = ["conftest_security"]


async def test_v2_header_rail_and_problem_table(authenticated_client, sec_db):
    job_id = await _seed_import_review_job(sec_db)
    response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=decide")
    assert response.status_code == 200
    html = response.text
    assert 'data-testid="import-review-overview"' in html
    assert re.search(r'data-testid="import-review-progress"[^>]*role="progressbar"', html)
    workspace = html.split('id="import-review-workspace"', 1)[1]
    assert re.search(r'id="import-review-lanes"[^>]*role="tablist"', workspace)
    assert 'data-testid="import-review-lane-description"' in workspace
    assert 'data-testid="import-review-reason-all"' in workspace
    assert _headers(workspace)[:5] == [
        "Series",
        "Files",
        "What needs attention",
        "Action",
        "Details",
    ]
    assert "Select all ready" not in workspace
    assert 'data-testid="import-review-more-actions"' in html
    assert 'aria-controls="import-review-detail-' in html
    assert 'data-testid="import-review-pagination"' in html.split('id="page-footer-dock"', 1)[1]


async def test_v2_ready_table_and_gate_preserve_file_selection(authenticated_client, sec_db):
    job_id = await _seed_import_review_job(sec_db)
    response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=ready")
    html = response.text
    headers = _headers(html.split('id="import-review-workspace"', 1)[1])
    assert headers[:6] == [
        "Select all on this page",
        "Series",
        "Files",
        "ComicVine match",
        "Confidence",
        "Actions",
    ]
    assert "data-import-review-selectable" in html
    assert 'data-testid="import-review-file-select"' in html
    gate = html.split('data-testid="import-review-gate"', 1)[1]
    assert "Imports now" in gate
    assert "Stays behind" in gate
    assert "Follow-up" in gate


def _headers(html):
    return [
        " ".join(re.sub(r"<[^>]*>", " ", text).split())
        for text in re.findall(r"<th\b[^>]*>(.*?)</th>", html, re.S)
    ]


async def test_copy_choices_use_radios_without_applying_until_confirmed(
    authenticated_client, sec_db
):
    from sqlalchemy import select

    from pullbox.models.import_job import ImportedFile, ImportedFileStatus

    job_id = await _seed_import_review_job(sec_db)
    async with sec_db() as session:
        file = await session.scalar(
            select(ImportedFile).where(ImportedFile.status == ImportedFileStatus.CONFLICT)
        )
        file.file_name = "Renamed archive.cbr"
        file.diagnostics = {
            **file.diagnostics,
            "source_metadata": {
                "archive_format": {"detected": "cbz"},
                "content_inspection": {"page_count": 22},
            },
        }
        await session.commit()
    response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=confirm")
    assert response.status_code == 200
    html = response.text
    assert 'data-testid="import-review-copy-choice"' in html
    assert 'type="radio"' in html
    assert "reviewCopyChoices[" in html
    assert 'data-testid="import-review-keep-selected-copy"' in html
    assert "ZIP (detected)" in html
    assert "ComicInfo metadata present" in html


async def test_explicit_skip_series_preserves_files_and_can_be_restored(
    authenticated_client, sec_db
):
    from sqlalchemy import select

    from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportSeriesStatus
    from tests.ui.test_import_safety_bulk_ui import _csrf_header_for

    job_id = await _seed_import_review_job(sec_db)
    async with sec_db() as session:
        series = await session.scalar(
            select(ImportedSeries).where(
                ImportedSeries.import_job_id == job_id,
                ImportedSeries.status == ImportSeriesStatus.NO_MATCH,
            )
        )
        series_id, status = series.id, series.status
        files = (
            await session.scalars(
                select(ImportedFile).where(ImportedFile.import_series_id == series_id)
            )
        ).all()
        original = [(f.id, f.status, f.file_path, f.diagnostics) for f in files]
    for action in ("skip", "restore"):
        url = f"/import/{job_id}/series/{series_id}/review-{action}"
        preview = await authenticated_client.get(url)
        assert preview.status_code == 200
        token = re.search(r'name="token" value="([^"]+)"', preview.text).group(1)
        response = await authenticated_client.post(
            url, data={"token": token}, headers=_csrf_header_for(authenticated_client)
        )
        assert response.status_code == 200
        if action == "skip":
            skipped = await authenticated_client.get(f"/import/{job_id}/review-partial?status=info")
            assert 'data-testid="import-review-skipped-series"' in skipped.text
        async with sec_db() as session:
            series = await session.get(ImportedSeries, series_id)
            assert series.status == (ImportSeriesStatus.SKIPPED if action == "skip" else status)
            assert not series.selected_for_import
            files = (
                await session.scalars(
                    select(ImportedFile).where(ImportedFile.import_series_id == series_id)
                )
            ).all()
            assert [(f.id, f.status, f.file_path, f.diagnostics) for f in files] == original


async def test_skip_preview_rejects_a_changed_job(authenticated_client, sec_db):
    from pullbox.models.import_job import ImportJob, ImportJobStatus
    from tests.ui.test_import_safety_bulk_ui import _csrf_header_for

    job_id = await _seed_import_review_job(sec_db)
    url = f"/import/{job_id}/series/1/review-skip"
    preview = await authenticated_client.get(url)
    assert preview.status_code == 200
    token = re.search(r'name="token" value="([^"]+)"', preview.text).group(1)
    async with sec_db() as session:
        (await session.get(ImportJob, job_id)).status = ImportJobStatus.COMPLETED
        await session.commit()
    response = await authenticated_client.post(
        url, data={"token": token}, headers=_csrf_header_for(authenticated_client)
    )
    assert response.status_code == 409


async def test_restoring_duplicate_series_does_not_reselect_its_files(authenticated_client, sec_db):
    from sqlalchemy import select

    from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportSeriesStatus
    from tests.ui.test_import_safety_bulk_ui import _csrf_header_for

    job_id = await _seed_import_review_job(sec_db)
    async with sec_db() as session:
        series = await session.scalar(
            select(ImportedSeries).where(ImportedSeries.status == ImportSeriesStatus.DUPLICATE)
        )
        series_id = series.id
        files = (
            await session.scalars(
                select(ImportedFile).where(ImportedFile.import_series_id == series_id)
            )
        ).all()
        assert any(f.include_in_import for f in files)
        evidence = [(f.id, f.status, f.matched_issue_cv_id, f.diagnostics) for f in files]
    for action in ("skip", "restore"):
        url = f"/import/{job_id}/series/{series_id}/review-{action}"
        preview = await authenticated_client.get(url)
        token = re.search(r'name="token" value="([^"]+)"', preview.text).group(1)
        response = await authenticated_client.post(
            url, data={"token": token}, headers=_csrf_header_for(authenticated_client)
        )
        assert response.status_code == 200
    async with sec_db() as session:
        files = (
            await session.scalars(
                select(ImportedFile).where(ImportedFile.import_series_id == series_id)
            )
        ).all()
        assert not any(f.include_in_import for f in files)
        assert [(f.id, f.status, f.matched_issue_cv_id, f.diagnostics) for f in files] == evidence


async def test_resolved_one_page_children_remain_until_group_is_finished(
    authenticated_client, sec_db
):
    from tests.ui.test_import_one_page_review import _review, _seed_one_page_job
    from tests.ui.test_import_safety_bulk_ui import _csrf_header_for

    seeded = await _seed_one_page_job(sec_db)
    response = await authenticated_client.post(
        f"/import/{seeded['job_id']}/files/{seeded['file_ids'][0]}/safety/skip?status=decide&reason=single_page_comic",
        headers=_csrf_header_for(authenticated_client),
    )
    assert response.status_code == 200
    html = (await _review(authenticated_client, seeded)).text
    assert f'data-import-review-file-outcome="{seeded["file_ids"][0]}"' in html
    assert ">Skipped</span>" in html
    assert html.count('data-testid="import-review-skip-safety-file"') == 1


async def test_resolved_copy_children_remain_while_the_series_has_unmatched_files(
    authenticated_client, sec_db
):
    from sqlalchemy import select

    from pullbox.models.import_job import ImportedFile, ImportedFileStatus
    from tests.ui.test_import_safety_bulk_ui import _csrf_header_for

    job_id = await _seed_import_review_job(sec_db)
    async with sec_db() as session:
        files = (
            await session.scalars(
                select(ImportedFile)
                .where(ImportedFile.import_series_id == 7)
                .order_by(ImportedFile.id)
            )
        ).all()
        keeper, discarded, pending = files
        keeper_id, discarded_id, group_id = keeper.id, discarded.id, keeper.conflict_group_id
        pending.status = ImportedFileStatus.NO_MATCH
        await session.commit()
    response = await authenticated_client.put(
        f"/api/v1/import/{job_id}/conflicts/{group_id}/resolve",
        json={"chosen_file_id": keeper_id},
        headers=_csrf_header_for(authenticated_client),
    )
    assert response.status_code == 200
    html = (await authenticated_client.get(f"/import/{job_id}/review-partial?status=decide")).text
    assert f'data-import-review-file-outcome="{keeper_id}"' in html
    assert f'data-import-review-file-outcome="{discarded_id}"' in html

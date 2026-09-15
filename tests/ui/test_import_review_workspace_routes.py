"""Route behavior for the task-oriented import review workspace."""

from __future__ import annotations

import pytest

from tests.ui.test_import_collection_shell_ui_routes import _seed_import_review_job

pytest_plugins = ["conftest_security"]


@pytest.mark.asyncio
async def test_review_has_exclusive_lanes_and_a_clear_import_gate(
    authenticated_client, sec_db
) -> None:  # type: ignore[no-untyped-def]
    job_id = await _seed_import_review_job(sec_db)
    response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=decide")
    assert response.status_code == 200
    html = response.text
    for lane in ("decide", "confirm", "fix_source", "blocked", "ready", "info"):
        assert f'data-testid="import-review-lane-{lane}"' in html
    assert 'data-testid="import-review-primary-action"' in html
    assert 'data-testid="import-review-gate"' in html
    assert "Continue to import" in html
    assert "Follow-up" in html
    assert "Save conflict choices" not in html
    assert 'role="progressbar"' not in html
    assert "Import all ready comics" not in html


@pytest.mark.asyncio
async def test_ready_selection_and_job_scoped_followup_survive_redesign(
    authenticated_client, sec_db
) -> None:  # type: ignore[no-untyped-def]
    job_id = await _seed_import_review_job(sec_db)
    response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=ready")
    assert response.status_code == 200
    assert "data-import-review-selectable" in response.text
    assert "Select all ready" in response.text
    assert "this import" in response.text
    assert 'data-testid="import-review-cancel"' in response.text
    assert 'data-testid="import-review-pagination"' in response.text
    assert 'data-import-review-selected-files="2"' in response.text
    assert "data-import-review-attention-files" in response.text


async def test_series_candidate_rematch_keeps_polling_without_a_safety_file(
    authenticated_client, sec_db
):
    from pullbox.models.import_job import ImportedSeries

    job_id = await _seed_import_review_job(sec_db)
    async with sec_db() as session:
        series = await session.get(ImportedSeries, 1)
        series.diagnostics = {**series.diagnostics, "rematch_pending": True}
        await session.commit()
    response = await authenticated_client.get(f"/import/{job_id}/review-rematch-status")
    assert "every 2s" in response.text
    assert "HX-Trigger" not in response.headers


async def test_existing_conflict_link_uses_immediate_choices(authenticated_client, sec_db):
    job_id = await _seed_import_review_job(sec_db)
    response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=conflicts")
    assert 'data-testid="import-review-keep-copy"' in response.text
    assert "Save conflict choices" not in response.text
    assert "pendingResolutions" not in response.text


async def test_duplicate_files_remain_individually_selectable(authenticated_client, sec_db):
    job_id = await _seed_import_review_job(sec_db)
    response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=duplicate")
    assert 'data-testid="import-review-file-select"' in response.text
    assert "toggleReviewFileSelection(" in response.text


async def test_legacy_safety_reason_gets_the_correct_lane(authenticated_client, sec_db):
    from pullbox.models.import_job import ImportedFile, ImportedFileStatus

    job_id = await _seed_import_review_job(sec_db)
    async with sec_db() as session:
        file = await session.get(ImportedFile, 1)
        file.status = ImportedFileStatus.SAFETY_BLOCKED
        file.diagnostics = {"safety_block": {"reason": "Archive decompressed size exceeds limit"}}
        await session.commit()
    response = await authenticated_client.get(
        f"/import/{job_id}/review-partial?status=decide&reason=decompression_size_limit"
    )
    assert "Review Series 1" in response.text
    assert 'data-testid="import-review-allow-safety-file"' in response.text
    assert '<input type="hidden" name="reason" value="decompression_size_limit">' in response.text


async def test_legacy_conflicts_do_not_suggest_keeping_different_comics(
    authenticated_client, sec_db
):
    from sqlalchemy import select

    from pullbox.models.import_job import ImportedFile, ImportedFileStatus

    job_id = await _seed_import_review_job(sec_db)
    async with sec_db() as session:
        file = await session.scalar(
            select(ImportedFile)
            .where(
                ImportedFile.import_series_id == 7,
                ImportedFile.status == ImportedFileStatus.CONFLICT,
            )
            .order_by(ImportedFile.id)
        )
        file.parsed_series = "A completely different comic"
        await session.commit()
    response = await authenticated_client.get(
        f"/import/{job_id}/review-partial?status=decide&reason=same_comic_review"
    )
    assert 'data-import-review-series-row="7"' in response.text
    assert "Do these files belong to the same comic?" in response.text
    assert "Suggested copy" not in response.text

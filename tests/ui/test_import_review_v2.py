"""Prototype v2 contracts without changing saved import decisions."""

import re
from pathlib import Path

import pytest

from tests.ui.test_import_collection_shell_ui_routes import _seed_import_review_job

pytest_plugins = ["conftest_security"]


@pytest.mark.parametrize("file_count", [0, 1])
async def test_hero_handles_no_decisions_and_one_open_file(
    authenticated_client, sec_db, file_count
):
    from pullbox.models.import_job import (
        ImportedFile,
        ImportedFileStatus,
        ImportedSeries,
        ImportJob,
        ImportJobStatus,
        ImportSeriesStatus,
        ImportSourceType,
    )

    async with sec_db() as session:
        job = ImportJob(
            source_path="/fixture",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
        )
        session.add(job)
        if file_count:
            series = ImportedSeries(
                import_job=job, raw_series_name="One file", status=ImportSeriesStatus.NO_MATCH
            )
            session.add(
                ImportedFile(
                    import_job=job,
                    import_series=series,
                    file_path="/fixture/one.cbz",
                    file_name="one.cbz",
                    file_format="cbz",
                    status=ImportedFileStatus.NO_MATCH,
                )
            )
        await session.commit()
        job_id = job.id
    response = await authenticated_client.get(f"/import/{job_id}/review-partial")
    assert response.status_code == 200
    text = " ".join(re.sub(r"<[^>]*>", " ", response.text).split())
    assert f"Decisions 0 of {file_count} made" in text
    if file_count:
        assert "1 file need" in text
        assert "Still open 1 file in 1 series" in text
    else:
        assert "Still open 0 files in 0 series" in text
        assert 'aria-valuetext="0 of 0 file decisions made"' in response.text
        assert 'style="width:100%' in response.text


async def test_hero_decision_counts_are_job_wide_and_follow_saved_actions(
    authenticated_client, sec_db
):
    from tests.ui.test_import_safety_bulk_ui import _csrf_header_for

    job_id = await _seed_import_review_job(sec_db)

    async def hero(lane, made, opened):
        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status={lane}&page=2"
        )
        assert response.status_code == 200
        overview = response.text.split('data-testid="import-review-overview"', 1)[1].split(
            'id="import-review-workspace"', 1
        )[0]
        text = " ".join(re.sub(r"<[^>]*>", " ", overview).split())
        assert f"Decisions {made} of 8 made" in text
        assert f"Still open {opened} files in {6 if made == 0 else 5} series" in text
        assert 'aria-label="Decisions made"' in overview
        assert 'aria-valuemax="8"' in overview
        assert f'aria-valuenow="{made}"' in overview
        assert "Ready or handled" not in overview

    await hero("decide", 0, 8)
    await hero("ready", 0, 8)
    response = await authenticated_client.put(
        f"/api/v1/import/{job_id}/conflicts/7/resolve",
        json={"chosen_file_id": 19},
        headers=_csrf_header_for(authenticated_client),
    )
    assert response.status_code == 200
    await hero("confirm", 2, 6)


async def test_hero_attention_cards_match_prototype_lane_and_selection_colors(
    authenticated_client, sec_db
):
    job_id = await _seed_import_review_job(sec_db)

    response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=confirm")
    assert response.status_code == 200

    def card(lane: str) -> str:
        match = re.search(
            rf'<button[^>]*data-testid="import-review-attention-{lane}"[^>]*>',
            response.text,
        )
        assert match is not None
        return match.group(0)

    decide = card("decide")
    confirm = card("confirm")
    fix_source = card("fix_source")

    assert 'aria-pressed="false"' in decide
    assert "border-pb-warning/40" in decide
    assert "bg-pb-warning/5" in decide

    assert 'aria-pressed="true"' in confirm
    assert "border-pb-interactive" in confirm
    assert "bg-pb-interactive-dim" in confirm
    assert "ring-1 ring-pb-interactive" in confirm

    assert 'aria-pressed="false"' in fix_source
    assert "border-pb-border bg-pb-card" in fix_source

    overview = response.text.split('data-testid="import-review-overview"', 1)[1].split(
        'id="import-review-workspace"', 1
    )[0]
    assert "Unresolved files stay in Follow-up." not in overview
    assert 'data-testid="import-review-attention-kicker-decide"' in overview
    assert 'data-testid="import-review-attention-kicker-confirm"' in overview
    assert 'data-testid="import-review-attention-kicker-fix_source"' in overview
    attention_units = (
        'data-import-review-attention-units class="mt-1 block text-xs text-pb-text-sec"'
    )
    assert overview.count(attention_units) == 3

    template = Path("src/pullbox/ui/templates/partials/import_review_overview.html").read_text()
    assert 'data-testid="import-review-attention-kicker-{{ lane }}"' in template
    assert "'text-pb-error' if lane == 'blocked'" in template


async def test_review_routes_missing_references_to_an_actionable_lane(authenticated_client, sec_db):
    from sqlalchemy import select

    from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries

    job_id = await _seed_import_review_job(sec_db)
    async with sec_db() as session:
        series = await session.scalar(
            select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
        )
        session.add(
            ImportedFile(
                import_job_id=job_id,
                import_series_id=series.id,
                file_name="Old filename.cbz",
                file_path="/fixture/Old filename.cbz",
                file_format="cbz",
                status=ImportedFileStatus.SAFETY_BLOCKED,
                diagnostics={
                    "safety_block": {"code": "source_missing", "category": "source_missing"}
                },
            )
        )
        await session.commit()
    response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=info")
    assert response.status_code == 200
    assert 'data-testid="import-review-missing-references"' not in response.text
    assert "tracked separately" not in response.text
    assert "Missing references" in response.text
    assert "Old filename.cbz" in response.text
    assert "Resolve reference" in response.text
    assert "Skip reference" in response.text


async def test_v2_header_rail_and_problem_table(authenticated_client, sec_db):
    job_id = await _seed_import_review_job(sec_db)
    response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=decide")
    assert response.status_code == 200
    html = response.text
    assert 'data-testid="import-review-overview"' in html
    assert re.search(r'data-testid="import-review-progress"[^>]*role="progressbar"', html)
    workspace = html.split('id="import-review-workspace"', 1)[1]
    assert re.search(r'id="import-review-lanes"[^>]*role="tablist"', workspace)
    lane_rail = workspace.split('id="import-review-lanes"', 1)[1].split("</nav>", 1)[0]
    lane_count_spans = re.findall(
        r'<span class="([^"]*)">([^<]*)</span>',
        lane_rail,
    )
    assert lane_count_spans
    for classes, count_text in lane_count_spans:
        assert classes == "font-mono text-xs text-current"
        assert re.fullmatch(r"\(\d+\)", count_text.strip())
    assert "border-l border-pb-border ml-2 pl-4" not in workspace
    assert 'data-testid="import-review-lane-description"' in workspace
    assert 'data-testid="import-review-reason-all"' in workspace
    assert "Why?" not in workspace
    assert _headers(workspace)[:5] == [
        "Series",
        "Files",
        "What needs attention",
        "Action",
        "Details",
    ]
    assert "Select all ready" not in workspace
    assert 'data-testid="import-review-more-actions"' not in html
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
        "Series match",
        "Confidence",
        "Actions",
    ]
    assert "data-import-review-selectable" in html
    assert 'data-testid="import-review-file-select"' in html
    gate = html.split('data-testid="import-review-gate"', 1)[1]
    assert "Imports now" in gate
    assert "Stays behind" in gate
    assert "Follow-up" in gate


@pytest.mark.parametrize("series_id", ["21", "999"])
async def test_metron_folder_review_is_selectable_without_comicvine_controls(
    authenticated_client, sec_db, tmp_path, series_id
):
    from pullbox.schemas.import_job import ImportJobCreate
    from tests.integration.test_metron_folder_import import _archive, _service, _setup

    async with sec_db() as session:
        root, _, _ = await _setup(session, tmp_path)
        source = tmp_path / "source"
        _archive(source / "Unsorted" / "unidentified.cbz", series_id=series_id)
        service = _service()
        job = await service.create_job(
            session,
            ImportJobCreate(
                source_path=str(source), source_type="filesystem", target_library_root_id=root.id
            ),
        )
        await service.start_scan(session, job.id)
        await session.commit()
        job_id = job.id
    lane = "ready" if series_id == "21" else "decide"
    response = await authenticated_client.get(f"/import/{job_id}/review-partial?status={lane}")
    assert response.status_code == 200
    workspace = response.text.split('id="import-review-workspace"', 1)[1]
    assert "Search ComicVine" not in workspace
    assert "Change ComicVine match" not in workspace
    assert 'data-testid="import-review-cv-id-link"' not in workspace
    if series_id == "21":
        assert "Metron identity" in workspace
        assert "data-import-review-duplicate-selectable" in workspace
        assert 'data-testid="import-review-file-select"' in workspace
        assert "1 ready" in workspace
        assert "Harbor Lights" in workspace
    else:
        assert "Skip series" in workspace
        assert "scan this folder again" in workspace


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
            assert 'data-testid="import-review-skipped-series"' not in skipped.text
            assert "Missing references" in skipped.text
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

    from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
    from tests.ui.test_import_safety_bulk_ui import _csrf_header_for

    job_id = await _seed_import_review_job(sec_db)
    async with sec_db() as session:
        # This is issue review within an identified series, not a series-match decision.
        (await session.get(ImportedSeries, 7)).cv_id = 700
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


async def test_dangerous_files_use_one_acknowledgement_without_suppressing_safe_files(
    authenticated_client, sec_db, tmp_path
):
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
    from tests.ui.test_import_safety_bulk_ui import _csrf_header_for

    safe_path = tmp_path / "safe.cbz"
    first_dangerous_path = tmp_path / "unsafe-one.cbz"
    second_dangerous_path = tmp_path / "unsafe-two.cbz"
    safe_path.write_bytes(b"safe source")
    first_dangerous_path.write_bytes(b"unsafe source one")
    second_dangerous_path.write_bytes(b"unsafe source two")

    async with sec_db() as session:
        job = ImportJob(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
            total_files_found=3,
            total_files_matched=1,
        )
        series = ImportedSeries(
            import_job=job,
            status=ImportSeriesStatus.MATCHED,
            raw_series_name="Mixed safety evidence",
            cv_id=1234,
            files_total=3,
            files_matched=1,
            selected_for_import=True,
            diagnostics={"safety_blocked_files": 2},
        )
        safe = ImportedFile(
            import_job=job,
            import_series=series,
            file_path=str(safe_path),
            file_name=safe_path.name,
            file_format="cbz",
            status=ImportedFileStatus.MATCHED,
            include_in_import=True,
            matched_issue_cv_id=111,
        )
        dangerous = []
        for path in (first_dangerous_path, second_dangerous_path):
            dangerous.append(
                ImportedFile(
                    import_job=job,
                    import_series=series,
                    file_path=str(path),
                    file_name=path.name,
                    file_format="cbz",
                    status=ImportedFileStatus.SAFETY_BLOCKED,
                    include_in_import=True,
                    diagnostics={
                        "safety_block": {
                            "category": "dangerous_path_or_payload",
                            "code": "path_traversal",
                            "reason": "The archive contains a dangerous path.",
                            "overrideable": False,
                        }
                    },
                )
            )
        session.add_all([job, series, safe, *dangerous])
        await session.commit()
        job_id = job.id
        series_id = series.id
        safe_id = safe.id
        dangerous_ids = [item.id for item in dangerous]

    response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=blocked")
    assert response.status_code == 200
    assert response.text.count(">Acknowledge</button>") == 1
    assert "Unsafe archive content cannot be allowed." in response.text
    assert "Unsafe archive content cannot be allowed once." not in response.text
    assert "Technical detail" not in response.text
    assert "Inspection details" not in response.text
    assert "ComicVine match:" not in response.text
    assert 'data-testid="import-review-skip-safety-file"' not in response.text
    assert 'data-testid="import-review-more-actions"' not in response.text
    assert 'data-testid="import-review-expand"' in response.text

    response = await authenticated_client.post(
        f"/import/{job_id}/series/{series_id}/dangerous/acknowledge?status=blocked",
        headers=_csrf_header_for(authenticated_client),
    )
    assert response.status_code == 200, response.text

    async with sec_db() as session:
        refreshed_series = await session.get(ImportedSeries, series_id)
        refreshed_safe = await session.get(ImportedFile, safe_id)
        refreshed_dangerous = list(
            (
                await session.scalars(
                    select(ImportedFile)
                    .where(ImportedFile.id.in_(dangerous_ids))
                    .order_by(ImportedFile.id)
                )
            ).all()
        )
        assert refreshed_series is not None
        assert refreshed_series.selected_for_import is True
        assert refreshed_series.files_matched == 1
        assert (refreshed_series.diagnostics or {}).get("safety_blocked_files") is None
        assert refreshed_safe is not None
        assert refreshed_safe.status is ImportedFileStatus.MATCHED
        assert refreshed_safe.include_in_import is True
        for item in refreshed_dangerous:
            assert item.status is ImportedFileStatus.SKIPPED
            assert item.include_in_import is False
            assert item.diagnostics["resolution"] == "dangerous_content_acknowledged"
            acknowledgement = item.diagnostics["dangerous_content_acknowledgement"]
            assert acknowledgement["actor_id"] > 0
            assert acknowledgement["acknowledged_at"]

    assert safe_path.read_bytes() == b"safe source"
    assert first_dangerous_path.read_bytes() == b"unsafe source one"
    assert second_dangerous_path.read_bytes() == b"unsafe source two"

    stale = await authenticated_client.post(
        f"/import/{job_id}/series/{series_id}/dangerous/acknowledge?status=blocked",
        headers=_csrf_header_for(authenticated_client),
    )
    assert stale.status_code == 409


async def test_recoverable_safety_failures_have_honest_lanes_copy_and_actions(
    authenticated_client, sec_db, tmp_path
):
    from pullbox.models.import_job import (
        ImportedFile,
        ImportedFileStatus,
        ImportedSeries,
        ImportJob,
        ImportJobStatus,
        ImportSeriesStatus,
        ImportSourceType,
    )

    async with sec_db() as session:
        job = ImportJob(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
            total_files_found=2,
        )
        unsupported_series = ImportedSeries(
            import_job=job,
            status=ImportSeriesStatus.NO_MATCH,
            raw_series_name="Unsupported source",
            files_total=1,
            diagnostics={"safety_blocked_files": 1},
        )
        unknown_series = ImportedSeries(
            import_job=job,
            status=ImportSeriesStatus.NO_MATCH,
            raw_series_name="Unknown source",
            files_total=1,
            diagnostics={"safety_blocked_files": 1},
        )
        session.add_all(
            [
                job,
                unsupported_series,
                unknown_series,
                ImportedFile(
                    import_job=job,
                    import_series=unsupported_series,
                    file_path=str(tmp_path / "Unsupported Comic.rar"),
                    file_name="Unsupported Comic.rar",
                    file_format="rar",
                    status=ImportedFileStatus.SAFETY_BLOCKED,
                    diagnostics={
                        "safety_block": {
                            "category": "unsupported_file_type",
                            "code": "unsupported_file_type",
                            "reason": "The file type is not supported for import.",
                            "overrideable": False,
                        }
                    },
                ),
                ImportedFile(
                    import_job=job,
                    import_series=unknown_series,
                    file_path=str(tmp_path / "Unknown Comic.cbz"),
                    file_name="Unknown Comic.cbz",
                    file_format="cbz",
                    status=ImportedFileStatus.SAFETY_BLOCKED,
                    diagnostics={
                        "safety_block": {
                            "category": "unknown",
                            "code": "unknown_safety_failure",
                            "reason": "Pullbox could not establish file safety.",
                            "overrideable": False,
                        }
                    },
                ),
            ]
        )
        await session.commit()
        job_id = job.id

    blocked = await authenticated_client.get(
        f"/import/{job_id}/review-partial?status=blocked&reason=unsupported_file_type"
    )
    assert blocked.status_code == 200
    assert "Unsupported source" in blocked.text
    assert "Unknown source" not in blocked.text
    assert "The .rar file type is not supported." in blocked.text
    assert ".cbz, .cbr, .cb7, .cbt, .pdf, and .epub" in blocked.text
    assert "Convert or replace this file with a supported type, then recheck it." in blocked.text
    assert blocked.text.count('data-testid="import-review-recheck-source"') == 1
    assert blocked.text.count('data-testid="import-review-skip-safety-file"') == 1
    assert "Inspection details" not in blocked.text
    assert ">View details</button>" not in blocked.text
    assert 'data-testid="import-review-primary-action"' not in blocked.text
    assert 'data-testid="import-review-more-actions"' not in blocked.text

    fix_source = await authenticated_client.get(
        f"/import/{job_id}/review-partial?status=fix_source&reason=unknown"
    )
    assert fix_source.status_code == 200
    assert "Unknown source" in fix_source.text
    assert "Unsupported source" not in fix_source.text
    assert "Pullbox could not establish that this file is safe to import." in fix_source.text
    assert 'data-testid="import-review-recheck-source"' in fix_source.text
    assert 'data-testid="import-review-skip-safety-file"' in fix_source.text

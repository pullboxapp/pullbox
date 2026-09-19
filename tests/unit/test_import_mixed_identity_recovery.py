"""Mixed folders use per-file identities rather than inherited folder labels."""

from dataclasses import replace

import pytest
from sqlalchemy import select

from pullbox.models.import_job import ImportedFileStatus, ImportedSeries, ImportSourceType
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.services.import_deferred_recovery_execution import prepare_deferred_recovery
from tests.unit.test_import_deferred_recovery import add_file
from tests.unit.test_import_reference_recovery import reference_case


def embedded_identity(*, number="104", issue_id=7001):
    return {
        "source_issue_type": "issue",
        "metadata_signals": {
            "series_name": "comicinfo",
            "issue_number": "comicinfo",
            "comicvine_issue_id": "comicinfo",
            "comicvine_series_id": "mylar3",
        },
        "comicvine_series_id": 100,
        "source_metadata": {
            "comicinfo": {
                "series": "Thunderbolts",
                "number": number,
                "year": 2021,
                "web": f"https://comicvine.gamespot.com/issue/4000-{issue_id}/",
            }
        },
    }


@pytest.mark.parametrize("source_type", list(ImportSourceType))
@pytest.mark.parametrize("existing_wrong_issue", [False, True])
async def test_embedded_identity_repairs_stale_folder_label(
    db_session, tmp_path, source_type, existing_wrong_issue
):
    job, file, library, wrong, metadata, path = await reference_case(
        db_session, tmp_path, source_type
    )
    item = await db_session.get(ImportedSeries, file.import_series_id)
    item.files_no_match = 0
    file.parsed_series = item.raw_series_name
    file.comicvine_issue_id = 7001
    file.diagnostics = embedded_identity()
    if existing_wrong_issue:
        wrong.comicvine_id = file.matched_issue_cv_id = 7001
    original_id = library.id
    original_bytes = path.read_bytes()
    await db_session.commit()

    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)

    target = await db_session.scalar(select(Issue).where(Issue.comicvine_id == 7001))
    assert target is not None, "Embedded titles must not disappear behind saved folder titles"
    target_series = await db_session.get(Series, target.series_id)
    assert target_series.title == "Thunderbolts"
    assert library.issue_id == file.matched_issue_id == target.id
    assert library.id == file.library_file_id == original_id
    assert path.read_bytes() == original_bytes
    assert library.file_path == str(path)
    assert target_series.path is None
    if existing_wrong_issue:
        assert target.id == wrong.id, "Preserve the issue row and its reading history"


@pytest.mark.parametrize("source_type", list(ImportSourceType))
@pytest.mark.parametrize("evidence", ["embedded", "embedded_id", "reading_order"])
async def test_deferred_mixed_file_uses_independent_title(
    db_session, tmp_path, source_type, evidence
):
    job, file, library, _wrong, metadata, _ = await reference_case(
        db_session, tmp_path, source_type
    )
    await db_session.delete(library)
    file.library_file_id = file.matched_issue_id = file.matched_issue_cv_id = None
    file.comicvine_issue_id = None
    file.status = ImportedFileStatus.NO_MATCH
    file.parsed_series = "Fritzi Ritz"
    file.file_name = "042 - Thunderbolts 104 (2021) (converted).cbz"
    file.diagnostics = {"metadata_signals": {"issue_number": "release_title"}}
    if evidence.startswith("embedded"):
        file.file_name = "Thunderbolts 104 (2021) (DR & Quinch-Empire).cbz"
        file.diagnostics = embedded_identity()
        if evidence == "embedded_id":
            file.comicvine_issue_id = 7001
        else:
            file.diagnostics["metadata_signals"].pop("comicvine_issue_id")
            file.diagnostics["source_metadata"]["comicinfo"].pop("web")
    await db_session.commit()

    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)

    assert file.status is ImportedFileStatus.CONFIRMED
    assert file.matched_issue_cv_id == 7001
    target = await db_session.get(ImportedSeries, file.import_series_id)
    assert target.cv_title == "Thunderbolts"
    metadata.search_catalog_series.assert_awaited_once_with("Thunderbolts", limit=1000)


async def test_catalog_years_are_checked_for_each_file_not_just_the_group(db_session, tmp_path):
    job, file, library, _, metadata, _ = await reference_case(db_session, tmp_path)
    await db_session.delete(library)
    file.library_file_id = file.matched_issue_id = file.matched_issue_cv_id = None
    file.status = ImportedFileStatus.NO_MATCH
    item = await db_session.get(ImportedSeries, file.import_series_id)
    other = await add_file(
        db_session,
        job,
        item,
        file_name="Thunderbolts 104 (1990).cbz",
        file_path="/comics/mixed/Thunderbolts 104 (1990).cbz",
        comicvine_issue_id=None,
        parsed_series="Fritzi Ritz",
        parsed_year=1990,
        diagnostics={"metadata_signals": {"issue_number": "release_title"}},
    )
    await db_session.commit()

    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)

    assert file.status is ImportedFileStatus.CONFIRMED
    assert other.status is ImportedFileStatus.NO_MATCH
    assert "year" in other.diagnostics["mixed_folder_recovery"]["reason"].lower()


@pytest.mark.parametrize("conflict", ["number", "embedded_id", "date", "type", "title"])
async def test_embedded_recovery_retains_conflicting_evidence(db_session, tmp_path, conflict):
    job, file, library, wrong, metadata, _ = await reference_case(db_session, tmp_path)
    file.parsed_series = "Fritzi Ritz"
    file.comicvine_issue_id = 7001
    file.diagnostics = embedded_identity()
    if conflict == "number":
        file.diagnostics = embedded_identity(number="105")
    elif conflict == "embedded_id":
        file.diagnostics = embedded_identity(issue_id=7002)
    elif conflict == "date":
        metadata.get_catalog_issue_summaries_for_series.return_value = [
            replace(
                metadata.get_catalog_issue_summaries_for_series.return_value[0],
                release_date="1990-01-01",
            )
        ]
    elif conflict == "title":
        file.file_name = "Action Comics 104 (2021).cbz"
    else:
        metadata.get_catalog_issue_summaries_for_series.return_value = [
            replace(
                metadata.get_catalog_issue_summaries_for_series.return_value[0], issue_type="tpb"
            )
        ]
    await db_session.commit()

    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)

    assert library.issue_id == wrong.id
    assert await db_session.scalar(select(Series).where(Series.comicvine_id == 700)) is None


@pytest.mark.parametrize("shared", ["file", "import", "manual"])
async def test_wrong_catalog_issue_is_not_reparented_with_other_ownership(
    db_session, tmp_path, shared
):
    from tests.unit.test_import_deferred_recovery import register

    job, file, library, wrong, metadata, _ = await reference_case(db_session, tmp_path)
    item = await db_session.get(ImportedSeries, file.import_series_id)
    file.parsed_series = "Fritzi Ritz"
    file.comicvine_issue_id = wrong.comicvine_id = file.matched_issue_cv_id = 7001
    file.diagnostics = embedded_identity()
    if shared == "manual":
        item.user_selected_cv_id = 100
    else:
        twin = await add_file(
            db_session,
            job,
            item,
            matched_issue_id=wrong.id,
            status=ImportedFileStatus.IMPORTED
            if shared == "import"
            else ImportedFileStatus.SKIPPED,
        )
        if shared == "file":
            from pullbox.models.library import LibraryRoot

            root = await db_session.get(LibraryRoot, library.library_root_id)
            await register(db_session, twin, wrong, root)
    await db_session.commit()

    await prepare_deferred_recovery(db_session, job.id, metadata_service=metadata)

    assert wrong.series_id == item.series_id
    assert await db_session.scalar(select(Series).where(Series.comicvine_id == 700)) is None


async def test_wrong_folder_cannot_offer_a_provisional_issue_for_a_foreign_comic(
    db_session, tmp_path
):
    from pullbox.services.import_reconcile_helpers import provisional_issue_number_for_file

    _, file, _, _, _, _ = await reference_case(db_session, tmp_path)
    item = await db_session.get(ImportedSeries, file.import_series_id)
    file.status = ImportedFileStatus.NO_MATCH
    file.parsed_series = "Fritzi Ritz"
    file.diagnostics = embedded_identity()

    assert provisional_issue_number_for_file(item, file, []) is None

"""Issue titles and chapter numbers must not replace embedded comic identity."""

import zipfile

import pytest
from sqlalchemy import select

from pullbox.core.collection_scanner import CollectionScanner
from pullbox.core.library_layout import ImportLayoutMode, SourceLayoutSpec
from pullbox.core.release_parser import parse_release_title
from pullbox.core.source_metadata import SourceMetadataExtractor
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportJob, ImportSourceType
from pullbox.providers.base import IssueSummary
from pullbox.services.import_scan_helpers import validate_discovered_files_safety
from pullbox.services.import_scan_materialization import materialize_discovered_scan_results
from pullbox.services.import_source_metadata import source_metadata_for_matching_series
from tests.unit.test_import_catalog_fallback import LocalCatalog, service


def comic(path, *, series, number, volume="2024", year=2025, issue_id=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    web = f"<Web>https://comicvine.gamespot.com/example/4000-{issue_id}/</Web>" if issue_id else ""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Series>{series}</Series><Number>{number}</Number>"
            f"<Volume>{volume}</Volume><Year>{year}</Year>{web}</ComicInfo>",
        )
        archive.writestr("001.jpg", b"page one")
        archive.writestr("002.jpg", b"page two")


@pytest.mark.parametrize(
    "filename,number",
    [
        ("Absolute Batman Abomination, Part Five 13.cbz", "13"),
        ("Absolute Batman Abomination, Part Five #13.cbz", "13"),
        ("Absolute Batman Abomination, Part Five 0.5.cbz", "0.5"),
        ("Absolute Batman Abomination, Part Five #1000000.cbz", "1000000"),
        ("Example Book Two.cbz", "2"),
        ("Example Part Three.cbz", "3"),
        ("Example Volume One.cbz", "1"),
        ("100 Bullets Book Two.cbz", "2"),
        ("Absolute Batman Abomination, Part Five 13 (2025) (Digital).cbz", "13"),
        ("Absolute Batman Abomination, Part Five 7.cbz", "7"),
    ],
)
def test_explicit_issue_number_beats_story_chapter(filename, number):
    assert parse_release_title(filename).issue_number_text == number


@pytest.mark.parametrize("preset", [None, "series_folders"])
async def test_issue_titles_group_by_comicinfo_not_each_filename(tmp_path, preset):
    folder = tmp_path / "Absolute Power 2024"
    for number, title in [(1, "Chapter One Powerless"), (4, "Chapter Four Showdown")]:
        comic(
            folder / f"Absolute Power {title} {number:02}.cbz",
            series="Absolute Power",
            number=number,
        )
    layout = SourceLayoutSpec(mode=ImportLayoutMode.PRESET, preset=preset) if preset else None
    groups = [group async for group in CollectionScanner(source_layout=layout).scan(tmp_path)]

    assert [(group.raw_series_name, group.raw_year, group.file_count) for group in groups] == [
        ("Absolute Power", 2024, 2)
    ]
    assert {file.parsed_issue_number for file in groups[0].files} == {1, 4}


async def test_embedded_mixed_series_are_not_swallowed_by_folder_prefix(tmp_path):
    folder = tmp_path / "Batman 2016"
    comic(folder / "Batman A Good Fantasy 149.cbz", series="Batman", number=149, volume="2016")
    comic(
        folder / "Batman Beyond The Return 01.cbz", series="Batman Beyond", number=1, volume="2016"
    )
    groups = [group async for group in CollectionScanner().scan(tmp_path)]
    assert {group.raw_series_name for group in groups} == {"Batman", "Batman Beyond"}


@pytest.mark.parametrize("embedded_volume", [False, True])
async def test_series_folder_year_is_not_each_issues_publication_year(tmp_path, embedded_volume):
    for number, year in [(1, 2016), (149, 2024)]:
        comic(
            tmp_path / "Batman 2016" / f"Batman {number:03} ({year}).cbz",
            series="Batman",
            number=number,
            volume="2016" if embedded_volume else "",
            year=year,
        )
    groups = [group async for group in CollectionScanner().scan(tmp_path)]
    assert [(group.raw_series_name, group.raw_year, group.file_count) for group in groups] == [
        ("Batman", 2016, 2)
    ]


async def test_distinct_embedded_series_volumes_remain_separate(tmp_path):
    for volume in [2011, 2016]:
        comic(
            tmp_path / "Batman" / f"Batman Opening Story {volume} 01.cbz",
            series="Batman",
            number=1,
            volume=str(volume),
            year=volume,
        )
    groups = [group async for group in CollectionScanner().scan(tmp_path)]
    assert {(group.raw_series_name, group.raw_year) for group in groups} == {
        ("Batman", 2011),
        ("Batman", 2016),
    }


async def test_missing_embedded_metadata_does_not_guess_from_a_shared_prefix(tmp_path):
    folder = tmp_path / "Batman 2016"
    folder.mkdir()
    for name in ["Batman 01.cbz", "Batman Beyond 01.cbz"]:
        with zipfile.ZipFile(folder / name, "w") as archive:
            archive.writestr("001.jpg", b"page one")
            archive.writestr("002.jpg", b"page two")
    groups = [group async for group in CollectionScanner().scan(tmp_path)]
    assert {group.raw_series_name for group in groups} == {"Batman", "Batman Beyond"}


async def test_chapter_five_does_not_create_a_false_duplicate_conflict(db_session, tmp_path):
    folder = tmp_path / "Absolute Batman 2024"
    for number, story in [(5, "The Zoo"), (13, "Abomination")]:
        comic(
            folder / f"Absolute Batman {story}, Part Five {number:02}.cbz",
            series="Absolute Batman",
            number=number,
        )
    before = {path: path.read_bytes() for path in folder.iterdir()}
    groups = [group async for group in CollectionScanner().scan(tmp_path)]
    await validate_discovered_files_safety(db_session, groups)
    job = ImportJob(source_path=str(tmp_path), source_type=ImportSourceType.FILESYSTEM)
    db_session.add(job)
    await db_session.flush()
    await materialize_discovered_scan_results(db_session, job, groups)
    provider = LocalCatalog()
    provider.issues = [
        IssueSummary(str(1000 + n), n, None, "2025-01-01", None, "issue") for n in [5, 13]
    ]
    owner = service(job, provider)
    await owner._run_matching(db_session, job)
    await owner._run_file_matching(db_session, job)
    files = list((await db_session.scalars(select(ImportedFile))).all())
    assert len(files) == 2
    assert all(file.status == ImportedFileStatus.MATCHED for file in files)
    assert {file.matched_issue_cv_id for file in files} == {1005, 1013}
    assert all(file.conflict_group_id is None for file in files)
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize(
    "excluded_status", [ImportedFileStatus.SKIPPED, ImportedFileStatus.SAFETY_BLOCKED]
)
async def test_inspected_metadata_is_applied_to_all_files_without_archive_reopens(
    db_session, tmp_path, monkeypatch, excluded_status
):
    folder = tmp_path / "Absolute Batman 2024"
    # Clean filenames take the deferred path. Later metadata must still win
    # even when the filename's number happens to identify an existing issue.
    for number in range(1, 7):
        comic(
            folder / f"Absolute Batman {number:03}.cbz",
            series="Absolute Batman",
            number=number + 10,
            issue_id=1000 + number,
        )
    groups = [group async for group in CollectionScanner().scan(tmp_path)]
    await validate_discovered_files_safety(db_session, groups)
    job = ImportJob(source_path=str(tmp_path), source_type=ImportSourceType.FILESYSTEM)
    db_session.add(job)
    await db_session.flush()
    pairs = await materialize_discovered_scan_results(db_session, job, groups)
    files = list((await db_session.scalars(select(ImportedFile).order_by(ImportedFile.id))).all())
    files[-1].status = excluded_status

    def no_reopen(*args, **kwargs):
        raise AssertionError("Already inspected ComicInfo must not reopen an archive")

    monkeypatch.setattr(SourceMetadataExtractor, "_read_archive_evidence", no_reopen)
    for _discovered, series in pairs:
        await source_metadata_for_matching_series(
            db_session, series, trusted_identity_probe_limit=1
        )

    assert [file.parsed_issue_number for file in files[:-1]] == [11, 12, 13, 14, 15]
    assert [file.comicvine_issue_id for file in files[:-1]] == [1001, 1002, 1003, 1004, 1005]
    assert files[-1].parsed_issue_number == 6
    assert files[-1].status == excluded_status

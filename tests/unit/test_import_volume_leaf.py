"""Volume directories are source hints, never issue identities or target policies."""

import json
import zipfile

import pytest
from sqlalchemy import select

from pullbox.core.collection_scanner import CollectionScanner
from pullbox.core.library_layout import ImportLayoutMode, SourceLayoutSpec
from pullbox.core.source_volume_layout import assess_volume_leaf, volume_leaf_from_path
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.services.import_layout_analysis import ImportLayoutAnalyzer
from pullbox.services.import_scan_materialization import materialize_discovered_scan_results


def _comic(path, xml=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", b"one")
        archive.writestr("002.jpg", b"two")
        if xml:
            archive.writestr("ComicInfo.xml", xml)
    return path


@pytest.mark.parametrize("publisher", [None, "DC Comics"])
@pytest.mark.parametrize("marker,year", [("v2017", 2017), ("v2", None)])
async def test_volume_leaf_layout_and_scan_agree(tmp_path, publisher, marker, year):
    folder = (tmp_path / publisher if publisher else tmp_path) / "Mister Miracle" / marker
    path = _comic(folder / "Mister Miracle 001.cbz")
    before = path.read_bytes()
    analysis = await ImportLayoutAnalyzer().analyze(tmp_path)
    assert analysis.files_fitting == 1
    example = analysis.clusters[0].examples[0]
    assert (example.series, example.publisher, example.year) == ("Mister Miracle", publisher, year)
    assert analysis.can_apply_future_policy is False
    assert analysis.archive_probes == 0
    results = [series async for series in CollectionScanner().scan(tmp_path)]
    assert len(results) == 1
    assert (results[0].raw_series_name, results[0].raw_publisher, results[0].raw_year) == (
        "Mister Miracle",
        publisher,
        year,
    )
    assert results[0].source_folder == str(folder)
    assert results[0].files[0].parsed_issue_number == 1
    assert results[0].files[0].metadata_diagnostics["volume_leaf"]["marker"] == marker
    assert path.read_bytes() == before
    assert await ImportLayoutAnalyzer().analyze(tmp_path) == analysis


@pytest.mark.parametrize("kind", ["series.json", "cvinfo", "comicinfo"])
async def test_volume_leaf_corroborates_existing_local_metadata(tmp_path, kind):
    folder = tmp_path / "Mister Miracle" / "v2017"
    xml = (
        "<ComicInfo><Series>Mister Miracle</Series><Number>1</Number>"
        "<Web>https://comicvine.gamespot.com/mister-miracle/4050-103699/</Web></ComicInfo>"
        if kind == "comicinfo"
        else None
    )
    _comic(folder / "001.cbz", xml)
    if kind == "series.json":
        (folder / kind).write_text(
            json.dumps({"metadata": {"name": "Mister Miracle", "comicid": 103699}})
        )
    elif kind == "cvinfo":
        (folder / kind).write_text(
            "https://comicvine.gamespot.com/4050-103699/\nname: Mister Miracle"
        )
    results = [series async for series in CollectionScanner().scan(tmp_path)]
    assert len(results) == 1
    assert results[0].raw_series_name == "Mister Miracle"
    assert results[0].raw_year == 2017
    assert results[0].comicinfo_cv_id == 103699
    assert results[0].files[0].metadata_diagnostics["volume_leaf"]["review_required"] is False


@pytest.mark.parametrize("name", ["unknown.cbz", "Other Series 001.cbz"])
@pytest.mark.parametrize("mode", ["managed_copy", "in_place"])
async def test_uncertain_volume_folder_enters_review_without_touching_sources(
    db_session, tmp_path, name, mode
):
    path = _comic(tmp_path / "Mister Miracle" / "v2017" / name)
    before = path.read_bytes()
    analysis = await ImportLayoutAnalyzer().analyze(tmp_path)
    assert analysis.files_ambiguous == 1
    results = [series async for series in CollectionScanner().scan(tmp_path)]
    job = ImportJob(
        source_path=str(tmp_path), source_type=ImportSourceType.FILESYSTEM, file_handling_mode=mode
    )
    db_session.add(job)
    await db_session.flush()
    await materialize_discovered_scan_results(db_session, job, results)
    file = await db_session.scalar(select(ImportedFile).where(ImportedFile.import_job_id == job.id))
    assert file.status == ImportedFileStatus.NO_MATCH
    assert file.diagnostics["reason"] == "volume_leaf_identity_unconfirmed"
    assert path.read_bytes() == before


async def test_volume_leaf_does_not_undo_proven_mixed_folder_recovery(tmp_path):
    folder = tmp_path / "Crossed" / "v2012"
    _comic(
        folder / "Absolute Batman 001.cbz",
        "<ComicInfo><Series>Absolute Batman</Series><Number>1</Number><Volume>2024</Volume><Web>https://comicvine.gamespot.com/absolute-batman/4050-160294/</Web></ComicInfo>",
    )
    results = [series async for series in CollectionScanner().scan(tmp_path)]
    assert len(results) == 1
    assert results[0].comicinfo_cv_id == 160294
    assert results[0].raw_series_name == "Absolute Batman"
    assert results[0].raw_year == 2024
    assert (
        not results[0].files[0].metadata_diagnostics.get("source_layout", {}).get("review_required")
    )


async def test_real_v2_title_is_not_replaced_with_publisher(tmp_path):
    _comic(
        tmp_path / "Publisher" / "V2" / "V2 001.cbz",
        "<ComicInfo><Series>V2</Series><Number>1</Number></ComicInfo>",
    )
    results = [series async for series in CollectionScanner().scan(tmp_path)]
    assert results[0].raw_series_name == "V2"
    analysis = await ImportLayoutAnalyzer().analyze(tmp_path)
    assert analysis.clusters[0].examples[0].series == "V2"


async def test_volume_folder_hint_does_not_change_publication_year_or_merge_releases(tmp_path):
    for marker in ("v2017", "v2020"):
        _comic(tmp_path / "Mister Miracle" / marker / "Mister Miracle 001 (2021).cbz")
    results = [series async for series in CollectionScanner().scan(tmp_path)]
    assert {series.raw_year for series in results} == {2017, 2020}
    assert all(series.files[0].parsed_year == 2021 for series in results)
    assert len({series.source_folder for series in results}) == 2


async def test_custom_layout_remains_authoritative(tmp_path):
    _comic(tmp_path / "Mister Miracle" / "v2017" / "Mister Miracle 001.cbz")
    spec = SourceLayoutSpec(
        mode=ImportLayoutMode.CUSTOM,
        series_path_template="{Publisher}/{Series}",
        fallback_to_auto=False,
    )
    analysis = await ImportLayoutAnalyzer().analyze(tmp_path, spec=spec)
    assert analysis.clusters[0].examples[0].evidence == ["selected_layout_match"]
    results = [series async for series in CollectionScanner(source_layout=spec).scan(tmp_path)]
    assert "volume_leaf" not in results[0].files[0].metadata_diagnostics


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_volume_leaf_keeps_unicode_and_path_separator_evidence(separator):
    path = separator.join(["\u00c9ditions", "\u00c9toile", "v2017", "\u00c9toile 13A.cbz"])
    leaf = volume_leaf_from_path(path)
    assert leaf.series == "\u00c9toile"
    assert leaf.publisher == "\u00c9ditions"
    assert assess_volume_leaf(leaf, "\u00c9toile 13A.cbz") == (False, True, False)


@pytest.mark.parametrize("title", ["\u00c9toile", "\u65e5\u672c\u8a9e"])
async def test_scanner_preserves_unicode_volume_series(tmp_path, title):
    _comic(tmp_path / title / "v2017" / f"{title} 001 (2018).cbz")
    results = [series async for series in CollectionScanner().scan(tmp_path)]
    assert results[0].raw_series_name == title
    assert results[0].raw_year == 2017
    assert results[0].files[0].parsed_year == 2018


@pytest.mark.parametrize(
    "evidence", ["sidecar_title", "comicinfo_title", "conflicting_ids", "year"]
)
async def test_volume_leaf_conflicting_evidence_remains_reviewable(tmp_path, evidence):
    folder = tmp_path / "Mister Miracle" / "v2017"
    xml = (
        "<ComicInfo><Series>Other Series</Series><Number>1</Number></ComicInfo>"
        if evidence == "comicinfo_title"
        else None
    )
    if evidence == "year":
        xml = (
            "<ComicInfo><Series>Mister Miracle</Series><Number>1</Number>"
            "<Volume>2020</Volume></ComicInfo>"
        )
    _comic(folder / "Mister Miracle 001.cbz", xml)
    if evidence == "sidecar_title":
        (folder / "series.json").write_text(json.dumps({"name": "Other Series", "comicid": 123}))
    if evidence == "conflicting_ids":
        (folder / "series.json").write_text(json.dumps({"name": "Mister Miracle", "comicid": 123}))
        (folder / "cvinfo").write_text("https://comicvine.gamespot.com/4050-456/")
    results = [series async for series in CollectionScanner().scan(tmp_path)]
    assert results[0].files[0].metadata_diagnostics["volume_leaf"]["review_required"] is True


async def test_volume_leaf_does_not_borrow_parent_sidecar_across_releases(tmp_path):
    for marker in ("v2", "v3"):
        _comic(tmp_path / "Mister Miracle" / marker / "Mister Miracle 001.cbz")
    (tmp_path / "Mister Miracle" / "series.json").write_text(
        json.dumps({"name": "Mister Miracle", "comicid": 103699})
    )
    results = [series async for series in CollectionScanner().scan(tmp_path)]
    assert len(results) == 2
    assert len({series.source_folder for series in results}) == 2
    assert all(series.comicinfo_cv_id is None for series in results)
    assert {
        series.files[0].metadata_diagnostics["volume_leaf"]["volume_hint"] for series in results
    } == {2, 3}


async def test_volume_leaf_does_not_inspect_above_selected_source_root(tmp_path):
    folder = tmp_path / "Mister Miracle" / "v2017"
    _comic(folder / "001.cbz")
    results = [series async for series in CollectionScanner().scan(folder)]
    assert "volume_leaf" not in results[0].files[0].metadata_diagnostics


async def test_volume_leaf_mixed_bucket_preserves_good_file(db_session, tmp_path):
    folder = tmp_path / "Mister Miracle" / "v2017"
    _comic(folder / "Mister Miracle 001.cbz")
    _comic(folder / "Other Series 002.cbz")
    results = [series async for series in CollectionScanner().scan(tmp_path)]
    evidence = {item.raw_series_name: item.diagnostics["volume_leaf"] for item in results}
    assert evidence["Mister Miracle"]["confirmed"] is True
    assert evidence["Other Series"]["confirmed"] is False
    assert evidence["Other Series"]["review_required"] is True
    job = ImportJob(source_path=str(tmp_path), source_type=ImportSourceType.FILESYSTEM)
    db_session.add(job)
    await db_session.flush()
    await materialize_discovered_scan_results(db_session, job, results)
    files = list(
        await db_session.scalars(select(ImportedFile).where(ImportedFile.import_job_id == job.id))
    )
    assert {file.file_name: file.status for file in files} == {
        "Mister Miracle 001.cbz": ImportedFileStatus.PENDING,
        "Other Series 002.cbz": ImportedFileStatus.NO_MATCH,
    }


async def test_ordinal_only_releases_require_series_confirmation(db_session, tmp_path):
    for marker in ("v2", "v3"):
        _comic(tmp_path / "Mister Miracle" / marker / "Mister Miracle 001.cbz")
    results = [series async for series in CollectionScanner().scan(tmp_path)]
    job = ImportJob(source_path=str(tmp_path), source_type=ImportSourceType.FILESYSTEM)
    db_session.add(job)
    await db_session.flush()
    await materialize_discovered_scan_results(db_session, job, results)
    series = list(
        await db_session.scalars(
            select(ImportedSeries).where(ImportedSeries.import_job_id == job.id)
        )
    )
    assert len(series) == 2
    assert all(item.status == ImportSeriesStatus.NO_MATCH for item in series)
    assert all(item.diagnostics["reason"] == "volume_leaf_release_unconfirmed" for item in series)
    expected = {item.id: dict(item.diagnostics) for item in series}
    await db_session.flush()
    for item in series:
        await db_session.refresh(item)
        assert item.diagnostics == expected[item.id]


async def test_ordinal_volume_preserves_explicit_metadata_series_year(tmp_path):
    _comic(
        tmp_path / "Mister Miracle" / "v2" / "Mister Miracle 001.cbz",
        "<ComicInfo><Series>Mister Miracle</Series><Number>1</Number>"
        "<Volume>2017</Volume><Year>2018</Year></ComicInfo>",
    )
    results = [series async for series in CollectionScanner().scan(tmp_path)]
    assert results[0].raw_year == 2017
    assert not results[0].diagnostics["volume_leaf"]["series_confirmation_required"]


@pytest.mark.parametrize("mode", ["managed_copy", "in_place"])
async def test_confirmed_volume_scan_keeps_source_and_target_policy(db_session, tmp_path, mode):
    path = _comic(tmp_path / "Mister Miracle" / "v2017" / "Mister Miracle 13A.cbz")
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    results = [series async for series in CollectionScanner().scan(tmp_path)]
    policy = {"series_folder_template": "{Series} ({Year})", "post_processing_method": "copy"}
    job = ImportJob(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        file_handling_mode=mode,
        ingest_policy_snapshot=dict(policy),
    )
    db_session.add(job)
    await db_session.flush()
    await materialize_discovered_scan_results(db_session, job, results)
    await db_session.refresh(job)
    file = await db_session.scalar(select(ImportedFile).where(ImportedFile.import_job_id == job.id))
    assert file.status == ImportedFileStatus.PENDING
    assert file.file_path == str(path)
    assert file.issue_number_raw == "13A"
    assert job.ingest_policy_snapshot == policy
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original

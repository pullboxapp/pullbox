"""Contradictory file evidence must survive trusted parent Mylar identity."""

from unittest.mock import Mock

import pytest

from pullbox.core.source_metadata import SourceMetadata
from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportSeriesStatus
from pullbox.models.issue import IssueType
from pullbox.services.import_file_match_targets import FileMatchTargetIndex
from pullbox.services.import_file_matching import _evaluate_file_match_candidate
from pullbox.services.import_source_metadata import (
    build_import_metadata_conflict,
    corroborated_import_title_conflict,
    source_metadata_for_import_file,
)
from pullbox.services.semantic_matching import ImportPolicy, SemanticMatchEngine


@pytest.mark.parametrize("recorded", [False, True])
@pytest.mark.parametrize("file_title", ["New Avengers Finale", "New Avengers - Ultron Forever"])
def test_corroborated_distinct_title_blocks_id_and_number_shortcuts(recorded, file_title):
    parent = ImportedSeries(
        raw_series_name="New Avengers",
        cv_title="New Avengers",
        cv_id=100,
        cv_match_method="mylar3_cv_id",
        status=ImportSeriesStatus.MATCHED,
    )
    file = ImportedFile(
        file_name=f"{file_title} 001 (2015).cbr",
        parsed_series="New Avengers",
        parsed_issue_number=1,
        parsed_year=2013,
        comicvine_issue_id=200 if recorded else None,
        diagnostics={
            "comicvine_series_id": 100,
            "metadata_signals": {
                "comicvine_issue_id": "mylar3",
                "comicvine_series_id": "mylar3",
            },
        },
    )
    metadata = SourceMetadata(
        original_title=file.file_name,
        series_name="New Avengers",
        issue_number=1,
        issue_type=IssueType.ISSUE,
        comicvine_issue_id=file.comicvine_issue_id,
        diagnostics={
            "filename_parse": {"series_name": file_title, "issue_number": 1, "year": 2015},
            "archive_entry_issue_hint": {
                "series_name": file_title,
                "issue_number": 1,
                "confidence": "strong",
            },
        },
    )
    entry = (None, 200, False, None, "Breakout")
    candidate, conflict = _evaluate_file_match_candidate(
        imp_series=parent,
        imp_file=file,
        target_index=FileMatchTargetIndex(cv_id_map={200: entry}, number_map={1: entry}),
        target_series=None,
        file_metadata=metadata,
        semantic_match_engine=SemanticMatchEngine(policy=ImportPolicy()),
        build_import_metadata_conflict=build_import_metadata_conflict,
        series_high_confidence=True,
    )
    assert candidate is None
    assert conflict["conflict_type"] == "corroborated_file_series_mismatch"
    assert conflict["preserve_series_match"] is True
    assert parent.cv_id == 100


def test_uncontradicted_mylar_identity_keeps_local_fast_path():
    parent = ImportedSeries(raw_series_name="New Avengers", cv_id=100)
    file = ImportedFile(
        file_name="New Avengers 001 (2013).cbz",
        comicvine_issue_id=200,
        parsed_issue_number=1,
        diagnostics={
            "comicvine_series_id": 100,
            "metadata_signals": {
                "comicvine_issue_id": "mylar3",
                "comicvine_series_id": "mylar3",
            },
        },
    )
    engine = Mock()
    entry = (None, 200, False, None, "Breakout")
    candidate, conflict = _evaluate_file_match_candidate(
        imp_series=parent,
        imp_file=file,
        target_index=FileMatchTargetIndex(cv_id_map={200: entry}),
        target_series=None,
        file_metadata=SourceMetadata(original_title=file.file_name),
        semantic_match_engine=engine,
        build_import_metadata_conflict=build_import_metadata_conflict,
        series_high_confidence=True,
    )
    assert candidate is not None
    assert conflict is None
    engine.match_against_issue.assert_not_called()


@pytest.mark.parametrize(
    ("file_title", "issue_number", "issue_cv_id"),
    [
        ("Action Comics", 969, 566668),
        ("Thunderbolts", 105, 234803),
    ],
)
def test_corroborated_unrelated_title_blocks_parent_issue_number_inheritance(
    file_title: str, issue_number: int, issue_cv_id: int
) -> None:
    parent = ImportedSeries(
        raw_series_name="Fritzi Ritz",
        cv_title="Fritzi Ritz",
        cv_id=31895,
        cv_match_method="mylar3_cv_id",
        status=ImportSeriesStatus.MATCHED,
    )
    file = ImportedFile(
        file_name=f"{file_title} {issue_number} (2017).cbz",
        parsed_series="Fritzi Ritz",
        parsed_issue_number=issue_number,
        diagnostics={
            "comicvine_series_id": 31895,
            "metadata_signals": {"comicvine_series_id": "mylar3"},
        },
    )
    metadata = SourceMetadata(
        original_title=file.file_name,
        series_name="Fritzi Ritz",
        issue_number=issue_number,
        issue_type=IssueType.ISSUE,
        diagnostics={
            "filename_parse": {
                "series_name": file_title,
                "issue_number": issue_number,
                "year": 2017,
            },
            "comicinfo": {"series": file_title, "number": str(issue_number)},
        },
    )
    entry = (None, issue_cv_id, False, None, "Existing parent issue")

    candidate, conflict = _evaluate_file_match_candidate(
        imp_series=parent,
        imp_file=file,
        target_index=FileMatchTargetIndex(number_map={issue_number: entry}),
        target_series=None,
        file_metadata=metadata,
        semantic_match_engine=SemanticMatchEngine(policy=ImportPolicy()),
        build_import_metadata_conflict=build_import_metadata_conflict,
        series_high_confidence=True,
    )

    assert candidate is None
    assert conflict is not None
    assert conflict["conflict_type"] == "corroborated_file_series_mismatch"
    assert conflict["source_series"] == file_title
    assert conflict["target_series"] == "Fritzi Ritz"
    assert conflict["preserve_series_match"] is True


def test_unrelated_reading_order_filename_keeps_parent_issue_match_review_only() -> None:
    """A clear foreign title cannot inherit a parent issue merely by sharing its number."""
    parent = ImportedSeries(
        raw_series_name="Fritzi Ritz",
        cv_title="Fritzi Ritz",
        cv_id=31895,
        cv_match_method="mylar3_cv_id",
        status=ImportSeriesStatus.MATCHED,
    )
    file = ImportedFile(
        file_name="042 - Thunderbolts 105 (converted).cbz",
        parsed_series="Fritzi Ritz",
        parsed_issue_number=105,
        diagnostics={
            "source_issue_type": IssueType.ISSUE.value,
            "comicvine_series_id": 31895,
            "metadata_signals": {
                "comicvine_series_id": "mylar3",
                "issue_number": "release_title",
            },
            "source_metadata": {
                "archive_metadata_loaded": True,
                "archive_entry_issue_hint_checked": True,
            },
        },
    )
    metadata = source_metadata_for_import_file(parent, file)
    entry = (None, None, False, None, "Coincidental parent issue")

    assert metadata.diagnostics["filename_parse"]["series_name"] == (
        "042 - Thunderbolts (converted)"
    )

    candidate, conflict = _evaluate_file_match_candidate(
        imp_series=parent,
        imp_file=file,
        target_index=FileMatchTargetIndex(number_map={105: entry}),
        target_series=None,
        file_metadata=metadata,
        semantic_match_engine=SemanticMatchEngine(policy=ImportPolicy()),
        build_import_metadata_conflict=build_import_metadata_conflict,
        series_high_confidence=True,
    )

    assert candidate is None
    assert conflict is not None
    assert conflict["conflict_type"] == "corroborated_file_series_mismatch"
    assert conflict["corroborating_signals"] == ["filename_parse"]
    assert conflict["preserve_series_match"] is True


@pytest.mark.parametrize(
    "file_name",
    [
        "original issue name 001.cbz",
        "Saga 001 dup.cbz",
    ],
)
def test_uncorroborated_free_form_filename_does_not_override_parent(file_name: str) -> None:
    metadata = SourceMetadata(
        original_title=file_name,
        series_name="Batman",
        issue_number=1,
        diagnostics={
            "filename_parse": {
                "series_name": file_name.rsplit(" ", 1)[0],
                "issue_number": 1,
            }
        },
    )

    assert corroborated_import_title_conflict(metadata, "Batman") is None


@pytest.mark.parametrize("corroboration", ["comicinfo", "archive_entry_issue_hint"])
def test_local_title_guard_is_shared_by_source_types(corroboration):
    metadata = SourceMetadata(
        original_title="New Avengers Finale 01 (2010).cbz",
        diagnostics={
            "filename_parse": {"series_name": "New Avengers Finale"},
            corroboration: {
                "series": "New Avengers Finale",
                "series_name": "New Avengers Finale",
                "issue_number": 1,
                "confidence": "strong",
            },
        },
    )
    assert corroborated_import_title_conflict(metadata, "New Avengers") is not None


@pytest.mark.parametrize(
    "issue_type,confidence,title",
    [
        (IssueType.ISSUE, "weak", "New Avengers Finale"),
        (IssueType.TPB, "strong", "New Avengers Finale"),
        (IssueType.ISSUE, "strong", "The New Avengers"),
    ],
)
def test_title_guard_preserves_weak_evidence_collections_and_article_variants(
    issue_type, confidence, title
):
    metadata = SourceMetadata(
        original_title=f"{title} 01 (2010).cbz",
        issue_type=issue_type,
        diagnostics={
            "filename_parse": {"series_name": title},
            "archive_entry_issue_hint": {
                "series_name": title,
                "issue_number": 1,
                "confidence": confidence,
            },
        },
    )
    assert corroborated_import_title_conflict(metadata, "New Avengers") is None


@pytest.mark.parametrize(
    "source_title,target_title,issue_type",
    [
        ("X-Men Red", "X-Men: Red Annual", IssueType.ANNUAL),
        ("Justice League", "Justice League 2022 Annual", IssueType.ANNUAL),
        ("Justice League - Darkseid War", "Justice League Darkseid War Special", IssueType.SPECIAL),
        ("Cataclysm - Ultimate Comics Ultimates", "Cataclysm: The Ultimates", IssueType.ISSUE),
    ],
)
def test_subtitle_guard_does_not_replace_type_or_alternate_title_matching(
    source_title, target_title, issue_type
):
    metadata = SourceMetadata(
        original_title=f"{source_title} 001 (2015).cbr",
        issue_type=issue_type,
        diagnostics={
            "filename_parse": {"series_name": source_title},
            "archive_entry_issue_hint": {
                "series_name": source_title,
                "issue_number": 1,
                "confidence": "strong",
            },
        },
    )
    assert corroborated_import_title_conflict(metadata, target_title) is None

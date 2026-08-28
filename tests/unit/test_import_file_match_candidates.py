"""Tests for import file-match candidate helpers."""

from __future__ import annotations

from pullbox.models.import_job import ImportedFile, ImportedSeries
from pullbox.models.issue import Issue, IssueType
from pullbox.models.series import Series
from pullbox.services.import_file_match_candidates import (
    build_file_match_target_context,
    reset_file_match_state,
    select_file_match_candidate,
)
from pullbox.services.import_file_match_targets import (
    PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD,
    FileMatchTargetIndex,
)
from pullbox.services.import_source_metadata import source_metadata_for_import_file
from pullbox.services.semantic_matching import ImportPolicy, SemanticMatchEngine


def test_reset_file_match_state_clears_transient_grouping_artifacts() -> None:
    imp_file = ImportedFile(
        conflict_group_id=3,
        duplicate_group_id=4,
        duplicate_of_file_id=5,
        content_hash="abc123",
        is_preferred=True,
    )

    reset_file_match_state(imp_file)

    assert imp_file.conflict_group_id is None
    assert imp_file.duplicate_group_id is None
    assert imp_file.duplicate_of_file_id is None
    assert imp_file.content_hash is None
    assert imp_file.is_preferred is False


def test_select_file_match_candidate_prefers_comicvine_issue_id() -> None:
    issue_by_cv = Issue(id=10, issue_number=1.0, comicvine_id=1001, title="CV Issue")
    issue_by_number = Issue(id=11, issue_number=2.0, comicvine_id=1002, title="Number Issue")
    target_index = FileMatchTargetIndex(
        cv_id_map={1001: (10, 1001, False, issue_by_cv, "CV Issue")},
        number_map={2.0: (11, 1002, True, issue_by_number, "Number Issue")},
    )
    imp_file = ImportedFile(comicvine_issue_id=1001, parsed_issue_number=2.0)

    candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=False,
    )

    assert candidate is not None
    assert candidate.matched_issue_id == 10
    assert candidate.matched_issue_cv_id == 1001
    assert candidate.has_library_file is False
    assert candidate.matched_issue is issue_by_cv
    assert candidate.confidence == "high"
    assert candidate.method == "comicvine_id"


def test_select_file_match_candidate_falls_back_to_issue_number_confidence() -> None:
    issue = Issue(id=12, issue_number=19.0, comicvine_id=1019, title="Issue 19")
    target_index = FileMatchTargetIndex(
        number_map={19.0: (12, 1019, True, issue, "Issue 19")},
    )
    imp_file = ImportedFile(parsed_issue_number=19.0)

    medium_candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=False,
    )
    high_candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=True,
    )

    assert medium_candidate is not None
    assert medium_candidate.method == "issue_number"
    assert medium_candidate.confidence == "medium"
    assert medium_candidate.has_library_file is True
    assert high_candidate is not None
    assert high_candidate.confidence == "high"


def test_explicit_comicvine_issue_id_never_falls_back_to_another_issue_number() -> None:
    regular_issue = Issue(
        id=12,
        issue_number=1.0,
        comicvine_id=900001,
        title="Regular issue",
    )
    target_index = FileMatchTargetIndex(
        cv_id_map={900001: (12, 900001, False, regular_issue, "Regular issue")},
        number_map={1.0: (12, 900001, False, regular_issue, "Regular issue")},
    )
    annual_file = ImportedFile(
        comicvine_issue_id=950001,
        parsed_issue_number=1.0,
    )

    candidate = select_file_match_candidate(
        annual_file,
        target_index,
        series_high_confidence=True,
    )

    assert candidate is None


def test_select_file_match_candidate_marks_provisional_mylar_target() -> None:
    target_index = FileMatchTargetIndex(
        number_map={4.0: (None, None, False, None, None)},
        synthetic_issue_types={4.0: IssueType.SPECIAL},
        provisional_issue_numbers={4.0},
    )
    imp_file = ImportedFile(parsed_issue_number=4.0)

    candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=True,
    )

    assert candidate is not None
    assert candidate.method == PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD
    assert candidate.synthetic_issue_type == IssueType.SPECIAL


def test_select_file_match_candidate_recovers_volume_issue_number_from_filename() -> None:
    target_index = FileMatchTargetIndex(
        number_map={2.0: (None, 1030016, False, None, "Vol. 2: A Dark Interlude")},
    )
    imp_file = ImportedFile(
        file_name="Fearscape.Vol.02.A.Dark.Interlude.2023.pdf",
        parsed_issue_number=None,
        diagnostics={},
    )

    candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=True,
    )

    assert candidate is not None
    assert candidate.matched_issue_cv_id == 1030016
    assert candidate.target_issue_number == 2.0
    assert candidate.method == "issue_number"


def test_select_file_match_candidate_uses_single_issue_provider_summary_fallback() -> None:
    target_index = FileMatchTargetIndex(
        number_map={1.0: (None, 165113, False, None, "Wasted Space: The Cosmic Collection")},
    )
    imp_file = ImportedFile(
        file_name="Wasted.Space.The.Cosmic.Collection.2023.pdf",
        parsed_series="Wasted Space The Cosmic Collection",
        parsed_issue_number=None,
        comicvine_issue_id=None,
    )

    candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=True,
    )

    assert candidate is not None
    assert candidate.matched_issue_cv_id == 165113
    assert candidate.target_issue_number == 1.0
    assert candidate.confidence == "medium"
    assert candidate.method == "single_issue_series"


def test_select_file_match_candidate_rejects_single_issue_fallback_for_low_confidence() -> None:
    target_index = FileMatchTargetIndex(
        number_map={1.0: (None, 165113, False, None, "Wasted Space: The Cosmic Collection")},
    )
    imp_file = ImportedFile(parsed_issue_number=None, comicvine_issue_id=None)

    assert (
        select_file_match_candidate(
            imp_file,
            target_index,
            series_high_confidence=False,
        )
        is None
    )


def test_select_file_match_candidate_returns_none_without_target() -> None:
    assert (
        select_file_match_candidate(
            ImportedFile(parsed_issue_number=99.0),
            FileMatchTargetIndex(),
            series_high_confidence=True,
        )
        is None
    )


def test_build_file_match_target_context_uses_existing_series_and_issue() -> None:
    issue = Issue(
        id=21,
        issue_number=7.0,
        comicvine_id=2007,
        title="Issue Seven",
        issue_type=IssueType.ANNUAL,
    )
    target_index = FileMatchTargetIndex(
        existing_series=Series(title="Absolute Batman", year_start=2024),
        number_map={7.0: (21, 2007, False, issue, "Issue Seven")},
    )
    imp_file = ImportedFile(parsed_issue_number=7.0)
    candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=True,
    )
    assert candidate is not None

    context = build_file_match_target_context(
        ImportedSeries(raw_series_name="Ignored", raw_year=1999),
        imp_file,
        candidate,
        existing_series=target_index.existing_series,
    )

    assert context.series_title == "Absolute Batman"
    assert context.series_year == 2024
    assert context.issue_number == 7.0
    assert context.issue_type == IssueType.ANNUAL
    assert context.issue_cv_id == 2007
    assert context.issue_title == "Issue Seven"


def test_build_file_match_target_context_uses_import_series_for_provider_summary() -> None:
    target_index = FileMatchTargetIndex(number_map={8.0: (None, 2008, False, None, "Issue Eight")})
    imp_file = ImportedFile(parsed_issue_number=8.0)
    candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=True,
    )
    assert candidate is not None

    context = build_file_match_target_context(
        ImportedSeries(
            raw_series_name="Absolute Wonder Woman",
            raw_year=2024,
            cv_title="Absolute Wonder Woman",
            cv_year=2024,
        ),
        imp_file,
        candidate,
        existing_series=None,
    )

    assert context.series_title == "Absolute Wonder Woman"
    assert context.series_year == 2024
    assert context.issue_number == 8.0
    assert context.issue_type == IssueType.ISSUE
    assert context.issue_cv_id == 2008
    assert context.issue_title == "Issue Eight"


def test_build_file_match_target_context_uses_annual_type_for_provider_summary() -> None:
    target_index = FileMatchTargetIndex(
        number_map={1.0: (None, 1155837, False, None, "Cursing and Cursed")}
    )
    imp_file = ImportedFile(
        file_name="Absolute Wonder Woman 2026 Annual (2026) #001.cbz",
        parsed_issue_number=1.0,
        diagnostics={
            "source_issue_type": IssueType.ANNUAL.value,
            "source_metadata": {
                "filename_parse": {
                    "series_name": "Absolute Wonder Woman",
                    "issue_number": 1.0,
                    "year": 2026,
                    "volume": None,
                    "issue_type": IssueType.ANNUAL.value,
                }
            },
        },
    )
    candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=True,
    )
    assert candidate is not None

    context = build_file_match_target_context(
        ImportedSeries(
            raw_series_name="Absolute Wonder Woman",
            raw_year=2026,
            cv_title="Absolute Wonder Woman 2026 Annual",
            cv_year=2026,
        ),
        imp_file,
        candidate,
        existing_series=None,
    )

    assert context.issue_number == 1.0
    assert context.issue_type == IssueType.ANNUAL
    assert context.issue_cv_id == 1155837
    assert context.issue_title == "Cursing and Cursed"
    assert context.series_title == "Absolute Wonder Woman"


def test_build_file_match_target_context_keeps_annual_out_of_base_series_summary() -> None:
    target_index = FileMatchTargetIndex(
        number_map={1.0: (None, 1048076, False, None, "All Weather Turns To Storm")}
    )
    imp_file = ImportedFile(
        file_name="Immortal Thor Annual 001 (2024) (Digital).cbz",
        parsed_issue_number=1.0,
        diagnostics={
            "source_issue_type": IssueType.ANNUAL.value,
            "source_metadata": {
                "filename_parse": {
                    "series_name": "Immortal Thor",
                    "issue_number": 1.0,
                    "year": 2024,
                    "volume": None,
                    "issue_type": IssueType.ANNUAL.value,
                }
            },
        },
    )
    candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=True,
    )
    assert candidate is not None

    context = build_file_match_target_context(
        ImportedSeries(
            raw_series_name="Immortal Thor",
            raw_year=2024,
            cv_title="Immortal Thor",
            cv_year=2024,
        ),
        imp_file,
        candidate,
        existing_series=None,
    )

    assert context.issue_type == IssueType.ISSUE
    assert context.series_title == "Immortal Thor"


def test_annual_import_bucket_label_does_not_make_base_target_annual() -> None:
    target_index = FileMatchTargetIndex(
        number_map={1.0: (None, 1048076, False, None, "All Weather Turns To Storm")}
    )
    imp_file = ImportedFile(
        file_name="Immortal Thor Annual 001 (2024) (Digital).cbz",
        parsed_series="Immortal Thor",
        parsed_issue_number=1.0,
        parsed_year=2024,
        diagnostics={
            "source_issue_type": IssueType.ANNUAL.value,
            "source_metadata": {
                "filename_parse": {
                    "series_name": "Immortal Thor",
                    "issue_number": 1.0,
                    "year": 2024,
                    "volume": None,
                    "issue_type": IssueType.ANNUAL.value,
                }
            },
        },
    )
    candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=True,
    )
    assert candidate is not None

    context = build_file_match_target_context(
        ImportedSeries(
            raw_series_name="Immortal Thor Annual",
            raw_year=2024,
            cv_title="Immortal Thor",
            cv_year=2024,
        ),
        imp_file,
        candidate,
        existing_series=None,
    )

    assert context.issue_type == IssueType.ISSUE
    assert context.series_title == "Immortal Thor"


def test_annual_provider_summary_context_passes_semantic_match() -> None:
    target_index = FileMatchTargetIndex(number_map={1.0: (None, 451141, False, None, "The Folly")})
    imp_series = ImportedSeries(
        raw_series_name="Crossed",
        raw_year=2014,
        cv_title="Crossed 2014 Annual",
        cv_year=2014,
        diagnostics={"source_issue_type": IssueType.ANNUAL.value},
    )
    imp_file = ImportedFile(
        file_name="Crossed 2014 Annual (2014) #001.cbr",
        file_path="/imports/test/Crossed 2014 Annual (2014) #001.cbr",
        parsed_series="Crossed",
        parsed_issue_number=1.0,
        parsed_year=2014,
        diagnostics={
            "source_issue_type": IssueType.ANNUAL.value,
            "source_metadata": {
                "filename_parse": {
                    "series_name": "Crossed",
                    "issue_number": 1.0,
                    "year": 2014,
                    "volume": None,
                    "issue_type": IssueType.ANNUAL.value,
                }
            },
        },
    )
    candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=True,
    )
    assert candidate is not None

    context = build_file_match_target_context(
        imp_series,
        imp_file,
        candidate,
        existing_series=None,
    )
    metadata = source_metadata_for_import_file(imp_series, imp_file)
    decision = SemanticMatchEngine(policy=ImportPolicy()).match_against_issue(
        metadata=metadata,
        wanted_series=context.series_title,
        wanted_issue=float(context.issue_number or 0.0),
        wanted_year=context.series_year,
        wanted_issue_type=context.issue_type,
        wanted_issue_cv_id=context.issue_cv_id,
        wanted_issue_title=context.issue_title,
    )

    assert context.series_title == "Crossed"
    assert decision.is_match is True
    assert decision.match_method == "issue_number"


def test_build_file_match_target_context_uses_single_issue_fallback_issue_number() -> None:
    target_index = FileMatchTargetIndex(
        number_map={1.0: (None, 165113, False, None, "Wasted Space: The Cosmic Collection")}
    )
    imp_file = ImportedFile(parsed_issue_number=None)
    candidate = select_file_match_candidate(
        imp_file,
        target_index,
        series_high_confidence=True,
    )
    assert candidate is not None

    context = build_file_match_target_context(
        ImportedSeries(
            raw_series_name="Wasted Space The Cosmic Collection",
            raw_year=2023,
            cv_title="Wasted Space: The Cosmic Collection",
            cv_year=2023,
        ),
        imp_file,
        candidate,
        existing_series=None,
    )

    assert context.issue_number == 1.0

"""Lettered issue identities must survive discovery and download validation."""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.core.collection_scanner import CollectionScanner
from pullbox.core.issue_numbers import parse_issue_number_text
from pullbox.core.naming import parse_filename
from pullbox.models.import_job import ImportedFile, ImportedSeries
from pullbox.models.issue import IssueType
from pullbox.providers.base import IssueSummary, ReleaseResult
from pullbox.providers.direct.contract import DirectCandidate, DirectParsedCandidate
from pullbox.services.direct_acquisition_planner_service import _coverage_numbers_match
from pullbox.services.direct_search_coordinator import _validate_direct_candidate
from pullbox.services.import_file_match_candidates import select_file_match_candidate
from pullbox.services.import_file_match_targets import (
    _requested_issue_numbers,
    load_file_match_target_index,
)
from pullbox.services.import_provider_cache import CachedImportMetadataProvider
from pullbox.services.release_validator import ReleaseValidator
from pullbox.services.search_service import SearchService
from pullbox.services.search_targets import IssueSearchTarget

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("series", "wanted", "offered", "expected"),
    [
        ("Gen13", "13A", "013a", True),
        ("Gen13", "13B", "13b", True),
        ("Gen13", "13C", "13c", True),
        ("Gen13", "13A", "13B", False),
        ("Gen13", "13A", "13", False),
        ("Gen13", "13", "13A", False),
        ("X-Manowar", "50X", "50x", True),
        ("X-Manowar", "50O", "50o", True),
        ("X-Manowar", "50X", "50O", False),
        ("Gen13", "0.5", "0.5", True),
        ("Gen13", "-1", "-1", True),
    ],
)
def test_direct_search_validates_the_exact_requested_issue(
    series: str, wanted: str, offered: str, expected: bool
) -> None:
    title = f"{series} #{offered} (1996).cbz"
    candidate = DirectCandidate(
        provider_candidate_id="lettered-issue",
        source_reference="https://example.com/release",
        display_title=title,
        raw_title=title,
        parsed=DirectParsedCandidate(series_title=series, issue_numbers=[offered]),
        provider_confidence=0.95,
    )
    release = ReleaseResult(
        title=title,
        indexer_name="Fixture",
        download_url=candidate.source_reference,
        size_bytes=40 * 1024 * 1024,
        age_days=1,
        seeders=None,
        leechers=None,
        grabs=None,
        protocol=AcquisitionProtocol.DIRECT,
    )
    target = IssueSearchTarget(
        issue_id=1,
        series_id=1,
        series_title=series,
        issue_number=parse_issue_number_text(wanted)[0],
        issue_number_text=wanted,
        issue_type=IssueType.ISSUE,
        series_year=1996,
    )

    result = _validate_direct_candidate(
        ReleaseValidator(), release=release, candidate=candidate, target=target
    )

    assert result.is_match is expected
    if not expected:
        assert "Issue mismatch" in (result.rejection_reason or "")
    best = SearchService.evaluate_results(
        [release],
        min_score=0,
        wanted_series=series,
        wanted_issue=target.issue_number,
        wanted_issue_number_text=wanted,
        wanted_year=1996,
    )
    assert (best is not None) is expected


@pytest.mark.parametrize(
    ("wanted", "offered", "expected"),
    [
        ("13A", "013a", True),
        ("13A", "13B", False),
        ("13A", "13", False),
        ("50O", "50X", False),
        ("0.5", "00.50", True),
        ("-1", "-01", True),
    ],
)
def test_artifact_coverage_does_not_alias_lettered_issues(
    wanted: str, offered: str, expected: bool
) -> None:
    assert _coverage_numbers_match(wanted, offered) is expected


@pytest.mark.parametrize("number", ["13A", "13B", "13C", "50X", "50O", "0.5", "-1"])
async def test_folder_discovery_preserves_exact_issue_designation(
    tmp_path: Path, number: str
) -> None:
    path = tmp_path / f"Gen13 #{number} (1996).cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("page01.jpg", b"fixture page")
        archive.writestr("page02.jpg", b"fixture page")
    discovered = [series async for series in CollectionScanner().scan(tmp_path)]
    files = [item for series in discovered for item in series.files]

    assert len(files) == 1
    assert files[0].issue_number_raw == number
    parsed = parse_filename(path.name)
    assert parsed is not None
    assert getattr(parsed, "issue_number_text", None) == number


async def test_new_series_provider_targets_do_not_collapse_suffix_siblings() -> None:
    provider = AsyncMock()
    provider.get_issues_for_series.return_value = [
        IssueSummary(
            provider_id=str(100 + index),
            issue_number=13,
            issue_number_text=number,
            title=None,
            release_date=None,
            cover_url=None,
            issue_type="issue",
        )
        for index, number in enumerate(["13A", "13B", "13C"])
    ]
    series = ImportedSeries(raw_series_name="Gen13", cv_id=123, cv_issue_count=3)
    files = [
        ImportedFile(
            file_name=f"Gen13 #{number}.cbz", parsed_issue_number=13, issue_number_raw=number
        )
        for number in ["13A", "13B", "13C", "13"]
    ]
    targets = await load_file_match_target_index(
        AsyncMock(), series, duplicate_series=False, metadata_provider=provider, files=files
    )
    candidates = [
        select_file_match_candidate(item, targets, series_high_confidence=True) for item in files
    ]

    assert [item.matched_issue_cv_id if item else None for item in candidates] == [
        100,
        101,
        102,
        None,
    ]


def test_targeted_import_lookup_preserves_letters_without_extra_requests() -> None:
    files = [
        ImportedFile(
            file_name=f"Gen13 #{number}.cbz", parsed_issue_number=13, issue_number_raw=number
        )
        for number in ["13a", "13A", "13B", "13"]
    ]
    assert _requested_issue_numbers(files) == [13.0, "13A", "13B"]


async def test_import_cache_filters_exact_numbers_not_numeric_aliases() -> None:
    provider = AsyncMock()
    provider.get_issues_for_series.return_value = [
        IssueSummary(
            provider_id=number,
            issue_number=13,
            issue_number_text=number,
            title=None,
            release_date=None,
            cover_url=None,
            issue_type="issue",
        )
        for number in ["13", "13A", "13B"]
    ]
    cache = CachedImportMetadataProvider(provider)
    results = await cache.get_issues_for_series_by_numbers("123", [13.0])
    assert [issue.issue_number_text for issue in results] == ["13"]
    results = await cache.get_issues_for_series_by_numbers("123", ["013a", "13A"])
    assert [issue.issue_number_text for issue in results] == ["13A"]
    provider.get_issues_for_series.assert_awaited_once()


@pytest.mark.parametrize("reverse", [False, True])
async def test_trusted_and_provisional_lettered_issues_do_not_share_match_state(
    reverse: bool,
) -> None:
    series = ImportedSeries(raw_series_name="Gen13", cv_id=123, cv_match_method="mylar3_cv_id")
    provisional = ImportedFile(
        file_name="Gen13 #13A.cbz", parsed_issue_number=13, issue_number_raw="13A"
    )
    trusted = ImportedFile(
        file_name="Gen13 #13B.cbz",
        parsed_issue_number=13,
        issue_number_raw="13B",
        comicvine_issue_id=100,
        diagnostics={
            "comicvine_series_id": 123,
            "metadata_signals": {"comicvine_issue_id": "mylar3"},
        },
    )
    files = [trusted, provisional] if reverse else [provisional, trusted]
    provider = AsyncMock()
    targets = await load_file_match_target_index(
        AsyncMock(), series, duplicate_series=False, metadata_provider=provider, files=files
    )
    result = select_file_match_candidate(provisional, targets, series_high_confidence=True)
    assert result is not None
    assert result.method == "import_reconcile_provisional_issue"
    assert result.matched_issue_cv_id is None
    assert 13 not in targets.provisional_issue_numbers
    assert 13 not in targets.synthetic_issue_types
    provider.get_issues_for_series.assert_not_awaited()

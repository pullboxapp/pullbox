"""Unit tests for ComicVine matching in import service."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from pullbox.core.exceptions import ImportProviderDegradedError, RateLimitError
from pullbox.core.source_metadata import MetadataSignal, SourceMetadata, SourceMetadataExtractor
from pullbox.models.issue import IssueType
from pullbox.providers.base import IssueSummary, SeriesMetadata, SeriesSearchResult
from pullbox.providers.metadata.comicvine import ComicVineError
from pullbox.services.import_service import (
    _score_cv_result,
    evaluate_comicvine_match,
    match_to_comicvine,
)


def _make_search_result(
    *,
    provider_id: str = "97508",
    title: str = "Batman",
    year_start: int | None = 2016,
    publisher: str | None = "DC Comics",
    issue_count: int | None = 85,
    status: str | None = "Ended",
) -> SeriesSearchResult:
    """Create a SeriesSearchResult for testing."""
    return SeriesSearchResult(
        provider_id=provider_id,
        title=title,
        year_start=year_start,
        publisher=publisher,
        issue_count=issue_count,
        status=status,
        cover_url=None,
        description=None,
    )


def _make_series_metadata(
    *,
    provider_id: str = "97508",
    title: str = "Batman",
    year_start: int | None = 2016,
    publisher: str | None = "DC Comics",
) -> SeriesMetadata:
    """Create a SeriesMetadata for testing."""
    return SeriesMetadata(
        provider_id=provider_id,
        title=title,
        sort_title=title,
        year_start=year_start,
        year_end=None,
        status="Ended",
        publisher=publisher,
        description=None,
        cover_url=None,
        issue_count=85,
        comicvine_url=f"https://comicvine.gamespot.com/batman/4050-{provider_id}/",
    )


class _GlobalSearchProviderDouble:
    """Provider double that exposes both legacy and global series search paths."""

    def __init__(
        self,
        *,
        page_results: list[SeriesSearchResult] | None = None,
        global_results: list[SeriesSearchResult] | None = None,
        issue_summaries: dict[tuple[str, float], list[IssueSummary]] | None = None,
    ) -> None:
        self.page_results = page_results or []
        self.global_results = global_results or []
        self.issue_summaries = issue_summaries or {}
        self.page_search_calls = 0
        self.global_search_calls = 0
        self.issue_number_calls = 0

    async def search_series(
        self,
        _query: str,
        _year: int | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
        suppress_errors: bool = True,
    ) -> list[SeriesSearchResult]:
        self.page_search_calls += 1
        return self.page_results[offset : offset + limit]

    async def search_series_globally(
        self,
        _query: str,
        *,
        max_results: int = 1000,
        batch_size: int = 100,
        suppress_errors: bool = True,
    ) -> tuple[list[SeriesSearchResult], int]:
        self.global_search_calls += 1
        results = self.global_results[:max_results]
        return results, len(results)

    async def get_issues_for_series_by_numbers(
        self,
        series_provider_id: str,
        issue_numbers: list[float],
    ) -> list[IssueSummary]:
        self.issue_number_calls += 1
        summaries: list[IssueSummary] = []
        for issue_number in issue_numbers:
            summaries.extend(
                self.issue_summaries.get((series_provider_id, float(issue_number)), [])
            )
        return summaries


class TestScoreCvResult:
    """Test the _score_cv_result scoring function."""

    def test_exact_title_and_year(self) -> None:
        score = _score_cv_result("Batman", 2016, "Batman", 2016, "DC Comics")
        assert score >= 0.95

    def test_exact_title_no_year(self) -> None:
        score = _score_cv_result("Batman", None, "Batman", None, "DC Comics")
        assert score >= 0.70

    def test_fuzzy_title_match(self) -> None:
        score = _score_cv_result("Batmn", 2016, "Batman", 2016, "DC Comics")
        assert 0.50 < score < 0.95

    def test_completely_different(self) -> None:
        score = _score_cv_result("Saga", 2012, "Batman", 2016, "DC Comics")
        assert score < 0.50

    def test_year_off_by_one(self) -> None:
        score = _score_cv_result("Batman", 2016, "Batman", 2017, "DC Comics")
        assert score >= 0.85

    def test_year_very_different(self) -> None:
        exact_score = _score_cv_result("Batman", 2016, "Batman", 2016, "DC Comics")
        diff_score = _score_cv_result("Batman", 2016, "Batman", 1940, "DC Comics")
        assert diff_score < exact_score

    def test_publisher_bonus(self) -> None:
        with_pub = _score_cv_result("Batman", 2016, "Batman", 2016, "DC Comics")
        without_pub = _score_cv_result("Batman", 2016, "Batman", 2016, None)
        assert with_pub > without_pub

    def test_parenthesized_publisher_suffix_scores_as_title_metadata(self) -> None:
        score = _score_cv_result(
            "Coraline (Harper Collins)",
            2008,
            "Coraline",
            2008,
            "HarperCollins",
        )
        assert score >= 0.95

    def test_parenthesized_non_publisher_suffix_does_not_score_as_exact_title(self) -> None:
        score = _score_cv_result(
            "Coraline (The Graphic Novel)",
            2008,
            "Coraline",
            2008,
            "HarperCollins",
        )
        assert score < 0.95

    def test_score_in_range(self) -> None:
        score = _score_cv_result("Batman", 2016, "Batman", 2016, "DC Comics")
        assert 0.0 <= score <= 1.0


class TestMatchToComicvine:
    """Test the match_to_comicvine function."""

    @pytest.mark.asyncio
    async def test_mylar3_cv_id_direct_lookup(self) -> None:
        """Trusted Mylar3 volume IDs avoid cold provider lookups during Step 2."""
        provider = AsyncMock()
        provider.get_series.return_value = _make_series_metadata()

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
            mylar3_cv_id=97508,
        )

        assert result is not None
        assert result["cv_id"] == 97508
        assert result["cv_match_method"] == "mylar3_cv_id"
        assert result["cv_match_score"] == 1.0
        assert result["cv_title"] == "Batman"
        assert result["cv_year"] == 2016
        provider.get_series.assert_not_called()

    @pytest.mark.asyncio
    async def test_comicinfo_cv_id_direct_lookup(self) -> None:
        """Untrusted ComicInfo CV IDs still use provider validation."""
        provider = AsyncMock()
        provider.get_series.return_value = _make_series_metadata()

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
            comicinfo_cv_id=97508,
        )

        assert result is not None
        assert result["cv_id"] == 97508
        assert result["cv_match_method"] == "comicinfo_cv_id"
        assert result["cv_match_score"] == 1.0
        provider.get_series.assert_called_once_with("97508")

    @pytest.mark.asyncio
    async def test_comicinfo_volume_url_cv_id_avoids_cold_lookup(self) -> None:
        """Explicit ComicVine volume URLs are trusted like Mylar IDs during Step 2."""
        provider = AsyncMock()
        source_metadata = SourceMetadata(
            original_title="Batman 001.cbz",
            series_name="Batman",
            year=2016,
            comicvine_series_id=97508,
            diagnostics={"comicvine_series_id_source": "comicvine_volume_url"},
        )

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
            source_metadata=source_metadata,
            comicinfo_cv_id=97508,
        )

        assert result is not None
        assert result["cv_id"] == 97508
        assert result["cv_match_method"] == "comicinfo_cv_id"
        provider.get_series.assert_not_called()

    @pytest.mark.asyncio
    async def test_exact_title_year_match(self) -> None:
        """Search finds exact title + year match."""
        provider = AsyncMock()
        provider.search_series.return_value = [_make_search_result()]

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
        )

        assert result is not None
        assert result["cv_id"] == 97508
        assert result["cv_match_method"] == "exact_title_year"
        assert result["cv_match_score"] >= 0.95

    @pytest.mark.asyncio
    async def test_fuzzy_title_match(self) -> None:
        """Search finds fuzzy title match."""
        provider = AsyncMock()
        provider.search_series.return_value = [_make_search_result()]

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batmna",
            raw_year=2016,
        )

        assert result is not None
        assert result["cv_match_method"] == "fuzzy_title"
        assert result["cv_match_score"] < 0.95

    @pytest.mark.asyncio
    async def test_no_match_found(self) -> None:
        """Search returns empty results."""
        provider = AsyncMock()
        provider.search_series.return_value = []

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Nonexistent Series XYZ",
            raw_year=2020,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_no_match_below_threshold(self) -> None:
        """Search returns results but none above threshold."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(title="Completely Different Series", year_start=1990),
        ]

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_rate_limit_retry(self) -> None:
        """Rate limit on first attempt, success on retry."""
        provider = AsyncMock()
        provider.search_series.side_effect = [
            RateLimitError("comicvine", retry_after_seconds=1),
            [_make_search_result()],
        ]

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
        )

        assert result is not None
        assert result["cv_id"] == 97508
        assert provider.search_series.call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_raises_provider_degraded(self) -> None:
        """Persistent timeout pauses import matching instead of faking a no-match."""
        provider = AsyncMock()
        provider.search_series.side_effect = httpx.TimeoutException("timed out")

        with pytest.raises(ImportProviderDegradedError):
            await match_to_comicvine(
                provider=provider,
                raw_name="Batman",
                raw_year=2016,
            )
        assert provider.search_series.call_count == 3

    @pytest.mark.asyncio
    async def test_timeout_retry_can_recover(self) -> None:
        """A transient timeout should retry instead of degrading into no results."""
        provider = AsyncMock()
        provider.search_series.side_effect = [
            httpx.TimeoutException("timed out"),
            [_make_search_result()],
        ]

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
        )

        assert result is not None
        assert result["cv_id"] == 97508
        assert provider.search_series.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_results_retry_can_recover(self) -> None:
        """A transient empty search result should retry before declaring no match."""
        provider = AsyncMock()
        provider.search_series.side_effect = [
            [],
            [_make_search_result()],
        ]

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
        )

        assert result is not None
        assert result["cv_id"] == 97508
        assert provider.search_series.call_count == 2

    @pytest.mark.asyncio
    async def test_transient_comicvine_error_retry_can_recover(self) -> None:
        """ComicVine transport errors should retry instead of becoming false no-matches."""
        provider = AsyncMock()
        provider.search_series.side_effect = [
            ComicVineError(0, "Request timed out: /search/"),
            [_make_search_result()],
        ]

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
        )

        assert result is not None
        assert result["cv_id"] == 97508
        assert provider.search_series.call_count == 2

    @pytest.mark.asyncio
    async def test_persistent_comicvine_error_raises_provider_degraded(self) -> None:
        """Repeated ComicVine transport failures should not harden into no-results."""
        provider = AsyncMock()
        provider.search_series.side_effect = ComicVineError(0, "Request timed out: /search/")

        with pytest.raises(ImportProviderDegradedError):
            await match_to_comicvine(
                provider=provider,
                raw_name="Batman",
                raw_year=2016,
            )

        assert provider.search_series.call_count == 3

    @pytest.mark.asyncio
    async def test_best_match_selected(self) -> None:
        """Multiple results — best scoring one is selected."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(title="Batman Adventures", year_start=1992, provider_id="111"),
            _make_search_result(title="Batman", year_start=2016, provider_id="222"),
            _make_search_result(title="Batman Beyond", year_start=1999, provider_id="333"),
        ]

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
        )

        assert result is not None
        assert result["cv_id"] == 222

    @pytest.mark.asyncio
    async def test_collection_source_prefers_collection_shaped_series(self) -> None:
        """Collection metadata should prefer an ended single-release candidate over a serial run."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(
                provider_id="162966",
                title="Absolute Martian Manhunter",
                year_start=2025,
                publisher="DC Comics",
                issue_count=10,
            ),
            _make_search_result(
                provider_id="168590",
                title="Absolute Martian Manhunter",
                year_start=2025,
                publisher="DC Comics",
                issue_count=1,
            ),
        ]
        extractor = SourceMetadataExtractor()
        source_metadata = extractor.from_release_title(
            "Absolute Martian Manhunter TPB Vol 1 (2025)"
        )

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Absolute Martian Manhunter",
            raw_year=2025,
            source_metadata=source_metadata,
        )

        assert result is not None
        assert result["cv_id"] == 168590

    @pytest.mark.asyncio
    async def test_standard_source_prefers_serial_run_over_collection_shaped_series(
        self,
    ) -> None:
        """Plain issue metadata should prefer a multi-issue run over a same-title volume."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(
                provider_id="162966",
                title="Absolute Martian Manhunter",
                year_start=2025,
                publisher="DC Comics",
                issue_count=11,
            ),
            _make_search_result(
                provider_id="168590",
                title="Absolute Martian Manhunter",
                year_start=2025,
                publisher="DC Comics",
                issue_count=1,
            ),
        ]
        extractor = SourceMetadataExtractor()
        source_metadata = extractor.from_release_title(
            "Absolute Martian Manhunter 001 (2025) (Digital).cbz"
        )

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Absolute Martian Manhunter",
            raw_year=2025,
            source_metadata=source_metadata,
        )

        assert result is not None
        assert result["cv_id"] == 162966

    @pytest.mark.asyncio
    async def test_ambiguous_pluralized_year_winner_becomes_series_conflict(self) -> None:
        """A fuzzy pluralized title should not outrank a near-tie exact title without review."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(
                provider_id="145525",
                title="Chicken Devils",
                year_start=2022,
                publisher="AfterShock Comics",
                issue_count=4,
            ),
            _make_search_result(
                provider_id="139451",
                title="Chicken Devil",
                year_start=2021,
                publisher="AfterShock Comics",
                issue_count=4,
            ),
        ]
        extractor = SourceMetadataExtractor()
        source_metadata = extractor.from_release_title("Chicken Devil 004 (2022)")

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Chicken Devil",
            raw_year=2022,
            source_metadata=source_metadata,
        )

        assert evaluation.match is None
        assert evaluation.diagnostics["kind"] == "series_conflict"
        assert evaluation.diagnostics["reason"] == "ambiguous_candidates"
        assert evaluation.diagnostics["selected_candidate"]["title"] == "Chicken Devils"
        assert evaluation.diagnostics["competing_candidate"]["title"] == "Chicken Devil"
        top_titles = [
            candidate["title"] for candidate in evaluation.diagnostics["top_candidates"][:2]
        ]
        assert top_titles == ["Chicken Devils", "Chicken Devil"]

    @pytest.mark.asyncio
    async def test_trusted_source_identity_conflict_skips_provider_lookup(self) -> None:
        provider = AsyncMock()
        source_metadata = SourceMetadata(
            original_title="Batman 001.cbz",
            series_name="Batman",
            comicvine_series_id=11111,
            signals={"comicvine_series_id": MetadataSignal.SIDECAR},
            diagnostics={
                "comicvine_series_id_source": "sidecar",
                "identity_conflicts": [
                    {
                        "field": "comicvine_series_id",
                        "comicinfo": 97508,
                        "sidecar": 11111,
                    }
                ],
            },
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
            source_metadata=source_metadata,
            comicinfo_cv_id=11111,
        )

        assert evaluation.match is None
        assert evaluation.diagnostics["kind"] == "series_conflict"
        assert evaluation.diagnostics["reason"] == "trusted_source_identity_conflict"
        provider.assert_not_called()
        provider.get_series.assert_not_awaited()
        provider.search_series.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_volume_subtitle_candidate_beats_base_series_conflict(self) -> None:
        """A collection subtitle in the filename should corroborate the matching CV title."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(
                provider_id="80837",
                title="The United States of Murder Inc.: Truth",
                year_start=2015,
                publisher="Marvel",
                issue_count=1,
            ),
            _make_search_result(
                provider_id="73896",
                title="The United States of Murder Inc.",
                year_start=2014,
                publisher="Marvel",
                issue_count=6,
            ),
        ]
        extractor = SourceMetadataExtractor()
        source_metadata = extractor.from_release_title(
            "The United States of Murder Inc. v01 - Truth (2015) (Digital).cbz"
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="The United States of Murder Inc",
            raw_year=2015,
            source_metadata=source_metadata,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 80837
        assert evaluation.match["cv_title"] == "The United States of Murder Inc.: Truth"
        assert evaluation.diagnostics["kind"] == "series_match"

    @pytest.mark.asyncio
    async def test_import_matching_uses_global_candidate_pool(self) -> None:
        """Import matching should use the same global series candidates as ComicVine search."""
        old_punisher = _make_search_result(
            provider_id="11164",
            title="The Punisher",
            year_start=2004,
            publisher="Marvel",
            issue_count=65,
        )
        current_punisher = _make_search_result(
            provider_id="170701",
            title="Punisher",
            year_start=2026,
            publisher="Marvel",
            issue_count=5,
        )
        provider = _GlobalSearchProviderDouble(
            page_results=[old_punisher],
            global_results=[old_punisher, current_punisher],
        )
        source_metadata = SourceMetadata(
            original_title="Punisher 004 (2026) (Digital) (Shan-Empire).cbz",
            series_name="Punisher",
            issue_number=4.0,
            year=2026,
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Punisher",
            raw_year=2026,
            source_metadata=source_metadata,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 170701
        assert evaluation.match["cv_title"] == "Punisher"
        assert provider.global_search_calls == 1
        assert provider.page_search_calls == 0

    @pytest.mark.asyncio
    async def test_import_matching_falls_back_when_global_search_misses_compact_title(
        self,
    ) -> None:
        """Global volume search can miss spaced alphanumeric titles like 2000AD -> 2000 AD."""
        best_of_2000ad = _make_search_result(
            provider_id="165612",
            title="Best of 2000AD",
            year_start=2025,
            publisher="Rebellion",
            issue_count=1,
        )
        main_2000ad = _make_search_result(
            provider_id="19752",
            title="2000 AD",
            year_start=1977,
            publisher="Rebellion",
            issue_count=2484,
        )
        provider = _GlobalSearchProviderDouble(
            page_results=[main_2000ad, best_of_2000ad],
            global_results=[best_of_2000ad],
        )
        source_metadata = SourceMetadata(
            original_title="2000AD prog 2482 (2026) (4320p) (juvecube).cbz",
            series_name="2000AD",
            issue_number=2482.0,
            year=2026,
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="2000AD",
            raw_year=2026,
            source_metadata=source_metadata,
            match_threshold=0.80,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 19752
        assert evaluation.match["cv_title"] == "2000 AD"
        assert evaluation.diagnostics["selected_candidate"]["match_type"] == "exact"
        assert evaluation.diagnostics["selected_candidate"]["score"] >= 0.80
        assert provider.global_search_calls == 1
        assert provider.page_search_calls == 1

    @pytest.mark.asyncio
    async def test_large_year_gap_rejected_when_candidate_issue_date_conflicts(self) -> None:
        """Issue coverage alone should not accept a candidate with a conflicting issue date."""
        old_punisher = _make_search_result(
            provider_id="11164",
            title="The Punisher",
            year_start=2004,
            publisher="Marvel",
            issue_count=65,
        )
        provider = _GlobalSearchProviderDouble(
            global_results=[old_punisher],
            issue_summaries={
                ("11164", 4.0): [
                    IssueSummary(
                        provider_id="11164-4",
                        issue_number=4.0,
                        title=None,
                        release_date="2004-06-01",
                        cover_url=None,
                        issue_type="issue",
                    )
                ]
            },
        )
        source_metadata = SourceMetadata(
            original_title="Punisher 004 (2026) (Digital) (Shan-Empire).cbz",
            series_name="Punisher",
            issue_number=4.0,
            year=2026,
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Punisher",
            raw_year=2026,
            source_metadata=source_metadata,
        )

        assert evaluation.match is None
        assert evaluation.diagnostics["kind"] == "series_no_match"
        assert evaluation.diagnostics["reason"] == "blocking_issue_year_mismatch"
        assert evaluation.diagnostics["selected_candidate"]["cv_id"] == 11164
        assert evaluation.diagnostics["selected_candidate"]["issue_year"] == 2004

    @pytest.mark.asyncio
    async def test_exact_title_year_beats_old_series_when_numbering_continues(self) -> None:
        """A same-year continuation issue should not lose to an older series with more issues."""
        current_heman = _make_search_result(
            provider_id="171916",
            title="He-Man and the Masters of the Universe",
            year_start=2026,
            publisher="Dark Horse Comics",
            issue_count=1,
        )
        old_heman = _make_search_result(
            provider_id="59980",
            title="He-Man and the Masters of the Universe",
            year_start=2013,
            publisher="DC Comics",
            issue_count=19,
        )
        provider = _GlobalSearchProviderDouble(
            global_results=[current_heman, old_heman],
            issue_summaries={
                ("59980", 5.0): [
                    IssueSummary(
                        provider_id="59980-5",
                        issue_number=5.0,
                        title="Once Upon A Time",
                        release_date="2013-10-01",
                        cover_url=None,
                        issue_type="issue",
                    )
                ]
            },
        )
        source_metadata = SourceMetadata(
            original_title=(
                "He-Man and the Masters of the Universe 005 (2026) "
                "(digital) (Son of Ultron-Empire).cbr"
            ),
            series_name="He-Man and the Masters of the Universe",
            issue_number=5.0,
            year=2026,
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="He-Man and the Masters of the Universe",
            raw_year=2026,
            source_metadata=source_metadata,
            match_threshold=0.70,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 171916
        assert evaluation.match["cv_match_method"] == "exact_title_year"
        assert evaluation.diagnostics["selected_candidate"]["score"] > 0.85

    @pytest.mark.asyncio
    async def test_one_shot_subtitle_release_matches_subtitle_series(self) -> None:
        """Release metadata like OS/Digital Rip should not make a base series beat a subtitle."""
        provider = _GlobalSearchProviderDouble(
            global_results=[
                _make_search_result(
                    provider_id="172008",
                    title="Murder Drones: Home",
                    year_start=2026,
                    publisher="Oni Press",
                    issue_count=1,
                ),
                _make_search_result(
                    provider_id="170666",
                    title="Murder Drones",
                    year_start=2026,
                    publisher="Oni Press",
                    issue_count=3,
                ),
            ],
        )
        extractor = SourceMetadataExtractor()
        source_metadata = extractor.from_release_title(
            "Murder Drones - Home 001 (OS) (2026) (Digital Rip) (Hourman-DCP).cbr"
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Murder Drones - Home One Shot",
            raw_year=source_metadata.year,
            source_metadata=source_metadata,
            match_threshold=0.70,
        )

        assert source_metadata.series_name == "Murder Drones - Home"
        assert source_metadata.issue_type == IssueType.ONE_SHOT
        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 172008
        assert evaluation.match["cv_title"] == "Murder Drones: Home"
        assert evaluation.match["cv_match_method"] == "exact_title_year"

    @pytest.mark.asyncio
    async def test_one_shot_source_matches_single_issue_special_volume(self) -> None:
        """ComicVine often names one-shot holiday volumes as Special instead of One-Shot."""
        provider = _GlobalSearchProviderDouble(
            global_results=[
                _make_search_result(
                    provider_id="170556",
                    title="Thundercats Valentine's Day Special",
                    year_start=2026,
                    publisher="Dynamite Entertainment",
                    issue_count=1,
                )
            ],
        )
        source_metadata = SourceMetadata(
            original_title=(
                "ThunderCats Valentine's Day Special 2026 001 (2026) "
                "(One Shot) (Dynamite Entertainment) (Digital-HD) (LeDuch).cbz"
            ),
            series_name="ThunderCats Valentine's Day Special 2026",
            issue_number=1.0,
            year=2026,
            issue_type=IssueType.ONE_SHOT,
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="ThunderCats Valentine's Day Special 2026",
            raw_year=2026,
            source_metadata=source_metadata,
            match_threshold=0.70,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 170556
        assert evaluation.match["cv_title"] == "Thundercats Valentine's Day Special"

    @pytest.mark.asyncio
    async def test_subtitle_match_with_large_year_gap_is_rejected(self) -> None:
        """Subtitle-style title matches should not override a large year mismatch."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(
                provider_id="144944",
                title="Agent Alpha",
                year_start=1997,
                publisher="Finix",
                issue_count=10,
            )
        ]

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Agent Alpha - Fucking Patriot",
            raw_year=2010,
            match_threshold=0.70,
        )

        assert evaluation.match is None
        assert evaluation.diagnostics["kind"] == "series_no_match"
        assert evaluation.diagnostics["reason"] == "blocking_year_mismatch"
        assert evaluation.diagnostics["selected_candidate"]["title"] == "Agent Alpha"
        assert evaluation.diagnostics["selected_candidate"]["year_delta"] == 13
        assert evaluation.diagnostics["selected_candidate"]["match_type"] == "starts_with"

    @pytest.mark.asyncio
    async def test_subtitle_year_gap_allowed_when_issue_number_is_covered(self) -> None:
        """Issue-year drift can be safe when an explicit issue number fits the series."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(
                provider_id="144944",
                title="Agent Alpha",
                year_start=1997,
                publisher="Finix",
                issue_count=18,
            )
        ]
        source_metadata = SourceMetadata(
            original_title="Agent Alpha 10 - Fucking Patriot (2010).cbr",
            series_name="Agent Alpha - Fucking Patriot",
            issue_number=10.0,
            year=2010,
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Agent Alpha - Fucking Patriot",
            raw_year=2010,
            source_metadata=source_metadata,
            match_threshold=0.70,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 144944
        assert evaluation.match["cv_title"] == "Agent Alpha"
        assert evaluation.diagnostics["selected_candidate"]["match_type"] == "starts_with"
        assert evaluation.diagnostics["selected_candidate"]["year_delta"] == 13

    @pytest.mark.asyncio
    async def test_comic_heroes_issue_filename_matches_magazine_series(self) -> None:
        """A filename with a literal -Issue suffix should still match the magazine series."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(
                provider_id="67345",
                title="Comic Heroes Presents: Superheroes",
                year_start=2013,
                publisher="Future Publishing",
                issue_count=1,
            ),
            _make_search_result(
                provider_id="53185",
                title="Comic Heroes Magazine",
                year_start=2010,
                publisher="Future Publishing",
                issue_count=32,
            ),
        ]
        source_metadata = SourceMetadata(
            original_title="Comic.Heroes-Issue.29.2016.cbz",
            series_name="Comic Heroes",
            issue_number=29.0,
            year=2016,
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Comic Heroes",
            raw_year=2016,
            source_metadata=source_metadata,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 53185
        assert evaluation.match["cv_title"] == "Comic Heroes Magazine"
        assert evaluation.diagnostics["selected_candidate"]["issue_count"] == 32

    @pytest.mark.asyncio
    async def test_collection_title_does_not_match_single_token_prefix_series(self) -> None:
        """A volume file should not match an unrelated article-stripped prefix title."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(
                provider_id="19356",
                title="The Dead",
                year_start=1998,
                publisher="Arrow",
                issue_count=3,
            )
        ]
        source_metadata = SourceMetadata(
            original_title="Dead Space V02 Salvage.pdf",
            series_name="Dead Space Salvage",
            volume="V02",
            issue_type=IssueType.VOLUME,
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Dead Space Salvage",
            raw_year=None,
            source_metadata=source_metadata,
        )

        assert evaluation.match is None
        assert evaluation.diagnostics["kind"] == "series_no_match"
        top_candidate = evaluation.diagnostics["top_candidates"][0]
        assert top_candidate["title"] == "The Dead"
        assert top_candidate["match_type"] == "none"

    @pytest.mark.asyncio
    async def test_exact_collection_title_beats_single_token_prefix_series(self) -> None:
        """The exact collection candidate should win over a tempting prefix false positive."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(
                provider_id="19356",
                title="The Dead",
                year_start=1998,
                publisher="Arrow",
                issue_count=3,
            ),
            _make_search_result(
                provider_id="38683",
                title="Dead Space Salvage",
                year_start=2010,
                publisher="IDW Publishing",
                issue_count=1,
            ),
        ]
        source_metadata = SourceMetadata(
            original_title="Dead Space V02 Salvage.pdf",
            series_name="Dead Space Salvage",
            volume="V02",
            issue_type=IssueType.VOLUME,
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Dead Space Salvage",
            raw_year=None,
            source_metadata=source_metadata,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 38683
        assert evaluation.match["cv_title"] == "Dead Space Salvage"

    @pytest.mark.asyncio
    async def test_complete_series_packaging_words_do_not_drive_bad_fuzzy_match(self) -> None:
        """Shared packaging words should not make an unrelated series look matchable."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(
                provider_id="160563",
                title="Batman Incorporated: The Complete Series",
                year_start=2024,
                publisher="DC Comics",
                issue_count=1,
            )
        ]
        source_metadata = SourceMetadata(
            original_title="Deadbox.The.Complete.Series.2024.pdf",
            series_name="Deadbox The Complete Series",
            year=2024,
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Deadbox The Complete Series",
            raw_year=2024,
            source_metadata=source_metadata,
        )

        assert evaluation.match is None
        assert evaluation.diagnostics["kind"] == "series_no_match"
        assert evaluation.diagnostics["top_candidates"][0]["title"] == (
            "Batman Incorporated: The Complete Series"
        )

    @pytest.mark.asyncio
    async def test_volume_base_alternate_can_rescue_subtitle_series_search(self) -> None:
        """A volume subtitle filename can search the base series after primary miss."""
        provider = AsyncMock()

        async def _search_side_effect(query: str, year: int | None = None, **_: object):
            if query == "Fearscape A Dark Interlude":
                return [
                    _make_search_result(
                        provider_id="132091",
                        title="A Dark Interlude",
                        year_start=2020,
                        publisher="Vault Comics",
                        issue_count=5,
                    ),
                    _make_search_result(
                        provider_id="119851",
                        title="Fearscape",
                        year_start=2019,
                        publisher="Vault Comics",
                        issue_count=2,
                    ),
                ]
            if query == "Fearscape":
                return [
                    _make_search_result(
                        provider_id="119851",
                        title="Fearscape",
                        year_start=2019,
                        publisher="Vault Comics",
                        issue_count=2,
                    )
                ]
            return []

        provider.search_series.side_effect = _search_side_effect
        source_metadata = SourceMetadata(
            original_title="Fearscape.Vol.02.A.Dark.Interlude.2023.pdf",
            series_name="Fearscape A Dark Interlude",
            issue_number=2.0,
            year=2023,
            volume="Vol 02",
            issue_type=IssueType.VOLUME,
            diagnostics={
                "alternate_release_candidates": [
                    {
                        "series_name": "Fearscape",
                        "year": 2023,
                        "file_name": "Fearscape.Vol.02.A.Dark.Interlude.2023.pdf",
                        "signal": "release_title",
                        "issue_title_hint": "A Dark Interlude",
                        "volume_issue_number": 2.0,
                    }
                ]
            },
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Fearscape A Dark Interlude",
            raw_year=2023,
            source_metadata=source_metadata,
            match_threshold=0.70,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 119851
        assert evaluation.match["cv_title"] == "Fearscape"
        assert evaluation.diagnostics["reason"] == "alternate_release_candidate"

    @pytest.mark.asyncio
    async def test_exact_title_with_year_tolerance_beats_subtitle_exact_year(self) -> None:
        """Issue-year drift should not make a subtitle one-shot beat the main series."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(
                provider_id="85082",
                title="Bitch Planet: Extraordinary Machine",
                year_start=2015,
                publisher="Image",
                issue_count=1,
                status="Ongoing",
            ),
            _make_search_result(
                provider_id="78698",
                title="Bitch Planet",
                year_start=2014,
                publisher="Image",
                issue_count=10,
            ),
        ]

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Bitch Planet",
            raw_year=2015,
            match_threshold=0.70,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 78698
        assert evaluation.match["cv_title"] == "Bitch Planet"
        assert evaluation.diagnostics["selected_candidate"]["match_type"] == "exact"

    @pytest.mark.asyncio
    async def test_exact_title_with_covered_issue_beats_collection_subtitle(self) -> None:
        """A covered issue/volume number should keep the main series above one-shot TPBs."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(
                provider_id="105813",
                title="Babyteeth #1: Halloween Edition",
                year_start=2017,
                publisher="Aftershock Comics",
                issue_count=1,
            ),
            _make_search_result(
                provider_id="115782",
                title="Babyteeth: Year One",
                year_start=2018,
                publisher="Aftershock Comics",
                issue_count=1,
            ),
            _make_search_result(
                provider_id="171891",
                title="Babyteeth",
                year_start=2017,
                publisher="Aftershock Comics",
                issue_count=4,
            ),
        ]
        source_metadata = SourceMetadata(
            original_title="Babyteeth v04 - Grave (2022) (Digital) (Kileko-Empire).cbz",
            series_name="Babyteeth",
            issue_number=4.0,
            year=2022,
            issue_type=IssueType.VOLUME,
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Babyteeth",
            raw_year=2022,
            source_metadata=source_metadata,
            match_threshold=0.70,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 171891
        assert evaluation.match["cv_title"] == "Babyteeth"
        assert evaluation.diagnostics["selected_candidate"]["match_type"] == "exact"
        assert evaluation.diagnostics["selected_candidate"]["year_delta"] == 5

    @pytest.mark.asyncio
    async def test_annual_alternate_release_candidate_beats_base_series_match(self) -> None:
        """An explicit annual release title should override the base-series match."""
        provider = AsyncMock()

        def _search_side_effect(query: str, year: int | None = None, **_: object):
            if query == "Absolute Wonder Woman":
                return [
                    _make_search_result(
                        provider_id="171171",
                        title="Absolute Wonder Woman",
                        year_start=2025,
                        publisher="Panini España",
                        issue_count=7,
                    ),
                    _make_search_result(
                        provider_id="170538",
                        title="Absolute Wonder Woman 2026 Annual",
                        year_start=2026,
                        publisher="DC Comics",
                        issue_count=1,
                    ),
                ]
            if query == "Absolute Wonder Woman 2026 Annual":
                return [
                    _make_search_result(
                        provider_id="170538",
                        title="Absolute Wonder Woman 2026 Annual",
                        year_start=2026,
                        publisher="DC Comics",
                        issue_count=1,
                    )
                ]
            return []

        provider.search_series.side_effect = _search_side_effect
        source_metadata = SourceMetadata(
            original_title="Absolute Wonder Woman 2026 Annual (2026) #001.cbz",
            series_name="Absolute Wonder Woman",
            issue_number=1.0,
            year=2026,
            issue_type=IssueType.ANNUAL,
            diagnostics={
                "alternate_release_candidates": [
                    {
                        "series_name": "Absolute Wonder Woman 2026 Annual",
                        "year": 2026,
                        "file_name": "Absolute Wonder Woman 2026 Annual (2026) #001.cbz",
                        "signal": MetadataSignal.RELEASE_TITLE.value,
                    }
                ]
            },
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Absolute Wonder Woman",
            raw_year=2026,
            source_metadata=source_metadata,
            match_threshold=0.70,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 170538
        assert evaluation.match["cv_title"] == "Absolute Wonder Woman 2026 Annual"
        assert evaluation.match["cv_match_method"] == "alternate_release_candidate"

    @pytest.mark.asyncio
    async def test_annual_alternate_release_candidate_wins_equal_base_score(self) -> None:
        """An exact annual candidate should beat an equally scored base-series match."""
        provider = AsyncMock()

        def _search_side_effect(query: str, year: int | None = None, **_: object):
            if query == "Immortal Thor":
                return [
                    _make_search_result(
                        provider_id="157225",
                        title="Immortal Thor",
                        year_start=2024,
                        publisher="Marvel",
                        issue_count=5,
                    ),
                    _make_search_result(
                        provider_id="158867",
                        title="The Immortal Thor Annual",
                        year_start=2024,
                        publisher="Marvel",
                        issue_count=1,
                    ),
                ]
            if query in {"Immortal Thor 2024 Annual", "Immortal Thor Annual"}:
                return [
                    _make_search_result(
                        provider_id="158867",
                        title="The Immortal Thor Annual",
                        year_start=2024,
                        publisher="Marvel",
                        issue_count=1,
                    )
                ]
            return []

        provider.search_series.side_effect = _search_side_effect
        source_metadata = SourceMetadata(
            original_title="Immortal Thor Annual 001 (2024) (Digital).cbz",
            series_name="Immortal Thor",
            issue_number=1.0,
            year=2024,
            issue_type=IssueType.ANNUAL,
            diagnostics={
                "alternate_release_candidates": [
                    {
                        "series_name": "Immortal Thor 2024 Annual",
                        "year": 2024,
                        "file_name": "Immortal Thor Annual 001 (2024) (Digital).cbz",
                        "signal": MetadataSignal.RELEASE_TITLE.value,
                        "issue_type": IssueType.ANNUAL.value,
                        "issue_type_qualified": True,
                    },
                    {
                        "series_name": "Immortal Thor Annual",
                        "year": 2024,
                        "file_name": "Immortal Thor Annual 001 (2024) (Digital).cbz",
                        "signal": MetadataSignal.RELEASE_TITLE.value,
                        "issue_type": IssueType.ANNUAL.value,
                        "issue_type_qualified": True,
                    },
                ]
            },
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Immortal Thor",
            raw_year=2024,
            source_metadata=source_metadata,
            match_threshold=0.70,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 158867
        assert evaluation.match["cv_title"] == "The Immortal Thor Annual"
        assert evaluation.match["cv_match_method"] == "alternate_release_candidate"

    @pytest.mark.asyncio
    async def test_annual_alternate_release_candidate_cannot_fall_back_to_base_series(
        self,
    ) -> None:
        """A type-qualified annual search must not accept a plain base-series result."""
        provider = AsyncMock()

        def _search_side_effect(query: str, year: int | None = None, **_: object):
            if query in {
                "Immortal Thor Annual",
                "Immortal Thor 2024 Annual",
                "Immortal Thor",
            }:
                return [
                    _make_search_result(
                        provider_id="157225",
                        title="Immortal Thor",
                        year_start=2024,
                        publisher="Marvel",
                        issue_count=5,
                    )
                ]
            return []

        provider.search_series.side_effect = _search_side_effect
        source_metadata = SourceMetadata(
            original_title="Immortal Thor Annual 001 (2024) (Digital).cbz",
            series_name="Immortal Thor Annual",
            issue_number=1.0,
            year=2024,
            issue_type=IssueType.ANNUAL,
            diagnostics={
                "alternate_release_candidates": [
                    {
                        "series_name": "Immortal Thor 2024 Annual",
                        "year": 2024,
                        "file_name": "Immortal Thor Annual 001 (2024) (Digital).cbz",
                        "signal": MetadataSignal.RELEASE_TITLE.value,
                        "issue_type": IssueType.ANNUAL.value,
                        "issue_type_qualified": True,
                    },
                    {
                        "series_name": "Immortal Thor",
                        "year": 2024,
                        "file_name": "Immortal Thor Annual 001 (2024) (Digital).cbz",
                        "signal": MetadataSignal.RELEASE_TITLE.value,
                    },
                ]
            },
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Immortal Thor Annual",
            raw_year=2024,
            source_metadata=source_metadata,
            match_threshold=0.70,
        )

        assert evaluation.match is None
        assert evaluation.diagnostics["reason"] == "issue_like_type_mismatch"
        assert evaluation.diagnostics["selected_candidate"]["cv_id"] == 157225

    @pytest.mark.asyncio
    async def test_type_qualified_alternate_release_candidate_beats_fuzzy_base_match(self) -> None:
        """Collection-type titles should override ambiguous fuzzy base-series matches."""
        provider = AsyncMock()

        def _search_side_effect(query: str, year: int | None = None, **_: object):
            if query == "Black Science":
                return [
                    _make_search_result(
                        provider_id="150153",
                        title="Black Science Compendium",
                        year_start=2023,
                        publisher="Image",
                        issue_count=1,
                    ),
                    _make_search_result(
                        provider_id="137899",
                        title="Black Science Premiere: A Brief Moment of Clarity",
                        year_start=2020,
                        publisher="Image",
                        issue_count=1,
                    ),
                ]
            if query == "Black Science Compendium":
                return [
                    _make_search_result(
                        provider_id="150153",
                        title="Black Science Compendium",
                        year_start=2023,
                        publisher="Image",
                        issue_count=1,
                    )
                ]
            return []

        provider.search_series.side_effect = _search_side_effect
        source_metadata = SourceMetadata(
            original_title="Black Science Compendium (2023) #001.cbz",
            series_name="Black Science",
            issue_number=1.0,
            year=2023,
            issue_type=IssueType.COMPENDIUM,
            diagnostics={
                "alternate_release_candidates": [
                    {
                        "series_name": "Black Science Compendium",
                        "year": 2023,
                        "file_name": "Black Science Compendium (2023) #001.cbz",
                        "signal": MetadataSignal.RELEASE_TITLE.value,
                        "issue_type": IssueType.COMPENDIUM.value,
                        "issue_type_qualified": True,
                    }
                ]
            },
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Black Science",
            raw_year=2023,
            source_metadata=source_metadata,
            match_threshold=0.70,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 150153
        assert evaluation.match["cv_title"] == "Black Science Compendium"
        assert evaluation.match["cv_match_method"] == "alternate_release_candidate"

    @pytest.mark.asyncio
    async def test_comicinfo_vs_filename_series_mismatch_becomes_series_conflict(self) -> None:
        """Conflicting strong series identities should require review instead of a clean match."""
        provider = AsyncMock()

        def _search_side_effect(query: str, year: int | None):
            if query == "Chicken Devils":
                return [
                    _make_search_result(
                        provider_id="145525",
                        title="Chicken Devils",
                        year_start=2022,
                        publisher="AfterShock Comics",
                        issue_count=4,
                    )
                ]
            if query == "Chicken Devil":
                return [
                    _make_search_result(
                        provider_id="145525",
                        title="Chicken Devils",
                        year_start=2022,
                        publisher="AfterShock Comics",
                        issue_count=4,
                    ),
                    _make_search_result(
                        provider_id="139451",
                        title="Chicken Devil",
                        year_start=2021,
                        publisher="AfterShock Comics",
                        issue_count=4,
                    ),
                ]
            return []

        provider.search_series.side_effect = _search_side_effect

        extractor = SourceMetadataExtractor()
        source_metadata = extractor.from_release_title("Chicken Devil 004 (2022).cb7").model_copy(
            update={
                "series_name": "Chicken Devils",
                "year": 2022,
                "comicvine_issue_id": 996957,
                "signals": {
                    "series_name": MetadataSignal.COMICINFO,
                    "comicvine_issue_id": MetadataSignal.COMICINFO,
                },
                "diagnostics": {
                    "alternate_release_candidates": [
                        {
                            "series_name": "Chicken Devil",
                            "year": 2022,
                            "file_name": "Chicken Devil 004 (2022).cb7",
                            "signal": MetadataSignal.RELEASE_TITLE.value,
                        }
                    ]
                },
            }
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Chicken Devils",
            raw_year=2022,
            source_metadata=source_metadata,
        )

        assert evaluation.match is None
        assert evaluation.diagnostics["kind"] == "series_conflict"
        assert evaluation.diagnostics["reason"] == "metadata_signal_conflict"
        assert evaluation.diagnostics["selected_candidate"]["title"] == "Chicken Devils"
        assert evaluation.diagnostics["competing_candidate"]["title"] == "Chicken Devil"
        assert evaluation.diagnostics["selected_signal"] == MetadataSignal.COMICINFO.value
        assert evaluation.diagnostics["competing_signal"] == MetadataSignal.RELEASE_TITLE.value

    @pytest.mark.asyncio
    async def test_exact_comicinfo_series_beats_release_title_base_series_expansion(self) -> None:
        """A story subtitle should not let a shorter base series beat exact ComicInfo."""
        provider = AsyncMock()

        def _search_side_effect(query: str, year: int | None):
            if query == "Alien by Shalvey & Broccardo":
                return [
                    _make_search_result(
                        provider_id="154673",
                        title="Alien",
                        year_start=2024,
                        publisher="Marvel",
                        issue_count=4,
                    ),
                    _make_search_result(
                        provider_id="154680",
                        title="Alien By Shalvey & Broccardo",
                        year_start=2023,
                        publisher="Marvel",
                        issue_count=2,
                    ),
                ]
            if query == "Alien by Shalvey & Broccardo - Thaw":
                return [
                    _make_search_result(
                        provider_id="154673",
                        title="Alien",
                        year_start=2024,
                        publisher="Marvel",
                        issue_count=4,
                    ),
                    _make_search_result(
                        provider_id="154680",
                        title="Alien By Shalvey & Broccardo",
                        year_start=2023,
                        publisher="Marvel",
                        issue_count=2,
                    ),
                ]
            return []

        provider.search_series.side_effect = _search_side_effect
        source_metadata = SourceMetadata(
            original_title=(
                "Alien by Shalvey & Broccardo v01 - Thaw (2024) (Digital) (dekabro-Empire).cbz"
            ),
            series_name="Alien by Shalvey & Broccardo",
            year=2024,
            issue_type=IssueType.VOLUME,
            signals={
                "series_name": MetadataSignal.COMICINFO,
                "publisher": MetadataSignal.COMICINFO,
            },
            diagnostics={
                "alternate_release_candidates": [
                    {
                        "series_name": "Alien by Shalvey & Broccardo - Thaw",
                        "year": 2024,
                        "file_name": (
                            "Alien by Shalvey & Broccardo v01 - Thaw (2024) "
                            "(Digital) (dekabro-Empire).cbz"
                        ),
                        "signal": MetadataSignal.RELEASE_TITLE.value,
                    }
                ]
            },
        )

        evaluation = await evaluate_comicvine_match(
            provider=provider,
            raw_name="Alien by Shalvey & Broccardo",
            raw_year=2024,
            source_metadata=source_metadata,
            match_threshold=0.70,
        )

        assert evaluation.match is not None
        assert evaluation.match["cv_id"] == 154680
        assert evaluation.match["cv_title"] == "Alien By Shalvey & Broccardo"
        assert evaluation.diagnostics["reason"] == "matched"
        assert evaluation.diagnostics["selected_candidate"]["match_type"] == "exact"

    @pytest.mark.asyncio
    async def test_cv_id_lookup_failure_falls_back_to_search(self) -> None:
        """If direct CV ID lookup fails, fall back to search."""
        provider = AsyncMock()
        provider.get_series.side_effect = Exception("CV API error")
        provider.search_series.return_value = [_make_search_result()]

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
            mylar3_cv_id=97508,
        )

        assert result is not None
        assert result["cv_id"] == 97508

    @pytest.mark.asyncio
    async def test_custom_threshold_accepts_lower_score(self) -> None:
        """A custom lower threshold accepts results that default would reject."""
        provider = AsyncMock()
        # "Batman" vs "Batman" with very different years scores ~0.75
        # (below default 0.80, above custom 0.70)
        provider.search_series.return_value = [
            _make_search_result(title="Batman", year_start=1940, provider_id="111"),
        ]

        # Default threshold (0.80) rejects 0.75
        result_default = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
        )
        assert result_default is None

        # Lower threshold (0.70) accepts 0.75
        result_custom = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
            match_threshold=0.70,
        )
        assert result_custom is not None
        assert result_custom["cv_id"] == 111

    @pytest.mark.asyncio
    async def test_strict_threshold_rejects_fuzzy(self) -> None:
        """A strict threshold (0.99) rejects fuzzy matches."""
        provider = AsyncMock()
        provider.search_series.return_value = [
            _make_search_result(title="Batman", year_start=2017),
        ]

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
            match_threshold=0.99,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_mylar3_cv_id_takes_priority_over_comicinfo(self) -> None:
        """mylar3_cv_id is preferred over comicinfo_cv_id."""
        provider = AsyncMock()
        provider.get_series.return_value = _make_series_metadata(provider_id="111")

        result = await match_to_comicvine(
            provider=provider,
            raw_name="Batman",
            raw_year=2016,
            mylar3_cv_id=111,
            comicinfo_cv_id=222,
        )

        assert result is not None
        assert result["cv_id"] == 111
        assert result["cv_match_method"] == "mylar3_cv_id"

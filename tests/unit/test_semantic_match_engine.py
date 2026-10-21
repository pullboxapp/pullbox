"""Unit tests for the shared semantic match engine."""

from __future__ import annotations

import pytest

from pullbox.core.source_metadata import MetadataSignal, SourceMetadataExtractor
from pullbox.models.issue import IssueType
from pullbox.models.library import MatchConfidence
from pullbox.providers.base import SeriesSearchResult
from pullbox.services.semantic_matching import (
    ImportPolicy,
    SearchPolicy,
    SemanticMatchConfig,
    SemanticMatchEngine,
)


class TestSemanticMatchEngine:
    """Search and import should share semantics with different strictness."""

    def setup_method(self) -> None:
        self.extractor = SourceMetadataExtractor()
        self.config = SemanticMatchConfig()

    @pytest.mark.parametrize("policy", [SearchPolicy(), ImportPolicy()])
    @pytest.mark.parametrize("label", ["B Story", "[B Story]", "(B Story)"])
    def test_explicit_story_extract_is_not_a_complete_issue(self, policy, label) -> None:
        metadata = self.extractor.from_release_title(
            f"Marvel Comics Presents 158 {label} (1994) (digital) (Marika-Empire)"
        )
        decision = SemanticMatchEngine(self.config, policy).match_against_issue(
            metadata=metadata,
            wanted_series="Marvel Comics Presents",
            wanted_issue=158,
            wanted_year=1988,
            wanted_issue_cv_id=39372,
        )
        assert not decision.is_match
        assert decision.match_method == "partial_issue"
        assert "complete issue" in decision.rejection_reason

    @pytest.mark.parametrize("number", ["13B", "50-X", "50-O"])
    def test_story_guard_keeps_lettered_issue_designations(self, number) -> None:
        metadata = self.extractor.from_release_title(f"Example {number} (2024)")
        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Example",
            wanted_issue=13 if number == "13B" else 50,
            wanted_issue_number_text=number,
            wanted_year=2024,
        )
        assert decision.is_match

    def test_story_in_series_title_is_not_an_extract_marker(self) -> None:
        metadata = self.extractor.from_release_title("Toy Story 158 (1994)")
        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Toy Story",
            wanted_issue=158,
            wanted_year=1994,
        )
        assert decision.is_match

    def test_collection_release_matches_collection_target(self) -> None:
        metadata = self.extractor.from_release_title("Batman TPB Vol 5 [2024] [Digital]")
        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Batman",
            wanted_issue=5.0,
            wanted_year=2024,
            wanted_issue_type=IssueType.TPB,
        )

        assert decision.is_match is True
        assert decision.match_diagnostics["type_mode"] == "exact"

    def test_volume_subtitle_issue_match_requires_issue_title_corroboration(self) -> None:
        metadata = self.extractor.from_release_title("Fearscape.Vol.02.A.Dark.Interlude.2023.pdf")
        metadata = metadata.model_copy(
            update={
                "series_name": "Fearscape",
                "issue_number": 2.0,
                "diagnostics": {
                    "volume_subtitle_hint": {
                        "base_series": "Fearscape",
                        "subtitle": "A Dark Interlude",
                        "issue_number": 2.0,
                    }
                },
            }
        )

        decision = SemanticMatchEngine(self.config, ImportPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Fearscape",
            wanted_issue=2.0,
            wanted_year=2019,
            wanted_issue_type=IssueType.VOLUME,
            wanted_issue_cv_id=1030016,
            wanted_issue_title="Vol. 2: A Dark Interlude",
        )

        assert decision.is_match is True
        assert decision.match_method == "issue_number"

    def test_volume_subtitle_issue_match_rejects_wrong_issue_title(self) -> None:
        metadata = self.extractor.from_release_title("Dead Space V02 Salvage.pdf")
        metadata = metadata.model_copy(
            update={
                "series_name": "Dead Space",
                "issue_number": 2.0,
                "diagnostics": {
                    "volume_subtitle_hint": {
                        "base_series": "Dead Space",
                        "subtitle": "Salvage",
                        "issue_number": 2.0,
                    }
                },
            }
        )

        decision = SemanticMatchEngine(self.config, ImportPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Dead Space",
            wanted_issue=2.0,
            wanted_year=2008,
            wanted_issue_type=IssueType.VOLUME,
            wanted_issue_cv_id=123456,
            wanted_issue_title="Part Two",
        )

        assert decision.is_match is False
        assert decision.match_method == "issue_title_mismatch"

    def test_volume_subtitle_issue_match_accepts_series_title_corroboration(self) -> None:
        metadata = self.extractor.from_release_title(
            "The United States of Murder Inc. v01 - Truth (2015).cbz"
        )

        decision = SemanticMatchEngine(self.config, ImportPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="The United States of Murder Inc.: Truth",
            wanted_issue=1.0,
            wanted_year=2015,
            wanted_issue_type=IssueType.VOLUME,
            wanted_issue_cv_id=483095,
            wanted_issue_title="Volume 1",
        )

        assert decision.is_match is True
        assert decision.match_method == "issue_number"

    def test_import_policy_rejects_same_collection_tagged_issue_candidate(self) -> None:
        metadata = self.extractor.from_release_title("Batman TPB Vol 5 [2024] [Digital]")
        decision = SemanticMatchEngine(self.config, ImportPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Batman",
            wanted_issue=5.0,
            wanted_year=2024,
            wanted_issue_type=IssueType.ISSUE,
        )

        assert decision.is_match is False
        assert "Issue type mismatch" in (decision.rejection_reason or "")

    def test_type_mismatch_still_reports_exact_title_similarity(self) -> None:
        metadata = self.extractor.from_release_title(
            "Batman-Officer Down v01 [2001] [hybrid] [Marika-Empire]"
        )
        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Batman: Officer Down",
            wanted_issue=1.0,
            wanted_year=2001,
            wanted_issue_type=IssueType.ONE_SHOT,
        )

        assert decision.is_match is False
        assert decision.match_method == "type_mismatch"
        assert decision.match_diagnostics["series_similarity"] == 1.0
        assert decision.match_diagnostics["match_type"] == "exact"

    def test_search_policy_accepts_collection_subtitle_without_number(self) -> None:
        metadata = self.extractor.from_release_title(
            "Immortal Thor All Weather Turns To Storm [2024] [Digital]"
        )
        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Immortal Thor",
            wanted_issue=1.0,
            wanted_year=2024,
            wanted_issue_type=IssueType.TPB,
        )

        assert decision.is_match is True
        assert decision.confidence == MatchConfidence.LOW
        assert decision.match_diagnostics["issue_check_skipped"] is True

    @pytest.mark.regression
    def test_search_policy_accepts_exact_year_subtitle_for_single_word_graphic_novel(
        self,
    ) -> None:
        metadata = self.extractor.from_release_title(
            "Taproot - A Story about a Gardener and a Ghost (2017) (digital) (Mr Norrell-Empire)"
        )

        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Taproot",
            wanted_issue=1.0,
            wanted_year=2017,
            wanted_issue_type=IssueType.GN,
            wanted_issue_title="GN",
            wanted_series_issue_count=1,
        )

        assert decision.is_match is True
        assert decision.match_method == "collection_series_match"
        assert decision.match_diagnostics["single_word_collection_prefix"] is True

    @pytest.mark.regression
    @pytest.mark.parametrize(
        ("wanted_year", "wanted_series_issue_count"),
        [(2018, 1), (2017, 2)],
    )
    def test_search_policy_rejects_ambiguous_single_word_collection_prefix(
        self,
        wanted_year: int,
        wanted_series_issue_count: int,
    ) -> None:
        metadata = self.extractor.from_release_title(
            "Taproot - A Story about a Gardener and a Ghost (2017) (digital) (Mr Norrell-Empire)"
        )

        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Taproot",
            wanted_issue=1.0,
            wanted_year=wanted_year,
            wanted_issue_type=IssueType.GN,
            wanted_issue_title="GN",
            wanted_series_issue_count=wanted_series_issue_count,
        )

        assert decision.is_match is False
        assert decision.match_method == "series_mismatch"

    @pytest.mark.regression
    def test_explicit_single_issue_tpb_without_number_is_high_confidence(self) -> None:
        metadata = self.extractor.from_release_title(
            "John Carpenter\u2019s Tales of Science Fiction \u2013 THE ENVOY (TPB) (2023)"
        )

        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="John Carpenter's Tales of Science Fiction: The Envoy",
            wanted_issue=1.0,
            wanted_year=2023,
            wanted_issue_type=IssueType.TPB,
            wanted_issue_title="TPB",
            wanted_series_issue_count=1,
        )

        assert decision.is_match is True
        assert decision.confidence == MatchConfidence.HIGH
        assert decision.match_method == "explicit_single_collection"

    @pytest.mark.regression
    def test_numbered_standard_issue_does_not_satisfy_single_issue_tpb(self) -> None:
        metadata = self.extractor.from_release_title(
            "John Carpenter's Tales of Science Fiction - The Envoy 01 (of 03) "
            "(2023) (digital) (Leifman-Empire).cbz"
        )

        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="John Carpenter's Tales of Science Fiction: The Envoy",
            wanted_issue=1.0,
            wanted_year=2023,
            wanted_issue_type=IssueType.TPB,
            wanted_issue_title="TPB",
            wanted_series_issue_count=1,
        )

        assert decision.is_match is False
        assert decision.match_method == "numbered_issue_collection_mismatch"
        assert "standard issue" in (decision.rejection_reason or "").lower()

    @pytest.mark.regression
    def test_search_policy_matches_single_issue_collection_by_volume_subtitle(self) -> None:
        metadata = self.extractor.from_release_title(
            "Clean Room v02 - Exile (2016) (digital-Empire).cbr"
        )

        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Clean Room: Exile",
            wanted_issue=1.0,
            wanted_year=2016,
            wanted_issue_type=IssueType.VOLUME,
            wanted_issue_title="Volume 2",
            wanted_series_issue_count=1,
        )

        assert decision.is_match is True
        assert decision.match_method == "single_issue_collection_volume_subtitle"

    @pytest.mark.regression
    def test_search_policy_rejects_sibling_single_issue_collection_subtitle(self) -> None:
        metadata = self.extractor.from_release_title(
            "Clean Room v01 - Immaculate Conception (2016) (Digital) (Zone-Empire).cbr"
        )

        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Clean Room: Exile",
            wanted_issue=1.0,
            wanted_year=2016,
            wanted_issue_type=IssueType.VOLUME,
            wanted_issue_title="Volume 2",
            wanted_series_issue_count=1,
        )

        assert decision.is_match is False
        assert decision.match_method == "issue_title_mismatch"

    def test_search_policy_rejects_part_title_as_implicit_issue_one(self) -> None:
        metadata = self.extractor.from_release_title(
            "Absolute Flash: The Trials of the Flash: Iron Heights, "
            "Part 1 by Jeff Lemire [ENG / CBZ]"
        )
        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Absolute Flash",
            wanted_issue=1.0,
            wanted_year=2025,
            wanted_issue_type=IssueType.ISSUE,
        )

        assert decision.is_match is False
        assert "Issue mismatch" in (decision.rejection_reason or "")

    def test_import_policy_accepts_special_without_explicit_issue_one(self) -> None:
        metadata = self.extractor.from_release_title("Flash Gordon - The 1995 Special (2026).cbr")
        decision = SemanticMatchEngine(self.config, ImportPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Flash Gordon - The 1995 Special",
            wanted_issue=1.0,
            wanted_year=2026,
            wanted_issue_type=IssueType.SPECIAL,
        )

        assert decision.is_match is True
        assert decision.match_method == "implicit_issue_one"

    def test_import_policy_accepts_annual_without_explicit_issue_one(self) -> None:
        metadata = self.extractor.from_release_title(
            "Street Sharks - Annual 2026 (2026) (Digital) (Pyrate-DCP).cbz"
        )
        metadata = metadata.model_copy(update={"series_name": "Street Sharks: Annual 2026"})

        decision = SemanticMatchEngine(self.config, ImportPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Street Sharks: Annual 2026",
            wanted_issue=1.0,
            wanted_year=2026,
            wanted_issue_type=IssueType.ANNUAL,
        )

        assert decision.is_match is True
        assert decision.match_method == "implicit_issue_one"

    def test_wrong_issue_number_never_matches(self) -> None:
        metadata = self.extractor.from_release_title("Batman 050 [2024] [Digital]")
        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Batman",
            wanted_issue=5.0,
            wanted_year=2024,
            wanted_issue_type=IssueType.ISSUE,
        )

        assert decision.is_match is False
        assert "Issue mismatch" in (decision.rejection_reason or "")

    def test_explicit_provider_issue_id_beats_parsed_issue_number(self) -> None:
        metadata = self.extractor.from_release_title("Batman 045 [2024] [Digital]")
        metadata = metadata.model_copy(
            update={
                "comicvine_issue_id": 987654,
                "signals": {
                    **metadata.signals,
                    "comicvine_issue_id": "comicinfo",
                },
            }
        )
        decision = SemanticMatchEngine(self.config, ImportPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Batman",
            wanted_issue=12.0,
            wanted_year=2024,
            wanted_issue_type=IssueType.ISSUE,
            wanted_issue_cv_id=987654,
        )

        assert decision.is_match is True
        assert decision.match_method == "comicvine_issue_id"

    def test_strong_comicvine_issue_id_mismatch_rejects_issue_number_fallback(self) -> None:
        metadata = self.extractor.from_release_title("Absolute Martian Manhunter (2025) Vol 01.cbz")
        metadata = metadata.model_copy(
            update={
                "series_name": "Absolute Martian Manhunter",
                "year": 2025,
                "comicvine_issue_id": 1144216,
                "signals": {
                    **metadata.signals,
                    "comicvine_issue_id": MetadataSignal.COMICINFO,
                },
            }
        )

        decision = SemanticMatchEngine(self.config, ImportPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Absolute Martian Manhunter",
            wanted_issue=1.0,
            wanted_year=2025,
            wanted_issue_type=IssueType.VOLUME,
            wanted_issue_cv_id=1100110,
        )

        assert decision.is_match is False
        assert decision.match_method == "comicvine_issue_id_mismatch"
        assert "ComicVine issue ID mismatch" in (decision.rejection_reason or "")

    def test_collection_family_match_lowers_confidence(self) -> None:
        metadata = self.extractor.from_release_title("Cairo Hardcover (2007) (Digital)")
        decision = SemanticMatchEngine(self.config, SearchPolicy()).match_against_issue(
            metadata=metadata,
            wanted_series="Cairo",
            wanted_issue=1.0,
            wanted_year=2007,
            wanted_issue_type=IssueType.DELUXE,
        )

        assert decision.is_match is True
        assert decision.confidence == MatchConfidence.LOW
        assert decision.lowers_confidence is True

    def test_collection_source_prefers_single_ended_series_shape(self) -> None:
        metadata = self.extractor.from_release_title("Absolute Martian Manhunter TPB Vol 1 (2025)")
        engine = SemanticMatchEngine(self.config, ImportPolicy())
        collection_candidate = SeriesSearchResult(
            provider_id="168590",
            title="Absolute Martian Manhunter",
            year_start=2025,
            publisher="DC Comics",
            issue_count=1,
            status="Ended",
            cover_url=None,
            description=None,
        )
        serial_candidate = SeriesSearchResult(
            provider_id="162966",
            title="Absolute Martian Manhunter",
            year_start=2025,
            publisher="DC Comics",
            issue_count=10,
            status="Continuing",
            cover_url=None,
            description=None,
        )

        collection_score = engine.score_series_search_result(
            metadata=metadata,
            candidate=collection_candidate,
        )
        serial_score = engine.score_series_search_result(
            metadata=metadata,
            candidate=serial_candidate,
        )

        assert (
            collection_score.diagnostics["shape_adjustment"]
            > serial_score.diagnostics["shape_adjustment"]
        )

    def test_standard_source_penalizes_single_release_series_shape(self) -> None:
        metadata = self.extractor.from_release_title("Absolute Martian Manhunter 002 (2025)")
        engine = SemanticMatchEngine(self.config, ImportPolicy())
        collection_candidate = SeriesSearchResult(
            provider_id="168590",
            title="Absolute Martian Manhunter",
            year_start=2025,
            publisher="DC Comics",
            issue_count=1,
            status="Ended",
            cover_url=None,
            description=None,
        )
        serial_candidate = SeriesSearchResult(
            provider_id="162966",
            title="Absolute Martian Manhunter",
            year_start=2025,
            publisher="DC Comics",
            issue_count=10,
            status="Continuing",
            cover_url=None,
            description=None,
        )

        collection_score = engine.score_series_search_result(
            metadata=metadata,
            candidate=collection_candidate,
        )
        serial_score = engine.score_series_search_result(
            metadata=metadata,
            candidate=serial_candidate,
        )

        assert serial_score.score > collection_score.score

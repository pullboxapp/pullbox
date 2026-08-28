"""Source-neutral winner selection for indexer and direct results."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models.issue import IssueType
from pullbox.providers.base import ReleaseResult
from pullbox.providers.direct.contract import DirectCandidate, DirectParsedCandidate
from pullbox.services.airdcpp_search_types import (
    DcMetrics,
    DcRoute,
    DcSearchOutcome,
    DcValidatedCandidate,
)
from pullbox.services.direct_search_coordinator import (
    DirectSearchOutcome,
    DirectSearchProvider,
    DirectValidatedCandidate,
)
from pullbox.services.release_validator import ReleaseValidator
from pullbox.services.search_source_selection import rank_search_sources, select_search_source
from pullbox.services.search_targets import IssueSearchOutcome, IssueSearchTarget


def _release(
    title: str,
    source: str,
    *,
    size: int = 100_000_000,
    is_torrent: bool = True,
    ranking_priority: int = 25,
) -> ReleaseResult:
    return ReleaseResult(
        title=title,
        indexer_name=source,
        download_url=f"https://example.test/{source}/{title}",
        size_bytes=size,
        age_days=1,
        seeders=10,
        leechers=1,
        grabs=None,
        is_torrent=is_torrent,
        category="7030",
        published_at=None,
        ranking_priority=ranking_priority,
    )


def _validation(release: ReleaseResult):  # type: ignore[no-untyped-def]
    matched, _ = ReleaseValidator().validate_all_results(
        [release],
        wanted_series="Batman",
        wanted_issue=1,
        wanted_year=2016,
    )
    return matched[0]


def _direct_result(
    release: ReleaseResult,
    *,
    provider_identity: str = "pullbox.getcomics",
    provider_priority: int | None = None,
) -> DirectValidatedCandidate:
    resolved_priority = release.ranking_priority if provider_priority is None else provider_priority
    display_name = "Anna's Archive" if provider_identity == "pullbox.annas_archive" else "GetComics"
    release = replace(
        release,
        indexer_name=display_name,
        download_url=f"direct://candidate/{provider_identity}",
        is_torrent=False,
        ranking_priority=resolved_priority,
    )
    return DirectValidatedCandidate(
        provider=DirectSearchProvider(
            provider_config_id=resolved_priority,
            provider_identity=provider_identity,
            display_name=display_name,
            endpoint="http://provider:8780",
            bearer_token="provider-token-with-enough-length",
            provider_priority=resolved_priority,
        ),
        candidate=DirectCandidate(
            provider_candidate_id=f"candidate-{provider_identity}",
            source_reference="https://getcomics.org/post",
            display_title=release.title,
            raw_title=release.title,
            parsed=DirectParsedCandidate(
                series_title="Batman",
                issue_numbers=["1"],
                year=2016,
                format="cbz",
                quality="digital",
            ),
            provider_confidence=0.98,
        ),
        release=release,
        validation=_validation(release),
    )


def _dc_result(
    release: ReleaseResult,
    *,
    config_id: int = 7,
    free_slots: int = 1,
    source_count: int = 2,
) -> DcValidatedCandidate:
    release = replace(
        release,
        indexer_name=f"Air {config_id}",
        download_url=f"airdcpp://client/{config_id}/opaque",
        protocol=AcquisitionProtocol.DC,
        ranking_priority=config_id,
    )
    return DcValidatedCandidate(
        release=release,
        validation=_validation(release),
        route=DcRoute(
            client_config_id=config_id,
            client_identity=f"airdcpp:{config_id}",
            search_instance_id=44,
            grouped_result_id=f"result-{config_id}",
            result_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            tth="CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y",
            size_bytes=release.size_bytes or 1,
        ),
        metrics=DcMetrics(
            source_count=source_count,
            free_slots=free_slots,
            total_slots=max(2, free_slots),
            aggregate_connection_bytes_per_second=1_000_000,
        ),
    )


def _outcome(
    indexer_release: ReleaseResult,
    direct_result: DirectValidatedCandidate | None,
) -> IssueSearchOutcome:
    validation = _validation(indexer_release)
    target = IssueSearchTarget(
        issue_id=1,
        series_id=2,
        series_title="Batman",
        issue_number=1,
        issue_type=IssueType.ISSUE,
        series_year=2016,
    )
    return IssueSearchOutcome(
        target=target,
        mode="fast",
        query_count=1,
        raw_results=[indexer_release],
        filtered_results=[indexer_release],
        matched=[validation],
        rejected=[],
        best_release=indexer_release,
        best_validation=validation,
        search_details={},
        elapsed_ms=1,
        direct_outcome=(
            DirectSearchOutcome(
                matched=(direct_result,),
                rejected=(),
                failures=(),
                providers_searched=1,
                elapsed_ms=1,
            )
            if direct_result is not None
            else None
        ),
    )


def _indexer_only_outcome(*releases: ReleaseResult) -> IssueSearchOutcome:
    outcome = _outcome(releases[0], None)
    validations = [_validation(release) for release in releases]
    return replace(
        outcome,
        raw_results=list(releases),
        filtered_results=list(releases),
        matched=validations,
        best_release=None,
        best_validation=None,
    )


def test_direct_candidate_can_win_using_existing_search_score() -> None:
    indexer = _release("Batman 001.cbz", "Indexer", size=25_000_000)
    direct = _direct_result(_release("Batman 001 (2016) (Digital).cbz", "GetComics"))

    selected = select_search_source(_outcome(indexer, direct), {})

    assert selected is not None
    assert selected.source_kind == "direct"
    assert selected.direct_result is direct
    assert selected.validation is direct.validation


def test_equal_score_preserves_existing_indexer_precedence() -> None:
    indexer = _release("Batman 001 (2016) (Digital).cbz", "Indexer")
    direct = _direct_result(_release("Batman 001 (2016) (Digital).cbz", "GetComics"))

    selected = select_search_source(_outcome(indexer, direct), {})

    assert selected is not None
    assert selected.source_kind == "indexer"
    assert selected.release is indexer
    assert selected.direct_result is None


def test_equal_score_respects_direct_first_source_priority() -> None:
    indexer = _release("Batman 001 (2016) (Digital).cbz", "Indexer")
    direct = _direct_result(_release("Batman 001 (2016) (Digital).cbz", "GetComics"))

    selected = select_search_source(
        _outcome(indexer, direct),
        {},
        source_priority=["direct", "torrent", "usenet"],
    )

    assert selected is not None
    assert selected.source_kind == "direct"
    assert selected.direct_result is direct


def test_source_lane_precedes_cross_protocol_quality_score() -> None:
    usenet = _release(
        "Batman 001 (2016) (Digital).cbz",
        "Usenet",
        size=25_000_000,
        is_torrent=False,
    )
    torrent = _release(
        "Batman 001 (2016) (Digital).cbz",
        "Torrent",
        size=100_000_000,
        is_torrent=True,
    )

    selected = select_search_source(
        _indexer_only_outcome(torrent, usenet),
        {},
        source_priority=["usenet", "torrent", "direct"],
    )

    assert selected is not None
    assert selected.release is usenet


def test_indexer_priority_ranks_candidates_within_one_source_lane() -> None:
    lower_priority = _release(
        "Batman 001 (2016) (Digital).cbz",
        "Lower priority",
        is_torrent=False,
        ranking_priority=50,
    )
    higher_priority = _release(
        "Batman 001 (2016) (Digital).cbz",
        "Higher priority",
        is_torrent=False,
        ranking_priority=1,
    )

    selected = select_search_source(
        _indexer_only_outcome(lower_priority, higher_priority),
        {},
        source_priority=["usenet", "torrent", "direct"],
    )

    assert selected is not None
    assert selected.release is higher_priority


def test_ranked_sources_reuse_existing_scorer_for_fallback_order() -> None:
    indexer = _release("Batman 001.cbz", "Indexer", size=25_000_000)
    direct = _direct_result(_release("Batman 001 (2016) (Digital).cbz", "GetComics"))

    ranked = rank_search_sources(_outcome(indexer, direct), {})

    assert [item.source_kind for item in ranked] == ["direct", "indexer"]
    assert ranked[0].validation is direct.validation
    assert ranked[1].release is indexer


def test_direct_filename_quality_precedes_provider_priority_for_automatic_search() -> None:
    indexer = _release("Batman 001.cbz", "Indexer", size=25_000_000)
    getcomics = _direct_result(
        _release("Batman 001 (2016)", "GetComics", size=100_000_000),
        provider_identity="pullbox.getcomics",
        provider_priority=10,
    )
    annas = _direct_result(
        _release(
            "Batman 001 (2016) (Digital).cbz",
            "Anna's Archive",
            size=100_000_000,
        ),
        provider_identity="pullbox.annas_archive",
        provider_priority=20,
    )
    outcome = replace(
        _outcome(indexer, getcomics),
        direct_outcome=DirectSearchOutcome(
            matched=(annas, getcomics),
            rejected=(),
            failures=(),
            providers_searched=2,
            elapsed_ms=1,
        ),
    )

    ranked = rank_search_sources(
        outcome,
        {},
        source_priority=["direct", "usenet", "torrent"],
    )

    assert [item.direct_result for item in ranked[:2]] == [annas, getcomics]


def test_direct_provider_priority_breaks_quality_ties_for_automatic_search() -> None:
    indexer = _release("Batman 001.cbz", "Indexer", size=25_000_000)
    preferred = _direct_result(
        _release("Batman 001 (2016) (Digital).cbz", "GetComics"),
        provider_identity="pullbox.getcomics",
        provider_priority=10,
    )
    lower_priority = _direct_result(
        _release("Batman 001 (2016) (Digital).cbz", "LibGen"),
        provider_identity="pullbox.libgen",
        provider_priority=20,
    )
    outcome = replace(
        _outcome(indexer, preferred),
        direct_outcome=DirectSearchOutcome(
            matched=(lower_priority, preferred),
            rejected=(),
            failures=(),
            providers_searched=2,
            elapsed_ms=1,
        ),
    )

    ranked = rank_search_sources(
        outcome,
        {},
        source_priority=["direct", "usenet", "torrent"],
    )

    assert [item.direct_result for item in ranked[:2]] == [preferred, lower_priority]


def test_fingerprint_alternate_remains_available_for_acquisition_fallback() -> None:
    indexer = _release("Batman 001.cbz", "Indexer", size=25_000_000)
    primary = _direct_result(
        _release("Batman 001 (2016) (Digital).cbz", "LibGen"),
        provider_identity="pullbox.libgen",
        provider_priority=10,
    )
    alternate = _direct_result(
        _release("Batman 001 (2016) (Digital).cbz", "Anna's Archive"),
        provider_identity="pullbox.annas_archive",
        provider_priority=20,
    )
    primary = replace(primary, alternate_results=(alternate,))
    outcome = replace(
        _outcome(indexer, primary),
        direct_outcome=DirectSearchOutcome(
            matched=(primary,),
            rejected=(),
            failures=(),
            providers_searched=2,
            elapsed_ms=1,
        ),
    )

    ranked = rank_search_sources(
        outcome,
        {},
        source_priority=["direct", "usenet", "torrent"],
    )

    assert [item.direct_result for item in ranked[:2]] == [primary, alternate]


def test_direct_semantic_confidence_precedes_provider_priority() -> None:
    indexer = _release("Batman 001.cbz", "Indexer", size=25_000_000)
    lower_confidence_getcomics = _direct_result(
        _release("Batman 001", "GetComics", size=100_000_000),
        provider_identity="pullbox.getcomics",
        provider_priority=10,
    )
    exact_annas = _direct_result(
        _release(
            "Batman 001 (2016) (Digital).cbz",
            "Anna's Archive",
            size=100_000_000,
        ),
        provider_identity="pullbox.annas_archive",
        provider_priority=20,
    )
    outcome = replace(
        _outcome(indexer, lower_confidence_getcomics),
        direct_outcome=DirectSearchOutcome(
            matched=(lower_confidence_getcomics, exact_annas),
            rejected=(),
            failures=(),
            providers_searched=2,
            elapsed_ms=1,
        ),
    )

    ranked = rank_search_sources(
        outcome,
        {},
        source_priority=["direct", "usenet", "torrent"],
    )

    assert ranked[0].direct_result is exact_annas


def test_indexer_only_selection_preserves_precomputed_winner() -> None:
    indexer = _release("Batman 001 (2016) (Digital).cbz", "Indexer")
    sparse_validation = MagicMock()
    sparse_validation.release = None
    outcome = replace(
        _outcome(indexer, None),
        best_validation=sparse_validation,
        matched=[sparse_validation],
    )

    ranked = rank_search_sources(outcome, {})

    assert len(ranked) == 1
    assert ranked[0].source_kind == "indexer"
    assert ranked[0].release is indexer
    assert ranked[0].validation is sparse_validation


def test_typed_dc_lane_respects_four_source_priority() -> None:
    indexer = _release(
        "Batman 001 (2016) (Digital).cbz",
        "Usenet",
        is_torrent=False,
    )
    dc = _dc_result(_release("Batman 001 (2016) (Digital).cbz", "Air"))
    outcome = replace(
        _outcome(indexer, None),
        dc_outcome=DcSearchOutcome(
            matched=(dc,),
            rejected=(),
            client_summaries=(),
            raw_count=1,
            deduplicated_count=1,
            dropped_count=0,
            elapsed_ms=1,
            partial=False,
        ),
    )

    selected = select_search_source(
        outcome,
        {},
        source_priority=["dc", "usenet", "torrent", "direct"],
    )

    assert selected is not None
    assert selected.source_kind == "dc"
    assert selected.dc_result is dc
    assert selected.direct_result is None


def test_dc_semantic_confidence_precedes_route_availability_metrics() -> None:
    indexer = _release("Batman 001.cbz", "Indexer", size=25_000_000)
    lower_confidence = _dc_result(
        _release("Batman 001.cbz", "Air"),
        config_id=7,
        free_slots=4,
        source_count=20,
    )
    exact = _dc_result(
        _release("Batman 001 (2016) (Digital).cbz", "Air"),
        config_id=8,
        free_slots=0,
        source_count=1,
    )
    outcome = replace(
        _outcome(indexer, None),
        dc_outcome=DcSearchOutcome(
            matched=(lower_confidence, exact),
            rejected=(),
            client_summaries=(),
            raw_count=2,
            deduplicated_count=2,
            dropped_count=0,
            elapsed_ms=1,
            partial=False,
        ),
    )

    ranked = rank_search_sources(
        outcome,
        {},
        source_priority=["dc", "usenet", "torrent", "direct"],
    )

    assert [item.dc_result for item in ranked[:2]] == [exact, lower_confidence]

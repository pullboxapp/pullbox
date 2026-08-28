"""Select one search winner without coupling scoring to its transport."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pullbox.services.search_evaluation import DEFAULT_MIN_SCORE, _select_best_validation
from pullbox.services.search_scoring import (
    DEFAULT_MAX_SIZE_MB,
    DEFAULT_MIN_SIZE_MB,
    DIRECT_PROVIDER_NEUTRAL_PRIORITY,
    match_confidence_rank,
    normalize_source_priority,
)

if TYPE_CHECKING:
    from pullbox.providers.base import ReleaseResult
    from pullbox.services.airdcpp_search_types import DcValidatedCandidate
    from pullbox.services.direct_search_coordinator import DirectValidatedCandidate
    from pullbox.services.release_validator import ValidationResult
    from pullbox.services.search_targets import IssueSearchOutcome
    from pullbox.services.search_types import SearchEvalKwargs


@dataclass(frozen=True, slots=True)
class SearchSourceSelection:
    """The highest-ranked candidate plus the adapter needed to acquire it."""

    source_kind: Literal["indexer", "direct", "dc"]
    release: ReleaseResult
    validation: ValidationResult
    direct_result: DirectValidatedCandidate | None = None
    dc_result: DcValidatedCandidate | None = None

    def __post_init__(self) -> None:
        if (self.source_kind == "direct") is (self.direct_result is None):
            raise ValueError("Direct source selection must carry exactly one direct result")
        if (self.source_kind == "dc") is (self.dc_result is None):
            raise ValueError("DC source selection must carry exactly one DC result")
        if self.direct_result is not None and self.dc_result is not None:
            raise ValueError("Search source selection cannot mix acquisition adapters")


def select_search_source(
    outcome: IssueSearchOutcome,
    eval_kwargs: SearchEvalKwargs,
    *,
    source_priority: list[str] | None = None,
) -> SearchSourceSelection | None:
    """Rank indexer and direct matches with the existing deterministic scorer."""
    ranked = rank_search_sources(
        outcome,
        eval_kwargs,
        source_priority=source_priority,
        limit=1,
    )
    return ranked[0] if ranked else None


def rank_search_sources(
    outcome: IssueSearchOutcome,
    eval_kwargs: SearchEvalKwargs,
    *,
    source_priority: list[str] | None = None,
    limit: int | None = None,
) -> tuple[SearchSourceSelection, ...]:
    """Return fallback candidates by repeatedly applying the existing scorer."""
    if limit is not None and limit <= 0:
        return ()

    direct_matches = (
        tuple(
            result
            for primary in outcome.direct_outcome.matched
            for result in (primary, *primary.alternate_results)
        )
        if outcome.direct_outcome
        else ()
    )
    dc_matches = outcome.dc_outcome.matched if outcome.dc_outcome else ()
    ranked: list[SearchSourceSelection] = []
    indexer_matches = outcome.matched
    if (
        not direct_matches
        and not dc_matches
        and outcome.best_validation is not None
        and outcome.best_release is not None
    ):
        # Search evaluation already selected this winner. Preserve that result
        # instead of scoring it a second time solely to build a fallback list.
        ranked.append(
            SearchSourceSelection(
                source_kind="indexer",
                release=outcome.best_release,
                validation=outcome.best_validation,
            )
        )
        if limit == 1:
            return tuple(ranked)
        indexer_matches = [item for item in outcome.matched if item is not outcome.best_validation]

    candidates: list[
        tuple[
            str,
            ValidationResult,
            DirectValidatedCandidate | None,
            DcValidatedCandidate | None,
        ]
    ] = [(item.release.protocol.value, item, None, None) for item in indexer_matches]
    if not ranked and not candidates and outcome.best_validation is not None:
        candidates.append(
            (
                outcome.best_validation.release.protocol.value,
                outcome.best_validation,
                None,
                None,
            )
        )
    candidates.extend(("direct", item.validation, item, None) for item in direct_matches)
    candidates.extend(("dc", item.validation, None, item) for item in dc_matches)
    normalized_priority = normalize_source_priority(source_priority)
    if normalized_priority is not None:
        priority_map = {source: index for index, source in enumerate(normalized_priority)}
        candidates.sort(key=lambda item: priority_map[item[0]])

    def _select(validations: list[ValidationResult]) -> ValidationResult | None:
        return _select_best_validation(
            validations,
            min_score=eval_kwargs.get("min_score", DEFAULT_MIN_SCORE),
            confidence_blend=eval_kwargs.get("confidence_blend", 0.40),
            min_size_mb=eval_kwargs.get("min_size_mb", DEFAULT_MIN_SIZE_MB),
            max_size_mb=eval_kwargs.get("max_size_mb", DEFAULT_MAX_SIZE_MB),
            preferred_format=eval_kwargs.get("preferred_format"),
            seeder_tiers=eval_kwargs.get("seeder_tiers"),
            score_weights=eval_kwargs.get("score_weights"),
            grabs_weight=eval_kwargs.get("grabs_weight", 0),
            pack_penalty=eval_kwargs.get("pack_penalty", -20),
            max_file_count=eval_kwargs.get("max_file_count", 5),
            preferred_language=eval_kwargs.get("preferred_language", "en"),
            digital_bonus=eval_kwargs.get("digital_bonus", 10),
            source_priority=normalized_priority,
        )

    def _preferred_direct_candidate(
        items: list[
            tuple[
                str,
                ValidationResult,
                DirectValidatedCandidate | None,
                DcValidatedCandidate | None,
            ]
        ],
    ) -> (
        tuple[
            str,
            ValidationResult,
            DirectValidatedCandidate | None,
            DcValidatedCandidate | None,
        ]
        | None
    ):
        direct_items = [item for item in items if item[2] is not None]
        semantic_keys = sorted(
            {
                (
                    match_confidence_rank(item[1].confidence),
                    -item[1].series_similarity,
                )
                for item in direct_items
                if item[2] is not None
            }
        )
        for key in semantic_keys:
            group = sorted(
                [
                    item
                    for item in direct_items
                    if item[2] is not None
                    and (
                        match_confidence_rank(item[1].confidence),
                        -item[1].series_similarity,
                    )
                    == key
                ],
                key=lambda item: item[2].provider.provider_priority if item[2] is not None else 0,
            )
            selected = _select_best_validation(
                [item[1] for item in group],
                min_score=eval_kwargs.get("min_score", DEFAULT_MIN_SCORE),
                confidence_blend=eval_kwargs.get("confidence_blend", 0.40),
                indexer_priority=DIRECT_PROVIDER_NEUTRAL_PRIORITY,
                min_size_mb=eval_kwargs.get("min_size_mb", DEFAULT_MIN_SIZE_MB),
                max_size_mb=eval_kwargs.get("max_size_mb", DEFAULT_MAX_SIZE_MB),
                preferred_format=eval_kwargs.get("preferred_format"),
                seeder_tiers=eval_kwargs.get("seeder_tiers"),
                score_weights=eval_kwargs.get("score_weights"),
                grabs_weight=eval_kwargs.get("grabs_weight", 0),
                pack_penalty=eval_kwargs.get("pack_penalty", -20),
                max_file_count=eval_kwargs.get("max_file_count", 5),
                preferred_language=eval_kwargs.get("preferred_language", "en"),
                digital_bonus=eval_kwargs.get("digital_bonus", 10),
            )
            if selected is not None:
                return next(item for item in group if item[1] is selected)
        return None

    def _preferred_dc_candidate(
        items: list[
            tuple[
                str,
                ValidationResult,
                DirectValidatedCandidate | None,
                DcValidatedCandidate | None,
            ]
        ],
    ) -> (
        tuple[
            str,
            ValidationResult,
            DirectValidatedCandidate | None,
            DcValidatedCandidate | None,
        ]
        | None
    ):
        dc_items = [item for item in items if item[3] is not None]
        if not dc_items:
            return None

        def route_key(
            item: tuple[
                str,
                ValidationResult,
                DirectValidatedCandidate | None,
                DcValidatedCandidate | None,
            ],
        ) -> tuple[int, float, int, int, int, int, int, int, str]:
            dc = item[3]
            if dc is None:  # pragma: no cover - filtered above
                raise RuntimeError("DC candidate is unavailable")
            connection = dc.metrics.aggregate_connection_bytes_per_second or 0
            return (
                match_confidence_rank(item[1].confidence),
                -item[1].series_similarity,
                dc.release.ranking_priority,
                0 if dc.metrics.free_slots > 0 else 1,
                -dc.metrics.free_slots,
                -dc.metrics.source_count,
                -connection,
                dc.route.client_config_id,
                dc.route.grouped_result_id,
            )

        return min(dc_items, key=route_key)

    remaining = list(candidates)
    while remaining and (limit is None or len(ranked) < limit):
        selected = _select([item[1] for item in remaining])
        if selected is None:
            break
        selected_item = next(item for item in remaining if item[1] is selected)
        if selected_item[2] is not None:
            preferred_direct = _preferred_direct_candidate(remaining)
            if preferred_direct is not None:
                selected_item = preferred_direct
                selected = preferred_direct[1]
        elif selected_item[3] is not None:
            preferred_dc = _preferred_dc_candidate(remaining)
            if preferred_dc is not None:
                selected_item = preferred_dc
                selected = preferred_dc[1]
        selected_index = next(
            index for index, item in enumerate(remaining) if item is selected_item
        )
        _, _, direct_result, dc_result = remaining.pop(selected_index)
        ranked.append(
            SearchSourceSelection(
                source_kind=(
                    "direct"
                    if direct_result is not None
                    else "dc"
                    if dc_result is not None
                    else "indexer"
                ),
                release=selected.release,
                validation=selected,
                direct_result=direct_result,
                dc_result=dc_result,
            )
        )
    return tuple(ranked)

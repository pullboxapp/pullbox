"""Known ComicVine ID shortcuts for import series matching."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pullbox.core.source_metadata import MetadataSignal

if TYPE_CHECKING:
    from pullbox.core.source_metadata import SourceMetadata


@dataclass
class ComicVineMatchEvaluation:
    """Evaluated ComicVine match result plus structured diagnostics."""

    match: dict[str, Any] | None
    diagnostics: dict[str, Any]


async def get_series_cached(provider: Any, series_provider_id: str) -> Any | None:
    """Return cached series metadata if the provider stack can do so without fetching."""
    cached_lookup = explicit_provider_method(provider, "get_series_cached")
    if cached_lookup is None:
        return None
    return await cached_lookup(series_provider_id)


def trusted_known_cv_id_without_fetch(
    *,
    mylar3_cv_id: int | None,
    comicinfo_cv_id: int | None,
    source_metadata: SourceMetadata,
) -> bool:
    """Return True only for source IDs we can use as Step 2 identity shortcuts."""
    if mylar3_cv_id is not None:
        return True
    if comicinfo_cv_id is None and source_metadata.comicvine_series_id is None:
        return False
    if source_metadata.diagnostics.get("identity_conflicts"):
        return False
    series_id_signal = source_metadata.signals.get("comicvine_series_id")
    if series_id_signal == MetadataSignal.SIDECAR:
        return True
    return series_id_signal in {None, MetadataSignal.COMICINFO} and source_metadata.diagnostics.get(
        "comicvine_series_id_source"
    ) in {"comicvine_volume_url", "comicvine_note_id"}


def known_cv_id_evaluation_from_metadata(
    meta: Any,
    *,
    match_method: str,
    raw_name: str,
    raw_year: int | None,
    normalized_query: str,
    match_threshold: float,
    reason: str,
) -> ComicVineMatchEvaluation:
    cv_id = int(meta.provider_id)
    return ComicVineMatchEvaluation(
        match={
            "cv_id": cv_id,
            "cv_title": meta.title,
            "cv_year": meta.year_start,
            "cv_publisher": meta.publisher,
            "cv_issue_count": meta.issue_count,
            "cv_url": meta.comicvine_url,
            "cv_match_score": 1.0,
            "cv_match_method": match_method,
        },
        diagnostics={
            "kind": "series_match",
            "reason": reason,
            "raw_name": raw_name,
            "raw_year": raw_year,
            "normalized_query": normalized_query,
            "threshold": round(match_threshold, 4),
            "selected_candidate": {
                "cv_id": cv_id,
                "title": meta.title,
                "year": meta.year_start,
                "publisher": meta.publisher,
                "issue_count": meta.issue_count,
                "score": 1.0,
                "score_pct": 100,
                "match_method": match_method,
            },
            "top_candidates": [],
        },
    )


def known_cv_id_evaluation_from_source(
    cv_id: int,
    *,
    match_method: str,
    raw_name: str,
    raw_year: int | None,
    normalized_query: str,
    source_metadata: SourceMetadata,
    match_threshold: float,
) -> ComicVineMatchEvaluation:
    title = source_metadata.series_name or raw_name
    year = source_metadata.year if source_metadata.year is not None else raw_year
    return ComicVineMatchEvaluation(
        match={
            "cv_id": cv_id,
            "cv_title": title,
            "cv_year": year,
            "cv_publisher": source_metadata.publisher,
            "cv_issue_count": source_metadata.issue_count_hint,
            "cv_url": None,
            "cv_match_score": 1.0,
            "cv_match_method": match_method,
        },
        diagnostics={
            "kind": "series_match",
            "reason": "trusted_known_cv_id_unverified",
            "raw_name": raw_name,
            "raw_year": raw_year,
            "normalized_query": normalized_query,
            "threshold": round(match_threshold, 4),
            "selected_candidate": {
                "cv_id": cv_id,
                "title": title,
                "year": year,
                "publisher": source_metadata.publisher,
                "issue_count": source_metadata.issue_count_hint,
                "score": 1.0,
                "score_pct": 100,
                "match_method": match_method,
                "verification": "deferred",
            },
            "top_candidates": [],
        },
    )


def explicit_provider_method(provider: Any, method_name: str) -> Any | None:
    """Return a provider method only when the concrete provider explicitly exposes it."""
    method = getattr(provider, method_name, None)
    if not callable(method):
        return None
    if not hasattr(type(provider), method_name):
        return None
    return method

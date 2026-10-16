"""Conservative, local-only title/year fallback for folder imports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.name_matcher import NameMatcher
from pullbox.core.release_parser import parse_release_title
from pullbox.core.type_semantics import issue_type_family
from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.models.issue import IssueType
from pullbox.services.catalog.contract import CatalogError
from pullbox.services.import_cv_candidate_ranking import (
    build_ranked_candidate_diagnostics,
    build_selected_candidate_summary,
)
from pullbox.services.import_known_cv_match import ComicVineMatchEvaluation
from pullbox.services.semantic_matching import ImportPolicy, SemanticMatchEngine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.source_metadata import SourceMetadata
    from pullbox.models.import_job import ImportedSeries

_CANDIDATE_LIMIT = 50
_ISSUE_SAMPLE_LIMIT = 200


async def evaluate_local_catalog_match(
    session: AsyncSession,
    item: ImportedSeries,
    metadata: SourceMetadata,
    *,
    provider: Any,
    match_threshold: float,
) -> ComicVineMatchEvaluation | None:
    """Require a unique exact title/year and a corroborating local issue number.

    Never broaden missing embedded identities into a title guess or turn a
    local miss into an HTTP request. Files still pass independent issue review.
    """
    if (
        getattr(provider, "is_local_catalog", False) is not True
        or item.user_selected_cv_id is not None
        or metadata.diagnostics.get("identity_conflicts")
    ):
        return None
    eligible = (
        ImportedFile.import_series_id == item.id,
        ImportedFile.status.not_in([ImportedFileStatus.SAFETY_BLOCKED, ImportedFileStatus.SKIPPED]),
    )
    embedded_id = await session.scalar(
        select(ImportedFile.id)
        .where(*eligible, ImportedFile.comicvine_issue_id.is_not(None))
        .limit(1)
    )
    if embedded_id is not None:
        return None
    rows = (
        await session.execute(
            select(ImportedFile.parsed_issue_number, ImportedFile.issue_number_raw)
            .where(*eligible, ImportedFile.parsed_issue_number.is_not(None))
            .order_by(ImportedFile.id)
            .limit(_ISSUE_SAMPLE_LIMIT)
        )
    ).all()
    if not rows:
        return None
    name = metadata.series_name or item.raw_series_name
    year = metadata.year or item.raw_year
    normalized_name = NameMatcher.normalize(name)
    diagnostics: dict[str, Any] = {
        "kind": "series_no_match",
        "reason": "local_catalog_match_needs_review",
        "raw_name": item.raw_series_name,
        "raw_year": item.raw_year,
        "normalized_query": normalized_name,
        "threshold": match_threshold,
        "lookup": "local_catalog",
        "provider_lookup_deferred": False,
        "top_candidates": [],
    }
    review = ComicVineMatchEvaluation(match=None, diagnostics=diagnostics)
    if not normalized_name or year is None:
        return review
    try:
        results = await provider.search_series(name, year, limit=_CANDIDATE_LIMIT + 1)
        ranked = build_ranked_candidate_diagnostics(
            raw_name=name,
            raw_year=year,
            source_metadata=metadata,
            search_results=results,
            semantic_engine=SemanticMatchEngine(policy=ImportPolicy()),
            match_threshold=match_threshold,
        )
        diagnostics["top_candidates"] = ranked[:3]
        exact = {
            result.provider_id: result
            for result in results
            if NameMatcher.normalize(result.title) == normalized_name and result.year_start == year
        }
        if len(results) > _CANDIDATE_LIMIT or len(exact) != 1:
            return review
        selected = next(iter(exact.values()))
        # Catalog series titles do not always distinguish collections from
        # singles. A non-standard source needs a matching explicit qualifier.
        parsed_title = parse_release_title(selected.title)
        if metadata.issue_type != IssueType.ISSUE and (
            parsed_title is None
            or issue_type_family(parsed_title.issue_type) != issue_type_family(metadata.issue_type)
            or (
                metadata.issue_type == IssueType.ANNUAL
                and parsed_title.issue_type != IssueType.ANNUAL
            )
        ):
            return review
        candidate = next(row for row in ranked if row["cv_id"] == int(selected.provider_id))
        if float(candidate["score"]) < max(0.9, match_threshold):
            return review
        source_numbers = {
            normalize_issue_number_text(row.issue_number_raw or row.parsed_issue_number)
            for row in rows
        }
        issues = await provider.get_issues_for_series_by_numbers(
            selected.provider_id, sorted(source_numbers)
        )
        catalog_numbers = {
            normalize_issue_number_text(issue.issue_number_text or issue.issue_number)
            for issue in issues
        }
        corroborated = source_numbers & catalog_numbers
        if not corroborated:
            diagnostics["reason"] = "local_catalog_issue_evidence_missing"
            return review
    except (CatalogError, ValueError):
        diagnostics["reason"] = "local_catalog_unavailable"
        return review
    diagnostics.update(
        kind="series_match",
        reason="local_catalog_title_year_verified",
        selected_candidate=build_selected_candidate_summary(
            selected,
            score=candidate["score"],
            match_method="exact_title_year",
            match_type=str(candidate.get("match_type") or ""),
            year_delta=0,
        ),
        local_catalog_evidence={
            "sampled_files": len(rows),
            "corroborated_numbers": sorted(corroborated)[:10],
        },
    )
    return ComicVineMatchEvaluation(
        match={
            "cv_id": int(selected.provider_id),
            "cv_title": selected.title,
            "cv_year": selected.year_start,
            "cv_publisher": selected.publisher,
            "cv_issue_count": selected.issue_count,
            "cv_url": selected.comicvine_url,
            "cv_match_score": candidate["score"],
            "cv_match_method": "exact_title_year",
        },
        diagnostics=diagnostics,
    )

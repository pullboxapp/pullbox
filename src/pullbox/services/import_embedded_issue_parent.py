"""Verify a folder's missing parent identity from embedded ComicVine issue IDs.

This is local-only discovery, not a remote search fallback. Every eligible
embedded issue ID must resolve to the same parent; missing or mixed evidence
stays in review. File-level title/number checks still run independently.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pullbox.core.name_matcher import NameMatcher
from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.services.catalog.contract import CatalogError
from pullbox.services.import_known_cv_match import (
    ComicVineMatchEvaluation,
    explicit_provider_method,
    get_series_cached,
    known_cv_id_evaluation_from_metadata,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedSeries

_PAGE_SIZE = 200


def _positive_cv_id(value: object) -> int | None:
    text = str(value)
    if not text.isascii() or not text.isdecimal() or len(text) > 19:
        return None
    result = int(text)
    return result if 0 < result < 2**63 else None


async def evaluate_embedded_issue_parent(
    session: AsyncSession,
    item: ImportedSeries,
    *,
    provider: Any,
    match_threshold: float,
    raise_if_cancelled: Callable[[AsyncSession, int], Awaitable[None]],
) -> ComicVineMatchEvaluation | None:
    """Resolve only cached issue-to-parent links using bounded projections."""
    lookup = explicit_provider_method(provider, "get_issue_batch_cached")
    if lookup is None or item.user_selected_cv_id is not None:
        return None
    after_id = 0
    parent_ids: set[int] = set()
    unresolved = False
    verified_count = 0
    samples: list[int] = []
    while True:
        await raise_if_cancelled(session, item.import_job_id)
        rows = (
            await session.execute(
                select(ImportedFile.id, ImportedFile.comicvine_issue_id, ImportedFile.diagnostics)
                .where(
                    ImportedFile.import_series_id == item.id,
                    ImportedFile.id > after_id,
                    ImportedFile.comicvine_issue_id.is_not(None),
                    ImportedFile.status.not_in(
                        [ImportedFileStatus.SAFETY_BLOCKED, ImportedFileStatus.SKIPPED]
                    ),
                )
                .order_by(ImportedFile.id)
                .limit(_PAGE_SIZE)
            )
        ).all()
        if not rows:
            break
        after_id = rows[-1].id
        identity_counts: Counter[str] = Counter()
        for row in rows:
            signals = (row.diagnostics or {}).get("metadata_signals")
            if (
                isinstance(signals, dict)
                and signals.get("comicvine_issue_id") == "comicinfo"
                and _positive_cv_id(row.comicvine_issue_id) is not None
            ):
                identity_counts[str(row.comicvine_issue_id)] += 1
        ids = list(identity_counts)
        if not ids:
            continue
        try:
            issues = await lookup(ids)
        except (CatalogError, ValueError, TypeError, KeyError):
            return None
        for issue_id in ids:
            issue = issues.get(issue_id)
            parent_id = _positive_cv_id(getattr(issue, "series_provider_id", None))
            if parent_id is None or str(getattr(issue, "provider_id", "")) != issue_id:
                unresolved = True
                continue
            parent_ids.add(parent_id)
            verified_count += identity_counts[issue_id]
            if len(samples) < 10:
                samples.append(int(issue_id))
        if len(parent_ids) > 1:
            return ComicVineMatchEvaluation(
                match=None,
                diagnostics={
                    "kind": "series_conflict",
                    "reason": "trusted_source_identity_conflict",
                    "identity_conflicts": [
                        {
                            "field": "comicvine_series_id",
                            "source": "comicinfo_issue_parent",
                            "series_ids": sorted(parent_ids),
                        }
                    ],
                    "provider_lookup_deferred": True,
                    "top_candidates": [],
                },
            )
    if unresolved or not parent_ids:
        return None
    parent_id = next(iter(parent_ids))
    try:
        parent = await get_series_cached(provider, str(parent_id))
    except (CatalogError, ValueError, TypeError, KeyError):
        return None
    if parent is None or _positive_cv_id(parent.provider_id) != parent_id:
        return None
    evaluation = known_cv_id_evaluation_from_metadata(
        parent,
        match_method="comicinfo_cv_id",
        raw_name=item.raw_series_name,
        raw_year=item.raw_year,
        normalized_query=NameMatcher.normalize(item.raw_series_name),
        match_threshold=match_threshold,
        reason="comicinfo_issue_parent_verified",
    )
    evaluation.diagnostics["embedded_issue_parent"] = {
        "series_id": parent_id,
        "verified_file_count": verified_count,
        "sample_issue_ids": samples,
        "lookup": "local_or_cached",
    }
    return evaluation

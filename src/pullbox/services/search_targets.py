"""Database-backed search target loaders and batch target execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import and_, exists, or_, select

from pullbox.core.type_semantics import TypeFamily, issue_type_family
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.series import Series

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.indexer import IndexerConfig
    from pullbox.providers.base import ReleaseResult
    from pullbox.services.airdcpp_search_types import DcSearchOutcome
    from pullbox.services.direct_search_coordinator import DirectSearchOutcome
    from pullbox.services.release_validator import ValidationResult
    from pullbox.services.search_types import IssueSearchMode, SearchEvalKwargs, ValidatorKwargs


@dataclass(frozen=True)
class IssueSearchTarget:
    """Read-only issue + series search context."""

    issue_id: int
    series_id: int
    series_title: str
    issue_number: float
    issue_type: IssueType
    issue_title: str | None = None
    series_year: int | None = None
    release_year: int | None = None
    alternate_names: list[str] | None = None
    series_issue_count: int | None = None

    @property
    def search_year(self) -> int | None:
        """Use publication year for collections and series year for serial issues."""
        if issue_type_family(self.issue_type) is TypeFamily.COLLECTION:
            return self.release_year or self.series_year
        return self.series_year


@dataclass(frozen=True)
class IssueSearchOutcome:
    """Fully evaluated result of searching for one issue target."""

    target: IssueSearchTarget
    mode: IssueSearchMode
    query_count: int
    raw_results: list[ReleaseResult]
    filtered_results: list[ReleaseResult]
    matched: list[ValidationResult]
    rejected: list[ValidationResult]
    best_release: ReleaseResult | None
    best_validation: ValidationResult | None
    search_details: dict[str, object]
    elapsed_ms: int
    used_fallback: bool = False
    direct_outcome: DirectSearchOutcome | None = None
    dc_outcome: DcSearchOutcome | None = None


SearchOutcomeCallback = Callable[[IssueSearchOutcome], Awaitable[None]]


class SearchIssueTargetFunc(Protocol):
    """Callable shape for searching a single issue target."""

    def __call__(
        self,
        session: AsyncSession,
        target: IssueSearchTarget,
        *,
        mode: IssueSearchMode = "deep",
        indexer_configs: dict[int, IndexerConfig] | None = None,
        eval_kwargs: SearchEvalKwargs | None = None,
        validator_kwargs: ValidatorKwargs | None = None,
        source_priority: list[str] | None = None,
        auto_fallback: bool = False,
        force_generic: bool = False,
        session_lock: asyncio.Lock | None = None,
    ) -> Awaitable[IssueSearchOutcome]: ...


def _target_from_row(row: Any) -> IssueSearchTarget:
    """Build a search target from a SQLAlchemy row with the expected labels."""
    release_date = getattr(row, "release_date", None) or getattr(row, "store_date", None)
    series_issue_count = getattr(row, "series_issue_count", None)
    return IssueSearchTarget(
        issue_id=int(row.issue_id),
        series_id=int(row.series_id),
        series_title=str(row.series_title),
        issue_number=float(row.issue_number),
        issue_type=IssueType(str(row.issue_type)) if row.issue_type else IssueType.ISSUE,
        issue_title=str(row.issue_title) if row.issue_title else None,
        series_year=int(row.series_year) if row.series_year else None,
        release_year=release_date.year if release_date is not None else None,
        alternate_names=list(row.alternate_names) if row.alternate_names else None,
        series_issue_count=(int(series_issue_count) if series_issue_count is not None else None),
    )


async def load_issue_search_target(
    session: AsyncSession,
    issue_id: int,
) -> IssueSearchTarget | None:
    """Load one issue search target."""
    result = await session.execute(
        select(
            Issue.id.label("issue_id"),
            Issue.series_id.label("series_id"),
            Issue.issue_number.label("issue_number"),
            Issue.issue_type.label("issue_type"),
            Issue.title.label("issue_title"),
            Issue.release_date.label("release_date"),
            Issue.store_date.label("store_date"),
            Series.title.label("series_title"),
            Series.year_start.label("series_year"),
            Series.alternate_names.label("alternate_names"),
            Series.issue_count.label("series_issue_count"),
        )
        .join(Series, Series.id == Issue.series_id)
        .where(Issue.id == issue_id)
        .limit(1)
    )
    row = result.first()
    if row is None:
        return None
    return _target_from_row(row)


async def load_series_wanted_search_targets(
    session: AsyncSession,
    series_id: int,
) -> list[IssueSearchTarget]:
    """Load all wanted issue targets for one series."""
    result = await session.execute(
        select(
            Issue.id.label("issue_id"),
            Issue.series_id.label("series_id"),
            Issue.issue_number.label("issue_number"),
            Issue.issue_type.label("issue_type"),
            Issue.title.label("issue_title"),
            Issue.release_date.label("release_date"),
            Issue.store_date.label("store_date"),
            Series.title.label("series_title"),
            Series.year_start.label("series_year"),
            Series.alternate_names.label("alternate_names"),
            Series.issue_count.label("series_issue_count"),
        )
        .join(Series, Series.id == Issue.series_id)
        .where(Issue.series_id == series_id)
        .where(Issue.status == IssueStatus.WANTED)
        .order_by(Issue.issue_number)
    )
    return [_target_from_row(row) for row in result.all()]


async def search_issue_targets(
    session: AsyncSession,
    targets: list[IssueSearchTarget],
    *,
    mode: IssueSearchMode,
    search_issue_target_func: SearchIssueTargetFunc,
    indexer_configs: dict[int, IndexerConfig] | None = None,
    eval_kwargs: SearchEvalKwargs | None = None,
    validator_kwargs: ValidatorKwargs | None = None,
    source_priority: list[str] | None = None,
    auto_fallback: bool = False,
    force_generic: bool = False,
    concurrency: int = 5,
    on_outcome: SearchOutcomeCallback | None = None,
) -> list[IssueSearchOutcome]:
    """Search issue targets with bounded concurrency."""
    if not targets:
        return []

    semaphore = asyncio.Semaphore(min(concurrency, len(targets)) or 1)
    # Provider requests stay concurrent, but every AsyncSession operation is serialized.
    session_lock = asyncio.Lock()

    async def _search(target: IssueSearchTarget) -> IssueSearchOutcome:
        async with semaphore:
            outcome = await search_issue_target_func(
                session,
                target,
                mode=mode,
                indexer_configs=indexer_configs,
                eval_kwargs=eval_kwargs,
                validator_kwargs=validator_kwargs,
                source_priority=source_priority,
                auto_fallback=auto_fallback,
                force_generic=force_generic,
                session_lock=session_lock,
            )
        if on_outcome is not None:
            async with session_lock:
                await on_outcome(outcome)
        return outcome

    return await asyncio.gather(*[_search(target) for target in targets])


async def load_wanted_issue_search_targets(
    session: AsyncSession,
    *,
    limit: int = 50,
    after: tuple[int, float, int] | None = None,
) -> list[IssueSearchTarget]:
    """Load wanted issue targets for the global sweep."""
    filters = [
        Issue.status == IssueStatus.WANTED,
        Series.monitored.is_(True),
        ~exists().where(
            and_(
                PendingMatch.issue_id == Issue.id,
                PendingMatch.status == PendingMatchStatus.PENDING,
            )
        ),
    ]
    if after is not None:
        series_id, issue_number, issue_id = after
        filters.append(
            or_(
                Issue.series_id > series_id,
                and_(Issue.series_id == series_id, Issue.issue_number > issue_number),
                and_(
                    Issue.series_id == series_id,
                    Issue.issue_number == issue_number,
                    Issue.id > issue_id,
                ),
            )
        )

    result = await session.execute(
        select(
            Issue.id.label("issue_id"),
            Issue.series_id.label("series_id"),
            Issue.issue_number.label("issue_number"),
            Issue.issue_type.label("issue_type"),
            Issue.title.label("issue_title"),
            Issue.release_date.label("release_date"),
            Issue.store_date.label("store_date"),
            Series.title.label("series_title"),
            Series.year_start.label("series_year"),
            Series.alternate_names.label("alternate_names"),
            Series.issue_count.label("series_issue_count"),
        )
        .join(Series, Series.id == Issue.series_id)
        .where(*filters)
        .order_by(Issue.series_id, Issue.issue_number, Issue.id)
        .limit(limit)
    )
    return [_target_from_row(row) for row in result.all()]


async def load_wanted_issue_search_targets_by_ids(
    session: AsyncSession,
    issue_ids: list[int],
) -> list[IssueSearchTarget]:
    """Load still-eligible wanted targets while preserving snapshot order."""
    if not issue_ids:
        return []
    result = await session.execute(
        select(
            Issue.id.label("issue_id"),
            Issue.series_id.label("series_id"),
            Issue.issue_number.label("issue_number"),
            Issue.issue_type.label("issue_type"),
            Issue.title.label("issue_title"),
            Issue.release_date.label("release_date"),
            Issue.store_date.label("store_date"),
            Series.title.label("series_title"),
            Series.year_start.label("series_year"),
            Series.alternate_names.label("alternate_names"),
            Series.issue_count.label("series_issue_count"),
        )
        .join(Series, Series.id == Issue.series_id)
        .where(
            Issue.id.in_(issue_ids),
            Issue.status == IssueStatus.WANTED,
            Series.monitored.is_(True),
            ~exists().where(
                and_(
                    PendingMatch.issue_id == Issue.id,
                    PendingMatch.status == PendingMatchStatus.PENDING,
                )
            ),
        )
    )
    targets_by_id = {
        target.issue_id: target for target in (_target_from_row(row) for row in result.all())
    }
    return [targets_by_id[issue_id] for issue_id in issue_ids if issue_id in targets_by_id]

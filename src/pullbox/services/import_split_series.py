"""Review-time detection for one logical series spanning library roots."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.services.library_root_management import validate_managed_library_root

if TYPE_CHECKING:
    from typing import Any, Protocol

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportJob, ImportJobAction

    class RecordActionFunc(Protocol):
        async def __call__(
            self,
            session: AsyncSession,
            job: ImportJob,
            *,
            phase: str,
            action_type: str,
            payload: dict[str, Any],
        ) -> ImportJobAction: ...


@dataclass(frozen=True, slots=True)
class SplitSeriesReviewItem:
    """One canonical series whose selected files span multiple roots."""

    imported_series_ids: tuple[int, ...]
    canonical_series_id: int | None
    comicvine_id: int | None
    title: str
    root_ids: tuple[int, ...]
    root_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SplitSeriesReview:
    """Selected split-series rows that require one future destination."""

    items: tuple[SplitSeriesReviewItem, ...] = ()

    @property
    def requires_preferred_destination(self) -> bool:
        return bool(self.items)


@dataclass(frozen=True, slots=True)
class _RootBoundary:
    root_id: int
    name: str
    lexical_path: Path
    resolved_path: Path


@dataclass(slots=True)
class _ReviewAccumulator:
    imported_series_ids: set[int] = field(default_factory=set)
    canonical_series_id: int | None = None
    comicvine_id: int | None = None
    title: str = "Series"
    root_ids: set[int] = field(default_factory=set)


async def load_selected_split_series_review(
    session: AsyncSession,
    job: ImportJob,
) -> SplitSeriesReview:
    """Return selected in-place series that will span more than one root.

    The analysis is read-only. It groups import rows by canonical local series
    identity (or ComicVine identity before creation), retains each selected
    source file's containing root, and includes roots already used by a matched
    local series. Existing paths are never rewritten by this workflow.
    """
    if job.file_handling_mode != ImportFileHandlingMode.IN_PLACE:
        return SplitSeriesReview()

    boundaries = await _load_root_boundaries(session)
    if not boundaries:
        return SplitSeriesReview()

    rows = await _load_selected_file_rows(session, int(job.id))
    if not rows:
        return SplitSeriesReview()

    comicvine_ids = {
        resolved_cv_id
        for _item_id, _status, _series_id, cv_id, user_cv_id, _title, _path in rows
        if (resolved_cv_id := _resolve_comicvine_id(cv_id, user_cv_id)) is not None
    }
    existing_series_by_cv = await _load_existing_series_by_comicvine_id(
        session,
        comicvine_ids,
    )

    accumulators: dict[tuple[str, int], _ReviewAccumulator] = {}
    for item_id, _status, series_id, cv_id, user_cv_id, title, file_path in rows:
        resolved_cv_id = _resolve_comicvine_id(cv_id, user_cv_id)
        canonical_series_id = (
            int(series_id)
            if series_id is not None
            else existing_series_by_cv.get(resolved_cv_id)
            if resolved_cv_id is not None
            else None
        )
        if canonical_series_id is not None:
            key = ("series", canonical_series_id)
        elif resolved_cv_id is not None:
            key = ("comicvine", resolved_cv_id)
        else:
            key = ("import", int(item_id))
        accumulator = accumulators.setdefault(
            key,
            _ReviewAccumulator(
                canonical_series_id=canonical_series_id,
                comicvine_id=resolved_cv_id,
                title=str(title or "Series"),
            ),
        )
        accumulator.imported_series_ids.add(int(item_id))
        root_id = _containing_root_id(str(file_path), boundaries)
        if root_id is not None:
            accumulator.root_ids.add(root_id)

    existing_root_ids = await _load_existing_series_root_ids(
        session,
        {
            accumulator.canonical_series_id
            for accumulator in accumulators.values()
            if accumulator.canonical_series_id is not None
        },
    )
    for accumulator in accumulators.values():
        if accumulator.canonical_series_id is not None:
            accumulator.root_ids.update(
                existing_root_ids.get(accumulator.canonical_series_id, set())
            )

    root_names = {boundary.root_id: boundary.name for boundary in boundaries}
    review_items = [
        SplitSeriesReviewItem(
            imported_series_ids=tuple(sorted(accumulator.imported_series_ids)),
            canonical_series_id=accumulator.canonical_series_id,
            comicvine_id=accumulator.comicvine_id,
            title=accumulator.title,
            root_ids=tuple(sorted(accumulator.root_ids)),
            root_names=tuple(
                root_names.get(root_id, f"Library root {root_id}")
                for root_id in sorted(accumulator.root_ids)
            ),
        )
        for accumulator in accumulators.values()
        if len(accumulator.root_ids) > 1
    ]
    review_items.sort(
        key=lambda item: (
            item.title.casefold(),
            item.canonical_series_id or 0,
            item.comicvine_id or 0,
            item.imported_series_ids,
        )
    )
    return SplitSeriesReview(items=tuple(review_items))


async def require_preferred_managed_root_for_selected_split_series(
    session: AsyncSession,
    job: ImportJob,
    *,
    preferred_library_root_id: int | None,
) -> SplitSeriesReview:
    """Require and validate a future root only when selected series are split."""
    review = await load_selected_split_series_review(session, job)
    if not review.requires_preferred_destination:
        return review
    if preferred_library_root_id is None:
        raise ValidationError(
            "Choose a preferred managed destination for future acquisitions before "
            "importing a series that spans multiple library roots. Existing files will "
            "remain in place."
        )
    root = await session.get(LibraryRoot, preferred_library_root_id)
    if root is None:
        raise ValidationError("The selected preferred managed destination does not exist.")
    await validate_managed_library_root(root)
    return review


async def apply_import_preferred_series_root(
    session: AsyncSession,
    job: ImportJob,
    *,
    series_id: int,
    record_action: RecordActionFunc,
) -> bool:
    """Persist an explicit in-place future destination without moving files."""
    if (
        job.file_handling_mode != ImportFileHandlingMode.IN_PLACE
        or job.target_library_root_id is None
    ):
        return False
    root = await session.get(LibraryRoot, job.target_library_root_id)
    if root is None:
        raise ValidationError("The selected preferred managed destination does not exist.")
    await validate_managed_library_root(root)
    series = await session.get(Series, series_id)
    if series is None:
        raise ValidationError("The imported series no longer exists.")
    if series.preferred_library_root_id == root.id:
        return False

    old_root_id = series.preferred_library_root_id
    series.preferred_library_root_id = root.id
    await record_action(
        session,
        job,
        phase="import",
        action_type="series_preferred_root_updated",
        payload={
            "series_id": int(series.id),
            "old_preferred_library_root_id": old_root_id,
            "new_preferred_library_root_id": int(root.id),
        },
    )
    await session.flush()
    return True


async def _load_root_boundaries(session: AsyncSession) -> tuple[_RootBoundary, ...]:
    roots = list(
        (
            await session.execute(
                select(LibraryRoot)
                .where(
                    LibraryRoot.enabled.is_(True),
                    LibraryRoot.allow_referenced_registrations.is_(True),
                )
                .order_by(LibraryRoot.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return tuple(
        _RootBoundary(
            root_id=int(root.id),
            name=root.name,
            lexical_path=Path(root.path).expanduser().absolute(),
            resolved_path=Path(root.path).expanduser().resolve(strict=False),
        )
        for root in roots
    )


async def _load_selected_file_rows(
    session: AsyncSession,
    job_id: int,
) -> list[tuple[int, ImportSeriesStatus, int | None, int | None, int | None, str, str]]:
    result = await session.execute(
        select(
            ImportedSeries.id,
            ImportedSeries.status,
            ImportedSeries.series_id,
            ImportedSeries.cv_id,
            ImportedSeries.user_selected_cv_id,
            ImportedSeries.raw_series_name,
            ImportedFile.file_path,
        )
        .join(ImportedFile, ImportedFile.import_series_id == ImportedSeries.id)
        .where(
            ImportedSeries.import_job_id == job_id,
            ImportedFile.status.in_((ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED)),
            or_(
                and_(
                    ImportedSeries.status.in_(
                        (ImportSeriesStatus.MATCHED, ImportSeriesStatus.CONFIRMED)
                    ),
                    ImportedSeries.selected_for_import.is_(True),
                ),
                and_(
                    ImportedSeries.status == ImportSeriesStatus.DUPLICATE,
                    ImportedFile.include_in_import.is_(True),
                ),
            ),
        )
        .order_by(ImportedSeries.id.asc(), ImportedFile.id.asc())
    )
    return [
        (
            int(item_id),
            status,
            int(series_id) if series_id is not None else None,
            int(cv_id) if cv_id is not None else None,
            int(user_cv_id) if user_cv_id is not None else None,
            str(title),
            str(file_path),
        )
        for item_id, status, series_id, cv_id, user_cv_id, title, file_path in result.all()
    ]


async def _load_existing_series_by_comicvine_id(
    session: AsyncSession,
    comicvine_ids: set[int],
) -> dict[int, int]:
    if not comicvine_ids:
        return {}
    result = await session.execute(
        select(Series.comicvine_id, Series.id).where(Series.comicvine_id.in_(comicvine_ids))
    )
    return {
        int(comicvine_id): int(series_id)
        for comicvine_id, series_id in result.all()
        if comicvine_id is not None
    }


async def _load_existing_series_root_ids(
    session: AsyncSession,
    series_ids: set[int],
) -> dict[int, set[int]]:
    if not series_ids:
        return {}
    result = await session.execute(
        select(Issue.series_id, LibraryFile.library_root_id)
        .join(LibraryFile, LibraryFile.issue_id == Issue.id)
        .where(Issue.series_id.in_(series_ids))
    )
    roots_by_series: dict[int, set[int]] = {}
    for series_id, root_id in result.all():
        roots_by_series.setdefault(int(series_id), set()).add(int(root_id))
    return roots_by_series


def _containing_root_id(
    raw_path: str,
    boundaries: tuple[_RootBoundary, ...],
) -> int | None:
    path = Path(raw_path).expanduser()
    lexical_path = path.absolute()
    resolved_path = path.resolve(strict=False)
    candidates = [
        boundary
        for boundary in boundaries
        if (
            lexical_path == boundary.lexical_path
            or lexical_path.is_relative_to(boundary.lexical_path)
        )
        and (
            resolved_path == boundary.resolved_path
            or resolved_path.is_relative_to(boundary.resolved_path)
        )
    ]
    if len(candidates) != 1:
        return None
    return candidates[0].root_id


def _resolve_comicvine_id(cv_id: int | None, user_cv_id: int | None) -> int | None:
    value = user_cv_id if user_cv_id is not None else cv_id
    return int(value) if value is not None else None

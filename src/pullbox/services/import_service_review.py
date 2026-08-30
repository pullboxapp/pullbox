"""Step 3 file-review facade helpers for ``ImportService``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pullbox.core.exceptions import NotFoundError
from pullbox.models.import_job import ImportedSeries, ImportJob, ImportSeriesStatus
from pullbox.services.import_file_review import (
    override_file_match as override_import_file_match,
)
from pullbox.services.import_file_review import (
    repair_file_metadata as repair_import_file_metadata,
)
from pullbox.services.import_review_actions import (
    allow_safety_blocked_file_once as allow_import_safety_blocked_file_once,
)
from pullbox.services.import_review_actions import (
    bulk_update_file_selection as bulk_update_import_file_selection,
)
from pullbox.services.import_review_actions import (
    bulk_update_series_selection as bulk_update_import_series_selection,
)
from pullbox.services.import_review_actions import (
    reset_conflicts as reset_import_conflicts,
)
from pullbox.services.import_review_actions import (
    resolve_conflict as resolve_import_conflict,
)
from pullbox.services.import_review_actions import (
    resolve_conflicts as resolve_import_conflicts,
)
from pullbox.services.import_review_actions import (
    skip_safety_blocked_file as skip_import_safety_blocked_file,
)
from pullbox.services.import_review_actions import (
    unmatch_duplicate_series as unmatch_import_duplicate_series,
)
from pullbox.services.import_review_actions import (
    unmatch_series_match as unmatch_import_series_match,
)
from pullbox.services.import_review_actions import (
    update_file_selection as update_import_file_selection,
)
from pullbox.services.import_review_actions import (
    update_series_selection as update_import_series_selection,
)
from pullbox.services.import_review_queries import ConflictGroupsPage
from pullbox.services.import_review_queries import (
    get_conflict_groups as get_import_conflict_groups,
)
from pullbox.services.import_review_queries import (
    get_conflict_groups_page as get_import_conflict_groups_page,
)
from pullbox.services.import_review_queries import (
    get_files_for_series as get_import_files_for_series,
)
from pullbox.services.import_review_selection import load_import_review_selection_state
from pullbox.services.import_story_arc_review import (
    StoryArcReviewAction,
    update_import_story_arc_decision,
)

if TYPE_CHECKING:
    from typing import Protocol

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedFile, ImportedFileStatus
    from pullbox.models.issue import Issue
    from pullbox.models.story_arc_import import ImportedStoryArc

    class ImportServiceReviewContext(Protocol):
        _sync_import_file_source_metadata: Any

        async def _apply_manual_file_match_for_override(
            self,
            session: AsyncSession,
            imp_file: ImportedFile,
            issue: Issue,
        ) -> tuple[str, str | None]: ...

        async def _build_comicinfo_payload_for_issue(self, *args: Any, **kwargs: Any) -> Any: ...

        async def _rewrite_import_file_comicinfo(self, *args: Any, **kwargs: Any) -> Any: ...

        async def _recompute_file_counters_for_review_action(
            self,
            session: AsyncSession,
            job: ImportJob,
            series_ids: list[int],
        ) -> None: ...

        async def _recompute_file_counters(self, *args: Any, **kwargs: Any) -> None: ...

        async def _recompute_series_counters(self, *args: Any, **kwargs: Any) -> None: ...

        async def _log_event(
            self,
            session: AsyncSession,
            job_id: int,
            level: str,
            event: str,
            message: str | None = None,
            **kwargs: Any,
        ) -> None: ...

        async def repair_file_metadata(self, *args: Any, **kwargs: Any) -> Any: ...

else:
    ImportServiceReviewContext = object


class ImportServiceReviewMixin:
    """Mixin for Step 3 file queries, conflict choices, and selection actions."""

    async def get_files_for_series(
        self,
        session: AsyncSession,
        job_id: int,
        series_id: int,
        *,
        status_filter: ImportedFileStatus | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[ImportedFile], int]:
        """Return paginated files for a series within a job."""
        return await get_import_files_for_series(
            session,
            job_id,
            series_id,
            status_filter=status_filter,
            page=page,
            page_size=page_size,
        )

    async def get_conflict_groups(
        self,
        session: AsyncSession,
        job_id: int,
    ) -> list[dict[str, Any]]:
        """Return all conflict groups for a job."""
        return await get_import_conflict_groups(session, job_id)

    async def get_conflict_groups_page(
        self,
        session: AsyncSession,
        job_id: int,
        *,
        page: int = 1,
        page_size: int = 25,
        sort: str = "legacy",
    ) -> ConflictGroupsPage:
        """Return one bounded conflict-group page for request paths."""
        return await get_import_conflict_groups_page(
            session,
            job_id,
            page=page,
            page_size=page_size,
            sort=sort,
        )

    async def override_file_match(
        self: ImportServiceReviewContext,
        session: AsyncSession,
        job_id: int,
        file_id: int,
        issue_id: int,
        *,
        repair_source_metadata: bool = False,
    ) -> ImportedFile:
        """Manually set a file's issue match."""
        return await override_import_file_match(
            session,
            job_id,
            file_id,
            issue_id,
            repair_source_metadata=repair_source_metadata,
            repair_file_metadata=self.repair_file_metadata,
            apply_manual_file_match_func=self._apply_manual_file_match_for_override,
            recompute_file_counters=self._recompute_file_counters_for_review_action,
            log_event=self._log_event,
        )

    async def repair_file_metadata(
        self: ImportServiceReviewContext,
        session: AsyncSession,
        job_id: int,
        file_id: int,
        *,
        issue_id: int | None = None,
    ) -> ImportedFile:
        """Repair embedded ComicInfo metadata for an imported file."""
        return await repair_import_file_metadata(
            session,
            job_id,
            file_id,
            issue_id=issue_id,
            build_comicinfo_payload_for_issue=self._build_comicinfo_payload_for_issue,
            rewrite_import_file_comicinfo=self._rewrite_import_file_comicinfo,
            sync_import_file_source_metadata=self._sync_import_file_source_metadata,
            apply_manual_file_match_func=self._apply_manual_file_match_for_override,
            recompute_file_counters=self._recompute_file_counters_for_review_action,
            log_event=self._log_event,
        )

    async def resolve_conflict(
        self: ImportServiceReviewContext,
        session: AsyncSession,
        job_id: int,
        group_id: int,
        chosen_file_id: int,
    ) -> list[ImportedFile]:
        """Resolve a conflict group by choosing one file."""
        return await resolve_import_conflict(
            session,
            job_id,
            group_id,
            chosen_file_id,
            recompute_file_counters=self._recompute_file_counters_for_review_action,
        )

    async def resolve_conflicts(
        self: ImportServiceReviewContext,
        session: AsyncSession,
        job_id: int,
        resolutions: list[tuple[int, int]],
    ) -> tuple[list[int], list[int]]:
        """Resolve multiple conflict groups in a single review mutation."""
        resolved_group_ids, resolved_series_ids, _files_by_group = await resolve_import_conflicts(
            session,
            job_id,
            resolutions,
            recompute_file_counters=self._recompute_file_counters_for_review_action,
        )
        return resolved_group_ids, resolved_series_ids

    async def reset_conflicts(
        self: ImportServiceReviewContext,
        session: AsyncSession,
        job_id: int,
        group_ids: list[int],
    ) -> tuple[list[int], list[int]]:
        """Reset resolved conflict groups back to review state."""
        return await reset_import_conflicts(
            session,
            job_id,
            group_ids,
            recompute_file_counters=self._recompute_file_counters_for_review_action,
        )

    async def _recompute_file_counters_for_review_action(
        self: ImportServiceReviewContext,
        session: AsyncSession,
        job: ImportJob,
        series_ids: list[int],
    ) -> None:
        """Compatibility adapter for extracted review action helpers."""
        await self._recompute_file_counters(session, job, series_ids=series_ids)

    async def update_file_selection(
        self,
        session: AsyncSession,
        job_id: int,
        file_id: int,
        *,
        include_in_import: bool,
    ) -> ImportedFile:
        """Persist include/exclude state for an importable duplicate-series file."""
        return await update_import_file_selection(
            session,
            job_id,
            file_id,
            include_in_import=include_in_import,
        )

    async def bulk_update_file_selection(
        self,
        session: AsyncSession,
        job_id: int,
        *,
        include_in_import: bool,
        imported_series_id: int | None = None,
    ) -> int:
        """Persist include/exclude state for all importable duplicate-series files in scope."""
        return await bulk_update_import_file_selection(
            session,
            job_id,
            include_in_import=include_in_import,
            imported_series_id=imported_series_id,
        )

    async def update_series_selection(
        self,
        session: AsyncSession,
        job_id: int,
        imported_series_id: int,
        *,
        include_in_import: bool,
    ) -> ImportedSeries:
        """Persist include/exclude state for a matched review series."""
        return await update_import_series_selection(
            session,
            job_id,
            imported_series_id,
            include_in_import=include_in_import,
        )

    async def update_story_arc_decision(
        self,
        session: AsyncSession,
        job_id: int,
        imported_story_arc_id: int,
        *,
        action: StoryArcReviewAction,
        proposed_story_arc_id: int | None,
    ) -> ImportedStoryArc:
        """Persist one staged story-arc select/skip decision."""
        return await update_import_story_arc_decision(
            session,
            job_id,
            imported_story_arc_id,
            action=action,
            proposed_story_arc_id=proposed_story_arc_id,
        )

    async def allow_safety_blocked_file_once(
        self: ImportServiceReviewContext,
        session: AsyncSession,
        job_id: int,
        file_id: int,
    ) -> ImportedSeries:
        """Allow one oversized source file to re-enter normal matching for this job."""
        imp_file = await allow_import_safety_blocked_file_once(
            session,
            job_id,
            file_id,
            recompute_file_counters=self._recompute_file_counters_for_review_action,
            recompute_series_counters=self._recompute_series_counters,
        )
        imported_series = await session.get(ImportedSeries, imp_file.import_series_id)
        if imported_series is None:
            raise NotFoundError("ImportedSeries", imp_file.import_series_id)
        if imported_series.status in {ImportSeriesStatus.MATCHED, ImportSeriesStatus.DUPLICATE}:
            diagnostics = dict(imported_series.diagnostics or {})
            diagnostics["rematch_pending"] = True
            imported_series.diagnostics = diagnostics
            await session.flush()
        return imported_series

    async def allow_safety_blocked_file_once_for_retry(
        self: ImportServiceReviewContext,
        session: AsyncSession,
        job_id: int,
        file_id: int,
    ) -> tuple[ImportJob, ImportedSeries]:
        """Approve one resource safety exception and prepare that file for Step 4 retry."""
        imp_file = await allow_import_safety_blocked_file_once(
            session,
            job_id,
            file_id,
            recompute_file_counters=self._recompute_file_counters_for_review_action,
            recompute_series_counters=self._recompute_series_counters,
            retry_import=True,
        )
        job = await session.get(ImportJob, job_id)
        if job is None:
            raise NotFoundError("ImportJob", job_id)
        imported_series = await session.get(ImportedSeries, imp_file.import_series_id)
        if imported_series is None:
            raise NotFoundError("ImportedSeries", imp_file.import_series_id)
        await session.flush()
        return job, imported_series

    async def skip_safety_blocked_file(
        self: ImportServiceReviewContext,
        session: AsyncSession,
        job_id: int,
        file_id: int,
    ) -> ImportedSeries:
        """Skip one oversized source file from this active import review."""
        imp_file = await skip_import_safety_blocked_file(
            session,
            job_id,
            file_id,
            recompute_file_counters=self._recompute_file_counters_for_review_action,
            recompute_series_counters=self._recompute_series_counters,
        )
        imported_series = await session.get(ImportedSeries, imp_file.import_series_id)
        if imported_series is None:
            raise NotFoundError("ImportedSeries", imp_file.import_series_id)
        if (
            (imported_series.files_matched or 0) <= 0
            and (imported_series.files_conflict or 0) <= 0
            and (imported_series.files_no_match or 0) <= 0
        ):
            job = await session.get(ImportJob, job_id)
            if job is None:
                raise NotFoundError("ImportJob", job_id)
            imported_series.status = ImportSeriesStatus.SKIPPED
            imported_series.selected_for_import = False
            await self._recompute_series_counters(session, job)
            await session.flush()
        return imported_series

    async def unmatch_duplicate_series(
        self: ImportServiceReviewContext,
        session: AsyncSession,
        job_id: int,
        imported_series_id: int,
    ) -> ImportedSeries:
        """Reject an incorrect existing-library duplicate match in import review."""
        return await unmatch_import_duplicate_series(
            session,
            job_id,
            imported_series_id,
            recompute_file_counters=self._recompute_file_counters_for_review_action,
            recompute_series_counters=self._recompute_series_counters,
        )

    async def unmatch_series_match(
        self: ImportServiceReviewContext,
        session: AsyncSession,
        job_id: int,
        imported_series_id: int,
    ) -> ImportedSeries:
        """Reject an incorrect Step 3 series match in import review."""
        return await unmatch_import_series_match(
            session,
            job_id,
            imported_series_id,
            recompute_file_counters=self._recompute_file_counters_for_review_action,
            recompute_series_counters=self._recompute_series_counters,
        )

    async def bulk_update_series_selection(
        self,
        session: AsyncSession,
        job_id: int,
        *,
        include_in_import: bool,
        imported_series_ids: list[int] | None = None,
    ) -> int:
        """Persist include/exclude state for matched review series in scope."""
        return await bulk_update_import_series_selection(
            session,
            job_id,
            include_in_import=include_in_import,
            imported_series_ids=imported_series_ids,
        )

    async def get_review_selection_state(
        self,
        session: AsyncSession,
        job_id: int,
    ) -> dict[str, object]:
        """Return canonical Step 3 selection state."""
        job = await session.get(ImportJob, job_id)
        if job is None:
            raise NotFoundError("ImportJob", job_id)
        return await load_import_review_selection_state(session, job_id)

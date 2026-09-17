"""Durable background reconciliation of a single series folder."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import select

from pullbox.core.file_safety import (
    get_allowed_extensions,
    get_archive_size_limit_bytes,
    is_dangerous_file_blocking_enabled,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.services.series_rescan import plan_series_rescan
from pullbox.services.series_rescan_registration import apply_rescan_match
from pullbox.utilities.base_executor import (
    ApplyResult,
    ExecutionMode,
    ItemResult,
    JobExecutor,
    JobRunSummary,
    ProcessedItem,
)
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation


class SeriesRescanExecutor(JobExecutor):
    """Inspect off-loop, then apply bounded, revalidated database registrations."""

    execution_mode = ExecutionMode.THREAD

    def validate_config(self, job_config: dict[str, Any]) -> list[str]:
        series_id = job_config.get("series_id")
        if type(series_id) is not int or series_id <= 0 or set(job_config) != {"series_id"}:
            return ["A positive series_id is required; paths are derived from the catalog."]
        return []

    async def build_job_context(self, session: Any, job_config: dict[str, Any]) -> dict[str, Any]:
        errors = self.validate_config(job_config)
        if errors:
            raise ValueError(errors[0])
        await ensure_no_active_import_file_mutation(session)
        series = await session.get(Series, job_config["series_id"])
        if series is None or not series.path:
            raise ValueError(
                "This series has no configured folder. Set its folder before rescanning."
            )
        roots = list(
            (
                await session.scalars(
                    select(LibraryRoot).where(
                        LibraryRoot.enabled.is_(True),
                        LibraryRoot.allow_referenced_registrations.is_(True),
                    )
                )
            ).all()
        )
        issues = list(
            (await session.scalars(select(Issue).where(Issue.series_id == series.id))).all()
        )
        files = list(
            (
                await session.scalars(
                    select(LibraryFile).join(Issue).where(Issue.series_id == series.id)
                )
            ).all()
        )
        return {
            "folder": series.path,
            "roots": [{"id": root.id, "path": root.path} for root in roots],
            "series": {
                "id": series.id,
                "title": series.title,
                "year_start": series.year_start,
                "comicvine_id": series.comicvine_id,
            },
            "issues": [
                {
                    "id": issue.id,
                    "number": issue.issue_number,
                    "text": issue.issue_number_text,
                    "cv_id": issue.comicvine_id,
                    "type": issue.issue_type.value,
                    "title": issue.title,
                    "year": issue.release_date.year if issue.release_date else None,
                }
                for issue in issues
            ],
            "files": [
                {"id": record.id, "path": record.file_path, "issue_id": record.issue_id}
                for record in files
            ],
            "block_dangerous": await is_dangerous_file_blocking_enabled(session),
            "extensions": list(await get_allowed_extensions(session)),
            "max_archive_size": await get_archive_size_limit_bytes(session),
        }

    async def generate_items(
        self, job_config: dict[str, Any], job_context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(plan_series_rescan, job_context or {})

    def process_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        return ProcessedItem(item_id=item_data["id"], result=ItemResult.COMPLETED)

    async def apply_item_result(
        self,
        session: Any,
        item: Any,
        item_data: dict[str, Any],
        processed: ProcessedItem,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None,
        summary: JobRunSummary,
    ) -> ApplyResult:
        outcome, reason = await apply_rescan_match(session, job_config["series_id"], item_data)
        item.after_state = json.dumps({"outcome": outcome, "reason": reason})
        return ApplyResult(
            warning_increment=int(outcome == "review"),
            warning_message=reason if outcome == "review" else None,
        )

    def rollback_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        return ProcessedItem(
            item_id=item_data["id"],
            result=ItemResult.FAILED,
            error_message="Rescans do not support rollback; source files were not changed.",
        )

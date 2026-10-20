"""Durable, selected-field propagation using the existing file writer and job lane."""

import hashlib
import json
from collections import Counter
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import JSON, Text, case, cast, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.models import Issue, LibraryFile
from pullbox.schemas.series_metadata_propagation import (
    PropagationPlan,
    PropagationReview,
    PropagationTarget,
)
from pullbox.services.metadata_series_refresh_state import (
    RefreshEntityState,
    read_series_refresh_state,
)
from pullbox.utilities.models import JobType, UtilityJob, UtilityJobItem

KEY = "series_propagation"


def read_plan(config: dict[str, Any]) -> PropagationPlan:
    if set(config) != {KEY}:
        raise ValueError("Invalid propagation job")
    plan = PropagationPlan.model_validate(config[KEY])
    if len({t.issue_id for t in plan.targets}) != len(plan.targets) or any(
        (t.review_key is not None) != (plan.mode == "write") for t in plan.targets
    ):
        raise ValueError("Invalid propagation targets")
    return plan


async def series_key(session: AsyncSession, series_id: int) -> str:
    state = await read_series_refresh_state(session, series_id, include_issues=False)
    return hashlib.sha256(TypeAdapter(RefreshEntityState).dump_json(state.series)).hexdigest()


async def preview_plan(
    session: AsyncSession, series_id: int, review: PropagationReview
) -> PropagationPlan:
    key = await series_key(session, series_id)
    rows = (
        await session.execute(
            select(Issue.id, Issue.issue_number_text)
            .where(
                Issue.series_id == series_id,
                select(LibraryFile.id).where(LibraryFile.issue_id == Issue.id).exists(),
            )
            .order_by(Issue.issue_number, Issue.id)
            .limit(10001)
        )
    ).all()
    if not rows:
        raise ValueError("This series has no registered comic files. Import an issue first.")
    if len(rows) > 10000:
        raise ValueError(
            "This series exceeds the 10,000-issue preview limit. Update issues individually."
        )
    return PropagationPlan(
        series_id=series_id,
        series_key=key,
        fields=review.fields,
        mode="preview",
        targets=tuple(
            PropagationTarget(
                issue_id=row.id, label=f"Issue {row.issue_number_text}", operation_id=uuid4().hex
            )
            for row in rows
        ),
    )


async def latest_job(session: AsyncSession, series_id: int) -> UtilityJob | None:
    return await session.scalar(
        select(UtilityJob)
        .where(
            UtilityJob.job_type == JobType.FILE_METADATA,
            type_coerce(cast(UtilityJob.config, JSON().with_variant(Text(), "sqlite")), JSON)[KEY][
                "series_id"
            ].as_integer()
            == series_id,
        )
        .order_by(UtilityJob.created_at.desc(), UtilityJob.id.desc())
        .limit(1)
    )


async def job_items(session: AsyncSession, job: UtilityJob) -> list[UtilityJobItem]:
    return list(
        await session.scalars(
            select(UtilityJobItem)
            .where(UtilityJobItem.job_id == job.id)
            .order_by(UtilityJobItem.item_index)
        )
    )


async def approved_plan(
    session: AsyncSession,
    series_id: int,
    job_id: str,
    selected: list[int] | None,
    *,
    retry: bool = False,
) -> PropagationPlan:
    job = await session.get(UtilityJob, job_id)
    if job is None or job.job_type != JobType.FILE_METADATA or KEY not in json.loads(job.config):
        raise ValueError("Open a completed comic metadata preview first.")
    plan = read_plan(json.loads(job.config))
    if (
        plan.series_id != series_id
        or not job.is_terminal
        or plan.mode != ("write" if retry else "preview")
    ):
        raise ValueError("Wait for this series' preview or write to finish before continuing.")
    targets = []
    for item in await job_items(session, job):
        data = json.loads(item.after_state or "{}")
        before = json.loads(item.before_state or "{}")
        original = plan.targets[before["target_index"]]
        if retry:
            if item.state != "COMPLETED":
                targets.append(original)
        elif data.get("outcome") == "ready":
            targets.append(original.model_copy(update={"review_key": data["review_key"]}))
    if selected is not None:
        if len(set(selected)) != len(selected) or not set(selected) <= {
            t.issue_id for t in targets
        }:
            raise ValueError("Select only ready issues from this completed preview.")
        targets = [target for target in targets if target.issue_id in selected]
    if not targets:
        raise ValueError(
            "No unfinished eligible files are selected. Preview again or review the issue details."
        )
    return plan.model_copy(update={"mode": "write", "targets": tuple(targets)})


async def job_details(session: AsyncSession, job: UtilityJob, page: int) -> dict[str, object]:
    plan = read_plan(json.loads(job.config))
    document = JSON().with_variant(Text(), "sqlite")
    before = type_coerce(cast(UtilityJobItem.before_state, document), JSON)
    after = type_coerce(cast(UtilityJobItem.after_state, document), JSON)
    # One snapshot for totals and the visible page; do not load every XML diff on each poll.
    items = (
        await session.execute(
            select(
                UtilityJobItem.item_index,
                UtilityJobItem.state,
                UtilityJobItem.error_message,
                before["target_index"].as_integer().label("target_index"),
                after["outcome"].as_string().label("outcome"),
                case(
                    (
                        UtilityJobItem.item_index.between((page - 1) * 20, page * 20 - 1),
                        UtilityJobItem.after_state,
                    ),
                    else_=None,
                ).label("details"),
            )
            .where(UtilityJobItem.job_id == job.id)
            .order_by(UtilityJobItem.item_index)
        )
    ).all()
    counts: Counter[str] = Counter()
    targets = []
    ready_ids = []
    for item in items:
        target = plan.targets[item.target_index]
        outcome = item.outcome or ("attention" if item.error_message else item.state.lower())
        counts[outcome] += 1
        if outcome == "ready":
            ready_ids.append(target.issue_id)
        if not (page - 1) * 20 <= item.item_index < page * 20:
            continue
        details = json.loads(item.details or "{}")
        targets.append(
            {
                "issue_id": target.issue_id,
                "label": target.label,
                "outcome": outcome,
                "file_name": details.get("file_name", ""),
                "changes": details.get("changes", []),
                "reason": item.error_message or details.get("reason", ""),
            }
        )
    return {
        "mode": plan.mode,
        "fields": sorted(plan.fields),
        "counts": dict(counts),
        "targets": targets,
        "page": page,
        "pages": max(1, (len(items) + 19) // 20),
        "ready_ids": ready_ids,
        "retryable": plan.mode == "write"
        and job.is_terminal
        and any(i.state != "COMPLETED" for i in items),
    }

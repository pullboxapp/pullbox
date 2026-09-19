"""Utility job queue item payload helpers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pullbox.utilities.base_executor import ItemResult
from pullbox.utilities.models import ItemState, UtilityJobItem

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


@dataclass(frozen=True, slots=True)
class BatchPayloads:
    """Payload indexes prepared for one worker-pool batch."""

    payloads: list[dict[str, Any]]
    items_by_id: dict[str, UtilityJobItem]
    payloads_by_id: dict[str, dict[str, Any]]


def item_result_to_state(result: object) -> ItemState:
    """Map an executor item result to the persisted utility item state."""
    if not isinstance(result, ItemResult):
        return ItemState.FAILED
    result_states: dict[ItemResult, ItemState] = {
        ItemResult.COMPLETED: ItemState.COMPLETED,
        ItemResult.FAILED: ItemState.FAILED,
        ItemResult.SKIPPED: ItemState.SKIPPED,
        ItemResult.CANCELLED: ItemState.PENDING,
    }
    return result_states.get(result, ItemState.FAILED)


def build_generated_job_items(
    *,
    job_id: str,
    items_data: Iterable[dict[str, Any]],
    item_id_factory: Callable[[], str] | None = None,
) -> list[UtilityJobItem]:
    """Build pending DB item rows from executor-generated payloads."""
    make_item_id = item_id_factory or (lambda: os.urandom(16).hex())
    return [
        UtilityJobItem(
            id=make_item_id(),
            job_id=job_id,
            item_index=idx,
            state=ItemState.PENDING,
            file_path=item_data.get("file_path"),
            operation=item_data.get("operation", "unknown"),
            before_state=json.dumps(item_data),
        )
        for idx, item_data in enumerate(items_data)
    ]


def build_batch_payloads(batch_items: Iterable[UtilityJobItem]) -> BatchPayloads:
    """Build executor payloads and lookup maps for a batch of DB items."""
    payloads: list[dict[str, Any]] = []
    items_by_id: dict[str, UtilityJobItem] = {}
    payloads_by_id: dict[str, dict[str, Any]] = {}
    for db_item in batch_items:
        if db_item.before_state and db_item.before_state != "{}":
            try:
                item_data = json.loads(db_item.before_state)
            except (json.JSONDecodeError, TypeError):
                item_data = {}
            item_data["id"] = db_item.id
        else:
            item_data = {
                "id": db_item.id,
                "file_path": db_item.file_path,
                "operation": db_item.operation,
            }
        payloads.append(item_data)
        items_by_id[db_item.id] = db_item
        payloads_by_id[db_item.id] = item_data
    return BatchPayloads(
        payloads=payloads,
        items_by_id=items_by_id,
        payloads_by_id=payloads_by_id,
    )

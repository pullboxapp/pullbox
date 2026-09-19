"""Tests for UT-F.5 — base executor interface.

Verifies ProcessedItem pickling, ItemResult enum, JobExecutor ABC
contract (cannot instantiate directly, subclass validation), and
edge cases (large state dicts, unicode, non-JSON types, duration bounds).

Run:
    pytest tests/utilities/test_base_executor.py -v
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

import pytest

from pullbox.utilities.base_executor import (
    ItemResult,
    JobExecutor,
    ProcessedItem,
)

# ── ItemResult Enum ───────────────────────────────────────────


class TestItemResult:
    """Verify ItemResult enum values."""

    def test_values(self) -> None:
        assert ItemResult.COMPLETED == "completed"
        assert ItemResult.FAILED == "failed"
        assert ItemResult.SKIPPED == "skipped"
        assert ItemResult.CANCELLED == "cancelled"
        assert len(ItemResult) == 4


# ── ProcessedItem Pickling ─────────────────────────────────────


class TestProcessedItemPickling:
    """Verify ProcessedItem survives pickle roundtrip for worker process IPC."""

    def test_minimal_fields_picklable(self) -> None:
        item = ProcessedItem(item_id="item-001", result=ItemResult.COMPLETED)
        restored = pickle.loads(pickle.dumps(item))
        assert restored.item_id == "item-001"
        assert restored.result == ItemResult.COMPLETED
        assert restored.before_state == {}
        assert restored.after_state == {}
        assert restored.duration_ms == 0
        assert restored.error_message is None
        assert restored.warning_message is None
        assert restored.log_entries == []

    def test_all_fields_populated_roundtrips(self) -> None:
        item = ProcessedItem(
            item_id="item-002",
            result=ItemResult.FAILED,
            before_state={"path": "/comics/batman.cbr", "size": 52428800},
            after_state={"path": "/comics/batman.cbz", "size": 48000000},
            duration_ms=1500,
            error_message="Corrupt archive header",
            warning_message="File size reduced by 8%",
            log_entries=[
                ("INFO", "Starting conversion", {"pages": 32}),
                ("ERROR", "Failed at page 15", {"page": 15}),
            ],
        )
        restored = pickle.loads(pickle.dumps(item))
        assert restored.item_id == "item-002"
        assert restored.result == ItemResult.FAILED
        assert restored.before_state["path"] == "/comics/batman.cbr"
        assert restored.after_state["size"] == 48000000
        assert restored.duration_ms == 1500
        assert restored.error_message == "Corrupt archive header"
        assert restored.warning_message == "File size reduced by 8%"
        assert len(restored.log_entries) == 2

    def test_none_optional_fields_picklable(self) -> None:
        item = ProcessedItem(
            item_id="item-003",
            result=ItemResult.SKIPPED,
            error_message=None,
            warning_message=None,
        )
        restored = pickle.loads(pickle.dumps(item))
        assert restored.error_message is None
        assert restored.warning_message is None

    def test_very_large_before_state(self) -> None:
        """50,000-key before_state dict (~1MB) pickles without error."""
        large_state = {f"key_{i}": f"value_{i}" for i in range(50000)}
        item = ProcessedItem(
            item_id="item-004",
            result=ItemResult.COMPLETED,
            before_state=large_state,
        )
        restored = pickle.loads(pickle.dumps(item))
        assert len(restored.before_state) == 50000

    def test_non_ascii_strings(self) -> None:
        """Unicode strings in all fields roundtrip correctly."""
        item = ProcessedItem(
            item_id="item-\u65e5\u672c",
            result=ItemResult.COMPLETED,
            before_state={"series": "\u9032\u6483\u306e\u5de8\u4eba"},
            after_state={"path": "/comics/\u30d0\u30c3\u30c8\u30de\u30f3.cbz"},
            error_message="\u2014 conversion failed \u201chere\u201d",
            log_entries=[("INFO", "\u30d5\u30a1\u30a4\u30eb converted", {})],
        )
        restored = pickle.loads(pickle.dumps(item))
        assert "\u9032\u6483\u306e\u5de8\u4eba" in restored.before_state["series"]
        assert "\u30d0\u30c3\u30c8\u30de\u30f3" in restored.after_state["path"]

    def test_empty_log_entries(self) -> None:
        item = ProcessedItem(
            item_id="item-005",
            result=ItemResult.COMPLETED,
            log_entries=[],
        )
        restored = pickle.loads(pickle.dumps(item))
        assert restored.log_entries == []

    def test_many_log_entries(self) -> None:
        """500 log entries pickle correctly."""
        entries: list[tuple[str, str, dict[str, Any]]] = [
            ("INFO", f"Step {i}", {"step": i}) for i in range(500)
        ]
        item = ProcessedItem(
            item_id="item-006",
            result=ItemResult.COMPLETED,
            log_entries=entries,
        )
        restored = pickle.loads(pickle.dumps(item))
        assert len(restored.log_entries) == 500

    def test_deeply_nested_extra_in_log_entries(self) -> None:
        """Log entry extra with 5 levels of nesting pickles correctly."""
        nested: dict[str, Any] = {"l1": {"l2": {"l3": {"l4": {"l5": "deep"}}}}}
        item = ProcessedItem(
            item_id="item-007",
            result=ItemResult.COMPLETED,
            log_entries=[("INFO", "deep", nested)],
        )
        restored = pickle.loads(pickle.dumps(item))
        assert restored.log_entries[0][2]["l1"]["l2"]["l3"]["l4"]["l5"] == "deep"

    def test_state_with_path_objects(self) -> None:
        """Path objects in before_state/after_state are picklable."""
        item = ProcessedItem(
            item_id="item-008",
            result=ItemResult.COMPLETED,
            before_state={"path": Path("/comics/batman.cbr")},
            after_state={"path": Path("/comics/batman.cbz")},
        )
        restored = pickle.loads(pickle.dumps(item))
        assert restored.before_state["path"] == Path("/comics/batman.cbr")

    def test_duration_ms_zero(self) -> None:
        item = ProcessedItem(
            item_id="item-009",
            result=ItemResult.COMPLETED,
            duration_ms=0,
        )
        assert pickle.loads(pickle.dumps(item)).duration_ms == 0

    def test_duration_ms_very_large(self) -> None:
        """Multi-hour job duration (7,200,000ms = 2 hours)."""
        item = ProcessedItem(
            item_id="item-010",
            result=ItemResult.COMPLETED,
            duration_ms=7200000,
        )
        assert pickle.loads(pickle.dumps(item)).duration_ms == 7200000


# ── JobExecutor ABC ────────────────────────────────────────────


class TestJobExecutorABC:
    """Verify JobExecutor abstract contract."""

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError, match="abstract"):
            JobExecutor()  # type: ignore[abstract]

    def test_subclass_missing_generate_items_raises(self) -> None:
        class Partial(JobExecutor):
            def process_item(
                self, item_data: dict[str, Any], job_config: dict[str, Any]
            ) -> ProcessedItem:
                return ProcessedItem(item_id="x", result=ItemResult.COMPLETED)

            def rollback_item(
                self, item_data: dict[str, Any], job_config: dict[str, Any]
            ) -> ProcessedItem:
                return ProcessedItem(item_id="x", result=ItemResult.COMPLETED)

        with pytest.raises(TypeError):
            Partial()  # type: ignore[abstract]

    def test_subclass_missing_process_item_raises(self) -> None:
        class Partial(JobExecutor):
            async def generate_items(self, job_config: dict[str, Any]) -> list[dict[str, Any]]:
                return []

            def rollback_item(
                self, item_data: dict[str, Any], job_config: dict[str, Any]
            ) -> ProcessedItem:
                return ProcessedItem(item_id="x", result=ItemResult.COMPLETED)

        with pytest.raises(TypeError):
            Partial()  # type: ignore[abstract]

    def test_subclass_missing_rollback_item_raises(self) -> None:
        class Partial(JobExecutor):
            async def generate_items(self, job_config: dict[str, Any]) -> list[dict[str, Any]]:
                return []

            def process_item(
                self, item_data: dict[str, Any], job_config: dict[str, Any]
            ) -> ProcessedItem:
                return ProcessedItem(item_id="x", result=ItemResult.COMPLETED)

        with pytest.raises(TypeError):
            Partial()  # type: ignore[abstract]

    def test_complete_subclass_instantiates(self) -> None:
        class Complete(JobExecutor):
            async def generate_items(self, job_config: dict[str, Any]) -> list[dict[str, Any]]:
                return []

            def process_item(
                self, item_data: dict[str, Any], job_config: dict[str, Any]
            ) -> ProcessedItem:
                return ProcessedItem(item_id="x", result=ItemResult.COMPLETED)

            def rollback_item(
                self, item_data: dict[str, Any], job_config: dict[str, Any]
            ) -> ProcessedItem:
                return ProcessedItem(item_id="x", result=ItemResult.COMPLETED)

        executor = Complete()
        assert isinstance(executor, JobExecutor)


# ── validate_config Default ────────────────────────────────────


class TestValidateConfig:
    """Verify default validate_config behavior."""

    def _make_executor(self) -> JobExecutor:
        class Stub(JobExecutor):
            async def generate_items(self, job_config: dict[str, Any]) -> list[dict[str, Any]]:
                return []

            def process_item(
                self, item_data: dict[str, Any], job_config: dict[str, Any]
            ) -> ProcessedItem:
                return ProcessedItem(item_id="x", result=ItemResult.COMPLETED)

            def rollback_item(
                self, item_data: dict[str, Any], job_config: dict[str, Any]
            ) -> ProcessedItem:
                return ProcessedItem(item_id="x", result=ItemResult.COMPLETED)

        return Stub()

    def test_default_returns_empty_list(self) -> None:
        executor = self._make_executor()
        assert executor.validate_config({"key": "value"}) == []

    def test_default_with_empty_dict(self) -> None:
        executor = self._make_executor()
        assert executor.validate_config({}) == []

    def test_default_with_deeply_nested_config(self) -> None:
        executor = self._make_executor()
        config: dict[str, Any] = {"a": {"b": {"c": {"d": {"e": "deep"}}}}}
        assert executor.validate_config(config) == []

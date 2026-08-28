"""Shared runtime helpers for download post-processing phases and summaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from pullbox.tasks.post_processing_progress import (
        PostProcessingPhase,
        PostProcessingRunTrace,
    )


@dataclass(frozen=True)
class PostProcessingRuntime:
    """Small wrapper around phase progress and lifecycle-summary emission."""

    download: Any
    trace: PostProcessingRunTrace
    log: Any
    summary_logger: Any
    set_phase: Callable[[int, PostProcessingPhase], None]
    publish_phase: Callable[[Any, PostProcessingPhase], None] | None = None

    def enter_phase(self, phase: PostProcessingPhase) -> None:
        """Enter a phase and update the live post-processing progress snapshot."""
        self.trace.enter_phase(phase)
        self.set_phase(self.download.id, phase)
        if self.publish_phase is not None:
            self.publish_phase(self.download, phase)
        self.log.debug(
            "post_processing_phase_entered",
            download_id=self.download.id,
            issue_id=self.download.issue_id,
            phase=phase.value,
            phase_label=phase.label,
        )

    def emit_summary(
        self,
        *,
        outcome: str,
        error_message: str | None = None,
    ) -> None:
        """Emit the post-processing lifecycle summary event."""
        self.summary_logger.info(
            "post_processing_lifecycle_summary",
            download_id=self.download.id,
            issue_id=self.download.issue_id,
            download_client=str(self.download.download_client.value),
            outcome=outcome,
            error_classification=self.trace.error_classification,
            error_message=error_message,
            source_path=self.trace.source_path,
            probe_root=self.trace.probe_root,
            final_path=self.trace.final_path or self.download.final_path,
            transfer_method=self.trace.transfer_method,
            configured_transfer_method=self.trace.configured_transfer_method,
            effective_transfer_method=self.trace.effective_transfer_method,
            torrent_import_strategy=self.trace.torrent_import_strategy,
            seed_safe_torrent_import=self.trace.seed_safe_torrent_import,
            source_preserved=self.trace.source_preserved,
            file_size_bytes=self.trace.file_size_bytes,
            transferred_bytes=self.trace.transferred_bytes,
            **self.trace.summary_fields(),
        )

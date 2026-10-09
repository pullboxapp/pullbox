"""In-memory post-processing progress snapshots for download imports."""

from __future__ import annotations

import enum
import time as _time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class PostProcessingPhase(enum.StrEnum):
    """Stable post-processing phases shared by task orchestration and the UI."""

    RESOLVING_SOURCE = "resolving_source"
    VALIDATING_FILES = "validating_files"
    PREPARING_DESTINATION = "preparing_destination"
    TRANSFERRING_FILE = "transferring_file"
    REGISTERING_LIBRARY_FILE = "registering_library_file"
    WRITING_METADATA = "writing_metadata"
    IMPORT_COMPLETE = "import_complete"

    @property
    def label(self) -> str:
        return {
            PostProcessingPhase.RESOLVING_SOURCE: "Resolving source",
            PostProcessingPhase.VALIDATING_FILES: "Validating files",
            PostProcessingPhase.PREPARING_DESTINATION: "Preparing destination",
            PostProcessingPhase.TRANSFERRING_FILE: "Transferring file",
            PostProcessingPhase.REGISTERING_LIBRARY_FILE: "Registering library file",
            PostProcessingPhase.WRITING_METADATA: "Writing file metadata",
            PostProcessingPhase.IMPORT_COMPLETE: "Import complete",
        }[self]

    @property
    def status_label(self) -> str:
        return "Transferring" if self is PostProcessingPhase.TRANSFERRING_FILE else self.label

    @property
    def shows_transfer_metrics(self) -> bool:
        return self is PostProcessingPhase.TRANSFERRING_FILE


@dataclass(frozen=True)
class PostProcessingSnapshot:
    """Point-in-time post-processing status for the queue UI."""

    phase: PostProcessingPhase
    started_at_epoch: float
    updated_at_epoch: float
    phase_started_at_epoch: float
    state_tone: str = "active"
    visible_until_epoch: float | None = None
    transfer_total_bytes: int | None = None
    transfer_done_bytes: int | None = None
    transfer_speed_bytes: int | None = None
    transfer_eta_seconds: int | None = None

    @property
    def phase_label(self) -> str:
        return self.phase.label


@dataclass
class PostProcessingRunTrace:
    """Timing and summary state for one post-processing execution."""

    download_id: int
    started_monotonic: float = field(default_factory=_time.monotonic)
    current_phase: PostProcessingPhase = PostProcessingPhase.RESOLVING_SOURCE
    phase_started_monotonic: float = field(default_factory=_time.monotonic)
    phase_timings_ms: dict[str, float] = field(default_factory=dict)
    source_path: str | None = None
    probe_root: str | None = None
    final_path: str | None = None
    transfer_method: str | None = None
    configured_transfer_method: str | None = None
    effective_transfer_method: str | None = None
    torrent_import_strategy: str | None = None
    seed_safe_torrent_import: bool | None = None
    source_preserved: bool | None = None
    file_size_bytes: int | None = None
    transferred_bytes: int | None = None
    safety_ms: float | None = None
    integrity_ms: float | None = None
    cleanup_ms: float | None = None
    error_classification: str | None = None

    def enter_phase(self, phase: PostProcessingPhase) -> None:
        if phase == self.current_phase and not self.phase_timings_ms:
            self.phase_started_monotonic = _time.monotonic()
            return
        self.finalize_current_phase()
        self.current_phase = phase
        self.phase_started_monotonic = _time.monotonic()

    def finalize_current_phase(self) -> None:
        phase_key = _POST_PROCESSING_PHASE_TIMING_KEYS.get(self.current_phase)
        if phase_key is None or phase_key in self.phase_timings_ms:
            return
        self.phase_timings_ms[phase_key] = round(
            (_time.monotonic() - self.phase_started_monotonic) * 1000,
            1,
        )

    def summary_fields(self) -> dict[str, object]:
        return {
            "source_probe_ms": self.phase_timings_ms.get("source_probe_ms"),
            "safety_ms": self.safety_ms,
            "integrity_ms": self.integrity_ms,
            "destination_prep_ms": self.phase_timings_ms.get("destination_prep_ms"),
            "transfer_ms": self.phase_timings_ms.get("transfer_ms"),
            "register_ms": self.phase_timings_ms.get("register_ms"),
            "metadata_ms": self.phase_timings_ms.get("metadata_ms"),
            "cleanup_ms": self.cleanup_ms,
            "post_processing_duration_ms": round(
                (_time.monotonic() - self.started_monotonic) * 1000,
                1,
            ),
        }


_POST_PROCESSING_PHASE_TIMING_KEYS: dict[PostProcessingPhase, str | None] = {
    PostProcessingPhase.RESOLVING_SOURCE: "source_probe_ms",
    PostProcessingPhase.VALIDATING_FILES: None,
    PostProcessingPhase.PREPARING_DESTINATION: "destination_prep_ms",
    PostProcessingPhase.TRANSFERRING_FILE: "transfer_ms",
    PostProcessingPhase.REGISTERING_LIBRARY_FILE: "register_ms",
    PostProcessingPhase.WRITING_METADATA: "metadata_ms",
    PostProcessingPhase.IMPORT_COMPLETE: None,
}

_post_processing_cache: dict[int, PostProcessingSnapshot] = {}
_POST_PROCESSING_COMPLETION_GRACE_SECONDS = 4.0


def _infer_effective_post_processing_transfer_method(
    *,
    source_path: Path,
    destination_path: Path,
    configured_transfer_method: str,
    seed_safe_torrent_import: bool,
) -> str:
    """Infer the effective method used by seed-safe materialization."""
    if not seed_safe_torrent_import:
        return configured_transfer_method
    if not source_path.exists() or not destination_path.exists():
        return configured_transfer_method
    try:
        return "hardlink" if source_path.samefile(destination_path) else "copy"
    except OSError:
        return "copy"


def get_all_post_processing_progress() -> dict[int, PostProcessingSnapshot]:
    """Return a shallow copy of live post-processing snapshots."""
    now = _time.time()
    expired_ids = [
        download_id
        for download_id, snapshot in _post_processing_cache.items()
        if snapshot.visible_until_epoch is not None and snapshot.visible_until_epoch <= now
    ]
    for download_id in expired_ids:
        _post_processing_cache.pop(download_id, None)
    return dict(_post_processing_cache)


def _set_post_processing_phase(download_id: int, phase: PostProcessingPhase) -> None:
    """Track the current post-processing phase for a download."""
    now = _time.time()
    existing = _post_processing_cache.get(download_id)
    _post_processing_cache[download_id] = PostProcessingSnapshot(
        phase=phase,
        started_at_epoch=existing.started_at_epoch if existing else now,
        updated_at_epoch=now,
        phase_started_at_epoch=now,
        state_tone="active",
        transfer_total_bytes=existing.transfer_total_bytes if existing else None,
        transfer_done_bytes=existing.transfer_done_bytes if existing else None,
        transfer_speed_bytes=existing.transfer_speed_bytes if existing else None,
        transfer_eta_seconds=existing.transfer_eta_seconds if existing else None,
    )


def _set_post_processing_transfer_progress(
    download_id: int,
    *,
    total_bytes: int,
    done_bytes: int,
) -> None:
    """Track byte-level progress for a file transfer phase."""
    now = _time.time()
    existing = _post_processing_cache.get(download_id)
    safe_total = max(total_bytes, 0)
    safe_done = min(max(done_bytes, 0), safe_total) if safe_total else 0

    previous_done = existing.transfer_done_bytes if existing else None
    previous_updated = existing.updated_at_epoch if existing else None
    previous_speed = existing.transfer_speed_bytes if existing else None

    speed_bytes: int | None = previous_speed
    if previous_done is not None and previous_updated is not None:
        delta_time = now - previous_updated
        delta_bytes = max(0, safe_done - previous_done)
        if delta_time > 0 and delta_bytes > 0:
            instant_speed = int(delta_bytes / delta_time)
            if previous_speed and previous_speed > 0:
                speed_bytes = int((previous_speed * 0.4) + (instant_speed * 0.6))
            else:
                speed_bytes = instant_speed

    eta_seconds: int | None = None
    if safe_done >= safe_total and safe_total > 0:
        eta_seconds = 0
    elif speed_bytes and speed_bytes > 0 and safe_total > 0:
        remaining = max(safe_total - safe_done, 0)
        eta_seconds = max(1, int(remaining / speed_bytes))

    _post_processing_cache[download_id] = PostProcessingSnapshot(
        phase=PostProcessingPhase.TRANSFERRING_FILE,
        started_at_epoch=existing.started_at_epoch if existing else now,
        updated_at_epoch=now,
        phase_started_at_epoch=(
            existing.phase_started_at_epoch
            if existing and existing.phase is PostProcessingPhase.TRANSFERRING_FILE
            else now
        ),
        state_tone="active",
        transfer_total_bytes=safe_total or None,
        transfer_done_bytes=safe_done if safe_total else None,
        transfer_speed_bytes=speed_bytes,
        transfer_eta_seconds=eta_seconds,
    )


def _mark_post_processing_complete(download_id: int) -> None:
    """Keep a just-finished import visible briefly so the UI can show completion."""
    now = _time.time()
    existing = _post_processing_cache.get(download_id)
    total_bytes = existing.transfer_total_bytes if existing else None
    done_bytes = total_bytes if total_bytes is not None else None
    _post_processing_cache[download_id] = PostProcessingSnapshot(
        phase=PostProcessingPhase.IMPORT_COMPLETE,
        started_at_epoch=existing.started_at_epoch if existing else now,
        updated_at_epoch=now,
        phase_started_at_epoch=now,
        state_tone="success",
        visible_until_epoch=now + _POST_PROCESSING_COMPLETION_GRACE_SECONDS,
        transfer_total_bytes=total_bytes,
        transfer_done_bytes=done_bytes,
        transfer_speed_bytes=None,
        transfer_eta_seconds=0 if total_bytes is not None else None,
    )


def _clear_post_processing(download_id: int) -> None:
    """Remove a download from the post-processing snapshot cache."""
    _post_processing_cache.pop(download_id, None)

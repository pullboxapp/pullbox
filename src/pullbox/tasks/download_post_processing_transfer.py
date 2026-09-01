"""Transfer helpers for download post-processing."""

from __future__ import annotations

from asyncio import get_running_loop
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pullbox.core.library_root_resolution import preferred_managed_root_id
from pullbox.models.library import MatchConfidence
from pullbox.tasks.post_processing_progress import PostProcessingPhase

RegisterLibraryFile = Callable[..., Awaitable[Any]]
SetTransferProgress = Callable[..., None]
InferEffectiveTransferMethod = Callable[..., str]


async def transfer_and_register_library_file(
    *,
    session: Any,
    comic_file: Path,
    issue: Any,
    series: Any,
    download: Any,
    ingest_policy: Any,
    trace: Any,
    runtime: Any,
    log: Any,
    register_library_file: RegisterLibraryFile,
    set_transfer_progress: SetTransferProgress,
    infer_effective_transfer_method: InferEffectiveTransferMethod,
    replacement_trash_dir: Path | None = None,
) -> Path:
    """Transfer the completed download into the library and update trace state."""
    method = ingest_policy.post_processing_method
    trace.transfer_method = method
    trace.configured_transfer_method = method
    runtime.enter_phase(PostProcessingPhase.TRANSFERRING_FILE)
    register_phase_entered = False

    def _on_transfer_progress(done: int, total: int) -> None:
        nonlocal register_phase_entered
        trace.transferred_bytes = done
        if trace.file_size_bytes is None:
            trace.file_size_bytes = total
        if not register_phase_entered:
            set_transfer_progress(
                download.id,
                total_bytes=total,
                done_bytes=done,
            )
        if total > 0 and done >= total and not register_phase_entered:
            register_phase_entered = True
            runtime.enter_phase(PostProcessingPhase.REGISTERING_LIBRARY_FILE)

    library_file = await register_library_file(
        session,
        comic_file,
        issue,
        MatchConfidence.HIGH,
        move_to_library=True,
        library_root_id=preferred_managed_root_id(series),
        transfer_progress_callback=_on_transfer_progress,
        download_client=download.download_client,
        replace_existing_library_file=bool(getattr(download, "replace_existing_file", False)),
        replacement_trash_dir=replacement_trash_dir,
    )
    dest_path = Path(library_file.file_path)
    trace.final_path = str(dest_path)
    trace.source_preserved = await get_running_loop().run_in_executor(
        None,
        comic_file.exists,
    )
    trace.effective_transfer_method = infer_effective_transfer_method(
        source_path=comic_file,
        destination_path=dest_path,
        configured_transfer_method=method,
        seed_safe_torrent_import=bool(trace.seed_safe_torrent_import),
    )
    trace.file_size_bytes = library_file.file_size
    if trace.transferred_bytes is None:
        trace.transferred_bytes = library_file.file_size
    trace.finalize_current_phase()
    log.info("post_processing_transferred", method=method, final_path=str(dest_path))
    return dest_path

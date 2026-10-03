"""No-session polling phase for the download monitor task."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

RecordDownloadProgress = Callable[..., bool]
BuildStatusUpdate = Callable[..., dict[str, object] | None]
BuildStatusCheckErrorUpdate = Callable[..., dict[str, object] | None]


async def poll_download_clients(
    poll_items: Sequence[Mapping[str, object]],
    download_svc: Any,
    *,
    record_download_progress: RecordDownloadProgress,
    build_status_update: BuildStatusUpdate,
    build_status_check_error_update: BuildStatusCheckErrorUpdate,
    event_logger: Any,
) -> list[dict[str, object]]:
    """Poll download clients and collect DB updates for the write phase."""
    updates: list[dict[str, object]] = []

    for item in poll_items:
        dl_id = item["id"]
        download_id = int(cast("Any", dl_id))
        external_id = item["external_id"]
        title = item["title"]
        client_type = item["download_client"]
        existing_path = item["downloaded_path"]

        client = download_svc.get_client_for_identity(
            item.get("download_client_config_id"), client_type
        )
        if not client:
            continue

        # Try to match by title if no external_id.
        if not external_id:
            if hasattr(client, "find_torrent_by_title") and title:
                try:
                    found_hash = await client.find_torrent_by_title(str(title))
                    if found_hash:
                        external_id = found_hash
                        updates.append(
                            {
                                "id": download_id,
                                "external_id": found_hash,
                            }
                        )
                    else:
                        continue
                except Exception:
                    event_logger.debug("download_title_match_failed", download_id=dl_id)
                    continue
            else:
                continue

        try:
            status = await client.get_download_status(str(external_id))
        except Exception as exc:
            update = build_status_check_error_update(
                download_id=download_id,
                external_id=external_id,
                client_type=client_type,
                issue_id=item["issue_id"],
                error=exc,
                event_logger=event_logger,
            )
            if update is not None:
                updates.append(update)
            continue

        is_stall_state = record_download_progress(
            download_id,
            status,
            event_logger=event_logger,
        )

        update = build_status_update(
            download_id=download_id,
            external_id=external_id,
            status=status,
            existing_path=existing_path,
            is_stall_state=is_stall_state,
            event_logger=event_logger,
        )
        if update is not None:
            updates.append(update)

    return updates

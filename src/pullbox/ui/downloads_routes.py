"""Downloads page and HTMX UI routes."""

import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import ColumnElement, String, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession
from pullbox.models.client import DownloadClientConfig
from pullbox.models.direct_acquisition import DirectAcquisitionAttempt, DirectArtifactAttempt
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue
from pullbox.models.operation_progress import OperationProgress, OperationProgressType
from pullbox.models.series import Series
from pullbox.services.download_history_classification import (
    download_history_clause,
    post_processing_failure_clause,
    post_processing_history_clause,
)
from pullbox.ui.formatters import format_filesize

logger = structlog.get_logger(__name__)

router = APIRouter()
htmx_router = APIRouter()

_DOWNLOAD_QUEUE_FINALIZATION_TOKENS = frozenset(
    {
        "repairing",
        "extracting",
        "verifying",
        "moving",
        "running",
        "quickcheck",
        "postprocessingqueued",
        "loadingpars",
        "verifyingrepair",
        "renaming",
        "unpacking",
    }
)
_DOWNLOAD_QUEUE_STATES = [
    DownloadState.QUEUED,
    DownloadState.SENT,
    DownloadState.DOWNLOADING,
    DownloadState.FINALIZING,
    DownloadState.PAUSED,
    DownloadState.RETRY_PENDING,
]
_DOWNLOAD_HISTORY_PER_PAGE = 25
_DOWNLOAD_HISTORY_SORT_OPTIONS = {"title", "issue", "status", "client", "size", "updated_at"}


@dataclass(frozen=True)
class DownloadQueueRowView:
    """Stable queue-row presentation derived from app and client state."""

    download: DownloadHistory
    display_title: str
    client_label: str
    primary_phase: str
    status_pill: str
    status_detail: str | None
    progress_pct: float
    progress_label: str
    progress_tone: str
    progress_indeterminate: bool
    speed_bytes: int | None
    eta_text: str | None
    is_active: bool


class _LoadDownloadProgressMap(Protocol):
    def __call__(
        self,
        session: AsyncSession,
        queue_items: Sequence[DownloadHistory],
        *,
        fallback_progress: Mapping[int, object],
    ) -> Awaitable[dict[int, object]]: ...


_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]
_FormatEta = Callable[[int | None], str]
_BuildQueueNames = Callable[[AsyncSession, Sequence[DownloadHistory]], Awaitable[dict[int, str]]]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None
_format_eta: _FormatEta | None = None
_build_queue_names: _BuildQueueNames | None = None
_load_download_progress_map: _LoadDownloadProgressMap | None = None
_sidebar_badge_no_store_headers: Mapping[str, str] = {}


def configure_downloads_routes(
    *,
    get_templates: _GetTemplates,
    build_context: _BuildContext,
    format_eta: _FormatEta,
    build_queue_names: _BuildQueueNames,
    load_download_progress_map: _LoadDownloadProgressMap,
    sidebar_badge_no_store_headers: Mapping[str, str],
) -> None:
    """Provide shared UI runtime dependencies from the facade module."""
    global _get_templates
    global _build_context
    global _format_eta
    global _build_queue_names
    global _load_download_progress_map
    global _sidebar_badge_no_store_headers
    _get_templates = get_templates
    _build_context = build_context
    _format_eta = format_eta
    _build_queue_names = build_queue_names
    _load_download_progress_map = load_download_progress_map
    _sidebar_badge_no_store_headers = sidebar_badge_no_store_headers


def _templates() -> Jinja2Templates:
    if _get_templates is None:
        msg = "downloads routes have not been configured with templates"
        raise RuntimeError(msg)
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        msg = "downloads routes have not been configured with a context builder"
        raise RuntimeError(msg)
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


def _eta(value: int | None) -> str:
    if _format_eta is None:
        msg = "downloads routes have not been configured with an ETA formatter"
        raise RuntimeError(msg)
    return _format_eta(value)


async def _queue_names(
    session: AsyncSession,
    downloads: Sequence[DownloadHistory],
) -> dict[int, str]:
    if _build_queue_names is None:
        msg = "downloads routes have not been configured with a queue-name builder"
        raise RuntimeError(msg)
    return await _build_queue_names(session, downloads)


async def _progress_map(
    session: AsyncSession,
    queue_items: Sequence[DownloadHistory],
    *,
    fallback_progress: Mapping[int, object],
) -> dict[int, object]:
    if _load_download_progress_map is None:
        msg = "downloads routes have not been configured with a progress-map loader"
        raise RuntimeError(msg)
    return await _load_download_progress_map(
        session,
        queue_items,
        fallback_progress=fallback_progress,
    )


async def get_download_queue_count(session: AsyncSession) -> int:
    """Return the number of active queue items."""
    return (
        await session.execute(
            select(func.count(DownloadHistory.id)).where(
                DownloadHistory.state.in_(_DOWNLOAD_QUEUE_STATES)
            )
        )
    ).scalar_one()


async def get_download_history_count(session: AsyncSession) -> int:
    """Return the number of history items shown on the downloads page."""
    history_filters = get_download_history_filters(None, None)
    return (
        await session.execute(select(func.count(DownloadHistory.id)).where(*history_filters))
    ).scalar_one()


def normalize_download_history_sort(sort: str | None) -> str:
    """Return a safe downloads history sort key."""
    if not sort:
        return "-updated_at"
    field = sort.lstrip("-")
    if field not in _DOWNLOAD_HISTORY_SORT_OPTIONS:
        return "-updated_at"
    return f"-{field}" if sort.startswith("-") else field


def get_download_history_order_by(sort: str) -> list[ColumnElement[object]]:
    """Build stable order clauses for downloads history sorting."""
    normalized_sort = normalize_download_history_sort(sort)
    sort_desc = normalized_sort.startswith("-")
    sort_field = normalized_sort.lstrip("-")

    status_sort = case(
        (
            and_(
                DownloadHistory.state == DownloadState.FAILED,
                DownloadHistory.error_message == "Cancelled by user",
            ),
            "cancelled",
        ),
        (DownloadHistory.state == DownloadState.COMPLETED, "completed"),
        (DownloadHistory.state == DownloadState.FAILED, "failed"),
        else_=DownloadHistory.state,
    )

    # SQLAlchemy column types (InstrumentedAttribute, Case, UnaryExpression)
    # don't align with ColumnElement in mypy's view, so use Any for the
    # intermediate sort map and cast to the return type.
    sort_map: dict[str, list[object]] = {
        "title": [DownloadHistory.title],
        "issue": [Series.sort_title, Issue.issue_number],
        "status": [status_sort, DownloadHistory.updated_at],
        "client": [DownloadHistory.download_client, DownloadHistory.updated_at],
        "size": [DownloadHistory.file_size],
        "updated_at": [DownloadHistory.updated_at],
    }
    sort_columns = sort_map.get(sort_field, [DownloadHistory.updated_at])

    order_by: list[ColumnElement[object]] = []
    for col in sort_columns:
        column: ColumnElement[object] = col  # type: ignore[assignment]
        order_by.append(column.desc().nullslast() if sort_desc else column.asc().nullslast())
    order_by.append(DownloadHistory.id.desc())  # type: ignore[arg-type]
    return order_by


def get_download_history_filters(
    status: str | None,
    client: str | None,
) -> list[ColumnElement[bool]]:
    """Build the history filter list for downloads history queries."""
    base_history_filter = download_history_clause()
    history_filters: list[ColumnElement[bool]] = [base_history_filter]
    if status == "cancelled":
        history_filters.append(DownloadHistory.error_message == "Cancelled by user")
    elif status:
        history_filters.append(DownloadHistory.state == status)
        if status == "failed":
            history_filters.append(DownloadHistory.error_message != "Cancelled by user")
            history_filters.append(~post_processing_failure_clause())
    if client:
        history_filters.append(DownloadHistory.download_client == client)
    return history_filters


def download_client_type_label(client_type: str) -> str:
    """Return the user-facing label for a download client type value."""
    labels = {
        DownloadClientType.SABNZBD.value: "SABnzbd",
        DownloadClientType.NZBGET.value: "NZBGet",
        DownloadClientType.QBITTORRENT.value: "qBittorrent",
        DownloadClientType.TRANSMISSION.value: "Transmission",
        DownloadClientType.DELUGE.value: "Deluge",
        DownloadClientType.DIRECT.value: "Direct Download",
        DownloadClientType.AIRDCPP.value: "AirDC++",
    }
    return labels.get(client_type, client_type.replace("_", " ").title())


def normalize_download_queue_client_state(client_state: str | None) -> str | None:
    """Return a cleaned human-readable client sub-state for queue rendering."""
    if client_state is None:
        return None
    normalized = client_state.strip()
    return normalized or None


def download_queue_client_state_token(client_state: str | None) -> str:
    """Return a compact tokenized form of a client sub-state."""
    normalized = normalize_download_queue_client_state(client_state)
    if not normalized:
        return ""
    return re.sub(r"[^a-z0-9]+", "", normalized.lower())


def is_download_queue_pollable_state(state: DownloadState) -> bool:
    """Return True when the queue page should ask the client for live status."""
    return state in {
        DownloadState.SENT,
        DownloadState.DOWNLOADING,
        DownloadState.FINALIZING,
        DownloadState.PAUSED,
    }


def is_download_queue_finalization_state(client_state: str | None) -> bool:
    """Return True when the client sub-state indicates post-download finalization."""
    token = download_queue_client_state_token(client_state)
    if not token:
        return False
    return any(token.startswith(prefix) for prefix in _DOWNLOAD_QUEUE_FINALIZATION_TOKENS)


def snapshot_value(snapshot: object | None, attr: str) -> object | None:
    """Read an attribute from a cached progress snapshot when present."""
    if snapshot is None:
        return None
    return getattr(snapshot, attr, None)


def snapshot_progress(snapshot: object | None) -> float:
    """Return a safe fractional progress value from a snapshot-like object."""
    value = snapshot_value(snapshot, "progress")
    if isinstance(value, int | float):
        return max(0.0, min(float(value), 1.0))
    return 0.0


def build_live_progress_snapshot(existing_snapshot: object | None, status: object) -> object:
    """Merge a fresh client queue item with cached progress for stable rendering."""
    from pullbox.tasks.download_task import ProgressSnapshot

    raw_progress = getattr(status, "progress", 0.0)
    client_state = normalize_download_queue_client_state(getattr(status, "client_state", None))
    state = str(getattr(status, "state", "") or "").lower()
    is_finalizing = is_download_queue_finalization_state(client_state)
    raw_progress_value = float(raw_progress) if isinstance(raw_progress, int | float) else 0.0
    raw_progress_value = max(0.0, min(raw_progress_value, 1.0))
    if is_finalizing or state == "finalizing":
        progress = raw_progress_value if raw_progress_value > 0 else 1.0
    else:
        progress = max(raw_progress_value, snapshot_progress(existing_snapshot))

    speed_bytes = getattr(status, "speed_bytes", None)
    eta_seconds = getattr(status, "eta_seconds", None)
    if is_finalizing or state not in {"downloading", "finalizing"}:
        speed_bytes = None
        eta_seconds = None

    return ProgressSnapshot(
        progress=progress,
        speed_bytes=speed_bytes if isinstance(speed_bytes, int) else None,
        eta_seconds=eta_seconds if isinstance(eta_seconds, int) else None,
        size_bytes=getattr(status, "size_bytes", None)
        if isinstance(getattr(status, "size_bytes", None), int)
        else None,
        updated_at=time.monotonic(),
        client_state=client_state,
    )


def build_download_queue_row_view(
    download: DownloadHistory,
    progress: object | None,
    renamed_title: str | None,
) -> DownloadQueueRowView:
    """Convert queue state plus live progress into a stable UI row model."""
    client_label = download_client_type_label(download.download_client.value)
    raw_source_label = snapshot_value(progress, "source_label")
    if (
        download.download_client is DownloadClientType.DIRECT
        and isinstance(raw_source_label, str)
        and raw_source_label
    ):
        client_label = raw_source_label
    raw_client_state = snapshot_value(progress, "client_state")
    client_state = normalize_download_queue_client_state(
        raw_client_state if isinstance(raw_client_state, str) else None
    )
    client_state_token = download_queue_client_state_token(client_state)
    is_finalizing = is_download_queue_finalization_state(client_state)
    progress_fraction = snapshot_progress(progress)
    progress_pct = round(progress_fraction * 100, 1)
    speed_bytes = snapshot_value(progress, "speed_bytes")
    eta_seconds = snapshot_value(progress, "eta_seconds")
    bytes_transferred = snapshot_value(progress, "bytes_transferred")
    progress_indeterminate = bool(snapshot_value(progress, "is_indeterminate"))
    source_slow = bool(snapshot_value(progress, "source_slow"))

    primary_phase = "Queued"
    status_pill = "pill-info"
    status_detail: str | None = None
    progress_tone = "is-blue"
    progress_label = "—"
    eta_text: str | None = None
    is_active = False

    if download.state == DownloadState.PAUSED:
        primary_phase = "Paused"
        status_pill = "pill-warning"
        progress_tone = "is-amber"
        if progress_pct > 0:
            progress_label = f"{round(progress_pct):.0f}%"
    elif download.state == DownloadState.RETRY_PENDING:
        primary_phase = "Retry pending"
        status_pill = "pill-warning"
        progress_tone = "is-amber"
        detail_parts: list[str] = []
        if download.error_message:
            detail_parts.append(download.error_message.rstrip("."))
        max_retries = download.max_retries or 0
        retry_count = download.retry_count or 0
        if max_retries > 0:
            detail_parts.append(f"Retry {retry_count} of {max_retries}")
        if detail_parts:
            status_detail = ". ".join(detail_parts) + "."
        if progress_pct > 0:
            progress_label = f"{round(progress_pct):.0f}%"
    elif download.state == DownloadState.QUEUED:
        primary_phase = client_state or "Queued"
        status_pill = "pill-info"
    elif download.state in {
        DownloadState.SENT,
        DownloadState.DOWNLOADING,
        DownloadState.FINALIZING,
    }:
        is_active = True
        if is_finalizing or download.state == DownloadState.FINALIZING:
            primary_phase = "Finalizing in client"
            status_pill = "pill-warning"
            progress_tone = "is-amber"
            if progress_pct <= 0:
                progress_pct = 100.0
            progress_label = client_state or "Finalizing"
            speed_bytes = None
            eta_seconds = None
        else:
            primary_phase = (
                "Downloading"
                if download.state == DownloadState.DOWNLOADING
                and client_state_token in {"queued", "sent"}
                else client_state or "Downloading"
            )
            status_pill = "pill-info"
            progress_tone = "is-blue"
            if progress_indeterminate:
                progress_label = (
                    f"{format_filesize(bytes_transferred)} received"
                    if isinstance(bytes_transferred, int) and bytes_transferred > 0
                    else "Receiving..."
                )
                eta_seconds = None
            else:
                progress_label = f"{round(progress_pct):.0f}%"
            if not progress_indeterminate and isinstance(eta_seconds, int) and eta_seconds > 0:
                eta_text = _eta(eta_seconds)
            if download.download_client is DownloadClientType.DIRECT and source_slow:
                primary_phase = "Source responding slowly"
                status_pill = "pill-warning"
                status_detail = (
                    "The source is still transferring data below 500 kbps. "
                    "Pullbox will keep downloading."
                )
                progress_tone = "is-amber"

    return DownloadQueueRowView(
        download=download,
        display_title=renamed_title or download.title,
        client_label=client_label,
        primary_phase=primary_phase,
        status_pill=status_pill,
        status_detail=status_detail,
        progress_pct=progress_pct,
        progress_label=progress_label,
        progress_tone=progress_tone,
        progress_indeterminate=progress_indeterminate,
        speed_bytes=speed_bytes if isinstance(speed_bytes, int) else None,
        eta_text=eta_text,
        is_active=is_active,
    )


def build_download_queue_rows(
    queue_items: Sequence[DownloadHistory],
    progress_map: Mapping[int, object],
    renamed_names: Mapping[int, str],
) -> tuple[list[DownloadQueueRowView], list[DownloadQueueRowView], list[DownloadQueueRowView]]:
    """Create stable active/waiting row collections for the downloads queue."""
    queue_rows = [
        build_download_queue_row_view(
            download,
            progress_map.get(download.id),
            renamed_names.get(download.id),
        )
        for download in queue_items
    ]
    active_rows = [row for row in queue_rows if row.is_active]
    waiting_rows = [row for row in queue_rows if not row.is_active]
    return queue_rows, active_rows, waiting_rows


async def load_download_queue_context(session: AsyncSession) -> dict[str, object]:
    """Load the active queue panel context for the downloads page."""
    queue_result = await session.execute(
        select(DownloadHistory)
        .options(joinedload(DownloadHistory.issue).joinedload(Issue.series))
        .where(DownloadHistory.state.in_(_DOWNLOAD_QUEUE_STATES))
        .order_by(DownloadHistory.created_at.desc())
    )
    queue_items = list(queue_result.unique().scalars().all())
    from pullbox.tasks.download_task import get_all_progress

    fallback_progress = get_all_progress()
    progress_map = await _progress_map(
        session,
        queue_items,
        fallback_progress=fallback_progress,
    )
    renamed_names = await _queue_names(session, queue_items)
    queue_rows, active_rows, waiting_rows = build_download_queue_rows(
        queue_items,
        progress_map,
        renamed_names,
    )
    active_count = len(active_rows)
    queued_count = sum(
        1 for row in waiting_rows if row.primary_phase in {"Queued", "Retry pending"}
    )
    paused_count = sum(1 for row in waiting_rows if row.primary_phase == "Paused")
    combined_speed = sum(row.speed_bytes or 0 for row in active_rows)

    return {
        "queue_items": queue_items,
        "queue_rows": queue_rows,
        "active_rows": active_rows,
        "waiting_rows": waiting_rows,
        "queue_count": len(queue_items),
        "active_count": active_count,
        "queued_count": queued_count,
        "paused_count": paused_count,
        "waiting_count": len(waiting_rows),
        "combined_speed_bytes": combined_speed,
        "progress_map": progress_map,
        "renamed_names": renamed_names,
    }


async def load_download_progress_map(
    session: AsyncSession,
    queue_items: Sequence[DownloadHistory],
    *,
    fallback_progress: Mapping[int, object],
) -> dict[int, object]:
    """Return queue progress from local durable state without polling clients."""
    from pullbox.tasks.download_task import ProgressSnapshot

    progress_map = dict(fallback_progress)
    direct_items: dict[int, int] = {}
    for item in queue_items:
        if item.download_client is not DownloadClientType.DIRECT or not item.external_id:
            continue
        prefix, separator, raw_id = item.external_id.partition(":")
        if prefix == "direct" and separator and raw_id.isdigit():
            direct_items[item.id] = int(raw_id)

    if direct_items:
        attempts = (
            await session.execute(
                select(DirectAcquisitionAttempt).where(
                    DirectAcquisitionAttempt.id.in_(direct_items.values())
                )
            )
        ).scalars()
        attempts_by_id = {attempt.id: attempt for attempt in attempts}
        for download_id, attempt_id in direct_items.items():
            attempt = attempts_by_id.get(attempt_id)
            if attempt is None:
                continue
            snapshot = attempt.progress_snapshot or {}
            raw_percent = snapshot.get("percent")
            percent = float(raw_percent) if isinstance(raw_percent, int | float) else 0.0
            speed = snapshot.get("bytes_per_second")
            eta = snapshot.get("eta_seconds")
            total = snapshot.get("total_bytes")
            transferred = snapshot.get("bytes_transferred")
            stage = snapshot.get("stage")
            host_kind = snapshot.get("host_kind")
            progress_map[download_id] = ProgressSnapshot(
                progress=max(0.0, min(percent / 100, 1.0)),
                speed_bytes=int(speed) if isinstance(speed, int | float) else None,
                eta_seconds=int(eta) if isinstance(eta, int | float) else None,
                size_bytes=int(total) if isinstance(total, int | float) else None,
                updated_at=time.monotonic(),
                client_state=_direct_progress_label(snapshot),
                source_label=_direct_source_label(attempt.provider_identity, host_kind),
                bytes_transferred=(
                    int(transferred) if isinstance(transferred, int | float) else None
                ),
                is_indeterminate=(
                    stage == "downloading"
                    and not isinstance(raw_percent, int | float)
                    and not isinstance(total, int | float)
                ),
                source_slow=snapshot.get("source_slow") is True,
            )
    air_items = {
        item.id for item in queue_items if item.download_client is DownloadClientType.AIRDCPP
    }
    if air_items:
        from pullbox.models.airdcpp import AirDcppAcquisition

        acquisitions = (
            await session.execute(
                select(AirDcppAcquisition).where(
                    AirDcppAcquisition.download_history_id.in_(air_items)
                )
            )
        ).scalars()
        for acquisition in acquisitions:
            queue = (acquisition.route_snapshot or {}).get("queue")
            if not isinstance(queue, Mapping):
                continue
            size = queue.get("size_bytes")
            transferred = queue.get("downloaded_bytes")
            speed = queue.get("speed_bytes")
            eta = queue.get("eta_seconds")
            status_id = queue.get("status_id")
            size_bytes = int(size) if isinstance(size, int | float) and size > 0 else None
            transferred_bytes = (
                int(transferred)
                if isinstance(transferred, int | float) and transferred >= 0
                else None
            )
            progress = (
                min(transferred_bytes / size_bytes, 1.0)
                if transferred_bytes is not None and size_bytes is not None
                else 0.0
            )
            client_state = (
                status_id.replace("_", " ").title()
                if isinstance(status_id, str) and status_id
                else acquisition.client_state
            )
            progress_map[acquisition.download_history_id] = ProgressSnapshot(
                progress=progress,
                speed_bytes=int(speed) if isinstance(speed, int | float) and speed > 0 else None,
                eta_seconds=int(eta) if isinstance(eta, int | float) and eta > 0 else None,
                size_bytes=size_bytes,
                updated_at=time.monotonic(),
                client_state=client_state,
                source_label="AirDC++",
                bytes_transferred=transferred_bytes,
            )

    operation_keys = [str(item.id) for item in queue_items]
    if operation_keys:
        operations = (
            await session.execute(
                select(OperationProgress).where(
                    OperationProgress.operation_type == OperationProgressType.DOWNLOAD,
                    OperationProgress.operation_key.in_(operation_keys),
                )
            )
        ).scalars()
        item_ids = {str(item.id): item.id for item in queue_items}
        for operation in operations:
            projected_download_id = item_ids.get(operation.operation_key)
            if projected_download_id is None:
                continue
            progress_map[projected_download_id] = ProgressSnapshot(
                progress=(operation.overall_percent or 0.0) / 100,
                speed_bytes=int(operation.rate) if operation.rate is not None else None,
                eta_seconds=operation.eta_seconds,
                size_bytes=operation.overall_total,
                updated_at=time.monotonic(),
                client_state=(operation.message or operation.phase.replace("_", " ").capitalize()),
                source_label=operation.source_label,
                bytes_transferred=operation.overall_current,
                is_indeterminate=operation.overall_indeterminate,
                source_slow=operation.detail_snapshot.get("source_slow") is True,
            )

    return progress_map


def _direct_progress_label(snapshot: Mapping[str, object]) -> str:
    stage = snapshot.get("stage")
    host_kind = snapshot.get("host_kind")
    stage_value = str(stage) if isinstance(stage, str) and stage else "direct"
    host_label = _direct_host_label(host_kind)

    if stage_value == "resolver":
        resolver_name = snapshot.get("resolver_name")
        resolver_kind = snapshot.get("resolver_kind")
        resolver_scope = snapshot.get("resolver_scope")
        attempt = snapshot.get("resolver_attempt")
        total = snapshot.get("resolver_total")
        label = (
            resolver_name.strip()
            if isinstance(resolver_name, str) and resolver_name.strip()
            else "browser resolver"
        )
        if resolver_kind == "trawl" and resolver_scope == "datanodes":
            return "Using TRAWL (required by DataNodes)"
        if isinstance(attempt, int) and isinstance(total, int) and total > 0:
            return f"Trying {label} (resolver {attempt} of {total})"
        return f"Trying {label}"
    if stage_value == "fallback_queued" and host_label:
        return f"Trying {host_label}"
    if stage_value == "resolving" and host_label:
        return f"Resolving {host_label}"
    if stage_value == "downloading" and host_label:
        return f"Downloading from {host_label}"
    if stage_value == "validating" and host_label:
        return f"Validating {host_label} download"
    return stage_value.replace("_", " ").capitalize()


def _direct_host_label(host_kind: object) -> str:
    host_value = str(host_kind) if isinstance(host_kind, str) and host_kind else ""
    host_labels = {
        "mega": "MEGA",
        "pixeldrain": "PixelDrain",
        "rootz": "Rootz",
        "mediafire": "MediaFire",
        "terabox": "TeraBox",
        "datanodes": "DataNodes",
        "generic_https": "HTTPS",
    }
    return host_labels.get(host_value, host_value.replace("_", " ").title())


def _direct_source_label(provider_identity: str, host_kind: object) -> str:
    provider_labels = {
        "pullbox.getcomics": "GetComics",
        "pullbox.annas_archive": "Anna's Archive",
    }
    provider_label = provider_labels.get(
        provider_identity,
        provider_identity.removeprefix("pullbox.").replace("_", " ").title(),
    )
    host_label = _direct_host_label(host_kind)
    return f"{provider_label} via {host_label}" if host_label else provider_label


async def build_download_history_client_labels(
    session: AsyncSession,
    history_items: Sequence[DownloadHistory],
) -> dict[int, str]:
    """Return client labels enriched with durable direct artifact hosts."""
    labels = {
        download.id: download_client_type_label(download.download_client.value)
        for download in history_items
    }
    attempt_ids_by_download: dict[int, int] = {}
    for download in history_items:
        if download.download_client is not DownloadClientType.DIRECT or not download.external_id:
            continue
        prefix, separator, raw_attempt_id = download.external_id.partition(":")
        if prefix == "direct" and separator and raw_attempt_id.isdigit():
            attempt_ids_by_download[download.id] = int(raw_attempt_id)

    if not attempt_ids_by_download:
        return labels

    artifact_rows = await session.execute(
        select(
            DirectArtifactAttempt.acquisition_attempt_id,
            DirectArtifactAttempt.host_kind,
        )
        .where(DirectArtifactAttempt.acquisition_attempt_id.in_(attempt_ids_by_download.values()))
        .order_by(
            DirectArtifactAttempt.acquisition_attempt_id,
            DirectArtifactAttempt.is_selected.desc(),
            DirectArtifactAttempt.sequence_no.desc(),
        )
    )
    host_by_attempt: dict[int, object] = {}
    for attempt_id, host_kind in artifact_rows:
        host_by_attempt.setdefault(attempt_id, host_kind)

    direct_label = download_client_type_label(DownloadClientType.DIRECT.value)
    for download_id, attempt_id in attempt_ids_by_download.items():
        host_label = _direct_host_label(host_by_attempt.get(attempt_id))
        labels[download_id] = f"{direct_label} · {host_label}" if host_label else direct_label
    return labels


async def load_download_history_context(
    session: AsyncSession,
    *,
    status: str | None,
    client: str | None,
    search_query: str,
    page: int,
    sort: str,
) -> dict[str, object]:
    """Load the paginated downloads history panel context."""
    from pullbox.core.db_utils import escape_like

    history_filters = get_download_history_filters(status, client)
    normalized_search_query = search_query.strip()
    normalized_sort = normalize_download_history_sort(sort)
    client_result = await session.execute(
        select(DownloadClientConfig).order_by(
            DownloadClientConfig.priority, DownloadClientConfig.name
        )
    )
    client_options: list[tuple[str, str]] = [("", "All Clients")]
    seen_client_types: set[str] = set()
    for configured_client in client_result.scalars().all():
        client_value = configured_client.client_type.value
        if client_value in seen_client_types:
            continue
        seen_client_types.add(client_value)
        client_options.append(
            (
                client_value,
                f"{configured_client.name} ({download_client_type_label(client_value)})",
            )
        )
    if client and client not in seen_client_types:
        client_options.append((client, download_client_type_label(client)))

    if normalized_search_query:
        search_term = f"%{escape_like(normalized_search_query)}%"
        history_filters.append(
            or_(
                DownloadHistory.title.ilike(search_term),
                Series.title.ilike(search_term),
                cast(Issue.issue_number, String).ilike(search_term),
            )
        )

    history_total: int = (
        await session.execute(
            select(func.count(DownloadHistory.id))
            .select_from(DownloadHistory)
            .join(Issue, DownloadHistory.issue_id == Issue.id)
            .join(Series, Issue.series_id == Series.id)
            .where(*history_filters)
        )
    ).scalar_one()

    post_processing_history_total: int = (
        await session.execute(
            select(func.count(DownloadHistory.id)).where(post_processing_history_clause())
        )
    ).scalar_one()

    history_completed_count, history_failed_count, history_cancelled_count = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(case((DownloadHistory.state == DownloadState.COMPLETED, 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    DownloadHistory.state == DownloadState.FAILED,
                                    DownloadHistory.error_message != "Cancelled by user",
                                    ~post_processing_failure_clause(),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case((DownloadHistory.error_message == "Cancelled by user", 1), else_=0)
                    ),
                    0,
                ),
            )
            .select_from(DownloadHistory)
            .join(Issue, DownloadHistory.issue_id == Issue.id)
            .join(Series, Issue.series_id == Series.id)
            .where(*history_filters)
        )
    ).one()

    history_pages = max(
        1, (history_total + _DOWNLOAD_HISTORY_PER_PAGE - 1) // _DOWNLOAD_HISTORY_PER_PAGE
    )
    page = min(page, history_pages)
    offset = (page - 1) * _DOWNLOAD_HISTORY_PER_PAGE

    history_result = await session.execute(
        select(DownloadHistory)
        .join(Issue, DownloadHistory.issue_id == Issue.id)
        .join(Series, Issue.series_id == Series.id)
        .options(joinedload(DownloadHistory.issue).joinedload(Issue.series))
        .where(*history_filters)
        .order_by(*get_download_history_order_by(normalized_sort))
        .limit(_DOWNLOAD_HISTORY_PER_PAGE)
        .offset(offset)
    )
    history_items = list(history_result.unique().scalars().all())
    history_client_labels = await build_download_history_client_labels(session, history_items)

    return {
        "history_items": history_items,
        "history_client_labels": history_client_labels,
        "history_total": history_total,
        "history_completed_count": int(history_completed_count or 0),
        "history_failed_count": int(history_failed_count or 0),
        "history_cancelled_count": int(history_cancelled_count or 0),
        "post_processing_history_total": post_processing_history_total,
        "history_pages": history_pages,
        "page": page,
        "status_filter": status or "",
        "client_filter": client or "",
        "search_query": normalized_search_query,
        "client_options": client_options,
        "sort": normalized_sort,
    }


@router.get("/downloads", response_class=HTMLResponse, include_in_schema=False)
async def downloads(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    tab: str = Query("queue"),
    status: str | None = Query(None),
    client: str | None = Query(None),
    search: str = Query(""),
    sort: str = Query("-updated_at"),
    page: int = Query(1, ge=1),
) -> Response:
    """Render the downloads page with queue and history tabs."""
    normalized_tab = tab if tab in {"queue", "history"} else "queue"
    queue_ctx = await load_download_queue_context(session)
    history_ctx: dict[str, object] = {}
    if normalized_tab == "history":
        history_ctx = await load_download_history_context(
            session,
            status=status,
            client=client,
            search_query=search,
            page=page,
            sort=sort,
        )

    ctx = _ctx(
        request,
        user,
        tab=normalized_tab,
        download_states=DownloadState,
        client_types=DownloadClientType,
        download_client_label=download_client_type_label,
        **queue_ctx,
        **history_ctx,
    )

    if request.headers.get("HX-Request"):
        if (
            normalized_tab == "history"
            and request.headers.get("HX-Target") == "downloads-history-results"
        ):
            return _templates().TemplateResponse(
                request, "partials/download_history_results_bundle.html", ctx
            )
        return _templates().TemplateResponse(request, "partials/downloads_content_bundle.html", ctx)

    return _templates().TemplateResponse(request, "pages/downloads.html", ctx)


@htmx_router.get("/htmx/downloads/queue", response_class=HTMLResponse, include_in_schema=False)
async def htmx_download_queue(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Return the download queue partial for HTMX polling."""
    queue_ctx = await load_download_queue_context(session)
    return _templates().TemplateResponse(
        request,
        "partials/download_queue_bundle.html",
        _ctx(
            request,
            user,
            tab="queue",
            download_states=DownloadState,
            download_client_label=download_client_type_label,
            **queue_ctx,
        ),
        headers=_sidebar_badge_no_store_headers,
    )


@htmx_router.get("/htmx/downloads/history", response_class=HTMLResponse, include_in_schema=False)
async def htmx_download_history(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    status: str | None = Query(None),
    client: str | None = Query(None),
    search: str = Query(""),
    sort: str = Query("-updated_at"),
    page: int = Query(1, ge=1),
) -> Response:
    """Return the download history partial for HTMX polling."""
    history_ctx = await load_download_history_context(
        session, status=status, client=client, search_query=search, page=page, sort=sort
    )

    return _templates().TemplateResponse(
        request,
        "partials/download_history_results_bundle.html",
        _ctx(
            request,
            user,
            tab="history",
            download_client_label=download_client_type_label,
            **history_ctx,
        ),
    )


@htmx_router.get(
    "/htmx/downloads/history/{download_id}/error-detail",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def htmx_download_history_error_detail(
    request: Request,
    download_id: int,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Render a failed download's error text only after its row is expanded."""
    result = await session.execute(
        select(DownloadHistory).where(
            DownloadHistory.id == download_id,
            DownloadHistory.state == DownloadState.FAILED,
            DownloadHistory.error_message.is_not(None),
            DownloadHistory.error_message != "Cancelled by user",
            download_history_clause(),
            ~post_processing_failure_clause(),
        )
    )
    download = result.scalar_one_or_none()
    if download is None:
        raise HTTPException(status_code=404, detail="Download history error not found")

    return _templates().TemplateResponse(
        request,
        "partials/download_history_error_detail.html",
        _ctx(request, user, dl=download),
    )

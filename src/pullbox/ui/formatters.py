"""Shared Jinja/UI formatting helpers."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from markupsafe import Markup

from pullbox.core.duration_format import format_duration_ms_label
from pullbox.core.html_sanitizer import sanitize_rich_html
from pullbox.core.issue_numbers import format_issue_number as _format_issue_number
from pullbox.models.series import SeriesStatus


def sanitize_rich_html_filter(value: str | None) -> Markup:
    """Return sanitized provider HTML marked safe for Jinja rendering."""
    return Markup(sanitize_rich_html(value))  # nosec


def format_issue_number(value: float) -> str:
    """Format issue number, stripping unnecessary trailing zeros."""
    return _format_issue_number(value)


def format_filesize(value: int) -> str:
    """Format bytes to human-readable file size."""
    fval = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(fval) < 1024:
            return f"{fval:.0f} {unit}" if unit == "B" else f"{fval:.1f} {unit}"
        fval /= 1024
    return f"{fval:.1f} PB"


def format_dlspeed(value: int | None) -> str:
    """Format download speed in bytes/sec to human-readable string."""
    if value is None or value <= 0:
        return ""
    if value < 1024:
        return f"{value} B/s"
    if value < 1024 * 1024:
        return f"{value / 1024:.0f} KB/s"
    return f"{value / (1024 * 1024):.1f} MB/s"


def format_eta(value: int | None) -> str:
    """Format ETA in seconds to human-readable string."""
    if value is None or value < 0:
        return ""
    if value < 60:
        return f"{value}s"
    if value < 3600:
        minutes, secs = divmod(value, 60)
        return f"{minutes}m {secs}s"
    hours, remainder = divmod(value, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m"


def format_duration_ms(value: object) -> str:
    """Format UI duration values using a shared compact style."""
    return format_duration_ms_label(value)


def format_series_year_label(
    year_start: int | None,
    year_end: int | None,
    status: SeriesStatus | str | None,
) -> str:
    """Format a series year range with status-aware semantics."""
    if year_start is None:
        return "Unknown"

    normalized_status = (
        (status.value if isinstance(status, SeriesStatus) else str(status).strip().lower())
        if status
        else ""
    )

    if normalized_status == SeriesStatus.CONTINUING.value:
        if year_end is not None:
            if year_end == year_start:
                return str(year_start)
            return f"{year_start}\u2013{year_end}"
        return f"{year_start}\u2013present"

    if normalized_status == SeriesStatus.ENDED.value:
        resolved_year_end = year_end if year_end is not None else year_start
        if resolved_year_end == year_start:
            return str(year_start)
        return f"{year_start}\u2013{resolved_year_end}"

    if year_end is not None and year_end != year_start:
        return f"{year_start}\u2013{year_end}"
    return str(year_start)


def format_localtime(value: date | datetime | None, fmt: str | None = None) -> str:
    """Convert a UTC datetime to the configured display timezone and format."""
    if value is None:
        return ""
    if isinstance(value, date) and not isinstance(value, datetime):
        if fmt:
            return value.strftime(fmt)
        from pullbox.core.display_time import format_datetime

        return format_datetime(value)

    from pullbox.core.display_time import format_datetime, get_display_timezone
    from pullbox.core.timezone import get_timezone

    if fmt:
        tz = get_timezone()
        if value.tzinfo is None:
            local_dt = value.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        else:
            local_dt = value.astimezone(tz)
        return local_dt.strftime(fmt)

    from pullbox.core.display_time import get_cached_display_settings

    settings = get_cached_display_settings()
    tz = get_display_timezone(db_value=str(settings.get("timezone", "browser"))) or get_timezone()
    return format_datetime(
        value,
        timezone=tz,
        date_format=str(settings.get("date_format", "MMM DD, YYYY")),
        time_format=str(settings.get("time_format", "24h")),
        show_seconds=bool(settings.get("show_seconds", False)),
        show_timezone=bool(settings.get("show_timezone", True)),
        show_ampm=bool(settings.get("show_ampm", True)),
    )


def format_localtime_time(value: datetime | None) -> str:
    """Convert a UTC datetime to the configured display timezone and time format only."""
    if value is None:
        return ""

    from pullbox.core.display_time import (
        format_time,
        get_cached_display_settings,
        get_display_timezone,
    )
    from pullbox.core.timezone import get_timezone

    settings = get_cached_display_settings()
    tz = get_display_timezone(db_value=str(settings.get("timezone", "browser"))) or get_timezone()
    return format_time(
        value,
        timezone=tz,
        time_format=str(settings.get("time_format", "24h")),
        show_seconds=bool(settings.get("show_seconds", False)),
        show_timezone=bool(settings.get("show_timezone", True)),
        show_ampm=bool(settings.get("show_ampm", True)),
    )


def dashboard_state_pill_tone(state: str) -> str:
    """Map dashboard severity states to shared semantic pill tones."""
    mapping = {
        "critical": "pill-error",
        "high": "pill-warning",
        "watch": "pill-info",
        "healthy": "pill-success",
        "info": "pill-info",
    }
    return mapping.get(state, "pill-neutral")


_ERROR_HINTS: list[tuple[str, str, str]] = [
    (
        "post-processing source does not exist",
        "Path not found",
        "The download path reported by the client doesn't exist on this machine. "
        "Check Remote Path and Download Directory in Settings \u2192 Download Clients.",
    ),
    (
        "authentication failed",
        "Auth failed",
        "Check your username/password in Settings \u2192 Download Clients.",
    ),
    (
        "connection refused",
        "Connection refused",
        "Can't reach the download client. Verify the URL and that the client is running.",
    ),
    (
        "request timed out",
        "Timed out",
        "The download client didn't respond in time. It may be overloaded or unreachable.",
    ),
    (
        "removed from the client externally",
        "Removed externally",
        "This download was deleted directly in the download client.",
    ),
    (
        "failed to download nzb from url",
        "NZB fetch failed",
        "Pullbox couldn't download the NZB file from the indexer. "
        "The indexer may be temporarily unavailable.",
    ),
    (
        "failed to download torrent file",
        "Torrent fetch failed",
        "Pullbox couldn't download the .torrent file from the indexer. "
        "The indexer may be temporarily unavailable.",
    ),
    (
        "url did not return nzb content",
        "Invalid NZB",
        "The indexer returned an error page instead of an NZB file. "
        "The release may no longer be available.",
    ),
    (
        "no download client available",
        "No client configured",
        "No download client is configured for this protocol. "
        "Add one in Settings \u2192 Download Clients.",
    ),
    (
        "no .* download client configured",
        "No client configured",
        "No download client is configured for this protocol. "
        "Add one in Settings \u2192 Download Clients.",
    ),
]


def humanize_download_error(error: str | None) -> dict[str, str]:
    """Map a raw download error to a user-friendly label and hint."""
    if not error:
        return {"label": "", "hint": "", "raw": ""}

    lower = error.lower()
    for pattern, label, hint in _ERROR_HINTS:
        if pattern in lower:
            return {"label": label, "hint": hint, "raw": error}

    return {"label": "Error", "hint": error, "raw": error}


_TYPE_DISPLAY_MAP: dict[str, str] = {
    "issue": "",
    "tpb": "Trade Paperback",
    "gn": "Graphic Novel",
    "ogn": "Original Graphic Novel",
    "hc": "Hardcover",
    "annual": "Annual",
    "omnibus": "Omnibus",
    "deluxe": "Deluxe Edition",
    "compendium": "Compendium",
    "one_shot": "One-Shot",
    "special": "Special",
    "volume": "Volume",
}


def format_type_display(value: str | None) -> str:
    """Convert issue type abbreviation to full display name."""
    if not value:
        return ""
    if value in _TYPE_DISPLAY_MAP:
        return _TYPE_DISPLAY_MAP[value]
    return value.replace("_", " ").title()

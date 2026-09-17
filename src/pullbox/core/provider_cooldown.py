"""Process-local account cooldowns shared by short-lived provider clients."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
import weakref
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def retry_after_seconds(header: str | None, *, default: float) -> float:
    """Accept seconds or an HTTP date; reject malformed and unbounded values."""
    try:
        delay = float(header or "")
    except ValueError:
        try:
            when = parsedate_to_datetime(header or "")
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            delay = (when - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return default
    return min(delay, 7 * 86400) if math.isfinite(delay) and delay > 0 else default


class ProviderCooldown:
    def __init__(self) -> None:
        self.until = 0.0
        self.request_lock = asyncio.Lock()
        self.last_request_time = 0.0

    @property
    def remaining_seconds(self) -> int:
        return max(0, math.ceil(self.until - time.monotonic()))

    def defer(self, seconds: float) -> None:
        self.until = max(self.until, time.monotonic() + seconds)


_STATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, ProviderCooldown]] = (
    weakref.WeakKeyDictionary()
)


def provider_cooldown(namespace: str, identity: str) -> ProviderCooldown:
    """Keep credentials out of registry keys, logs, and durable state."""
    key = hashlib.sha256(f"{namespace}\0{identity}".encode()).hexdigest()
    states = _STATES.setdefault(asyncio.get_running_loop(), {})
    return states.setdefault(key, ProviderCooldown())

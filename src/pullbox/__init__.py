"""Pullbox — modern comic book management and acquisition platform."""

from datetime import UTC, datetime

__version__ = "1.1.3-dev"

# Set once at process start; used by System > About for uptime calculation.
STARTED_AT: datetime = datetime.now(UTC)

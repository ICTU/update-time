"""Dependency update cooldown helpers."""

from datetime import UTC, datetime, timedelta

COOLDOWN_DAYS = 7


def within_cooldown(timestamp: datetime | None) -> bool:
    """Return whether the timestamp falls within the cooldown period."""
    if timestamp is None:
        return False
    return datetime.now(UTC) - timestamp < timedelta(days=COOLDOWN_DAYS)

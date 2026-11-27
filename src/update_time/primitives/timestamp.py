"""Reading and comparing timestamps, and the minutes and seconds in a day."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

MINUTES_PER_DAY: Final[int] = 24 * 60
SECONDS_PER_DAY: Final[int] = MINUTES_PER_DAY * 60


def days_since(timestamp: datetime) -> int:
    """Return how many whole days ago the timestamp was, negative for a timestamp in the future.

    Whole days rather than an exact duration, so the result is an `int` that compares against a day count of any
    size. Turning such a count into a `timedelta` instead would cap it at 999999999 days.
    """
    return (datetime.now(UTC) - timestamp).days


def utc_date_and_time(timestamp: datetime) -> str:
    """Return the timestamp's date and time in UTC, without the seconds, labelled UTC."""
    return f"{timestamp.astimezone(UTC):%Y-%m-%d %H:%M} UTC"


def newest_timestamp(timestamps: Iterable[str | None]) -> datetime | None:
    """Return the most recent of the ISO-8601 timestamps there are, or None if none of them is there."""
    parsed = (parse_timestamp(timestamp) for timestamp in timestamps)
    return max((timestamp for timestamp in parsed if timestamp is not None), default=None)


def parse_timestamp(timestamp: str | None) -> datetime | None:
    """Return the ISO-8601 timestamp parsed, or None when there is none to parse."""
    return datetime.fromisoformat(timestamp) if timestamp else None

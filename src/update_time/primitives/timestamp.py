"""Reading and comparing timestamps."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


def days_since(timestamp: datetime) -> int:
    """Return how many whole days ago the timestamp was, negative for a timestamp in the future.

    Whole days rather than an exact duration, so the result is an `int` that compares against a day count of any
    size. Turning such a count into a `timedelta` instead would cap it at 999999999 days.
    """
    return (datetime.now(UTC) - timestamp).days


def parse_timestamp(timestamp: str | None) -> datetime | None:
    """Return the ISO-8601 timestamp parsed, or None when there is none to parse.

    An absent timestamp yields None rather than raising, so a caller that has nothing to parse is left with an
    unknown date to decide about.
    """
    return datetime.fromisoformat(timestamp) if timestamp else None


def newest_timestamp(timestamps: Iterable[str | None]) -> datetime | None:
    """Return the most recent of the ISO-8601 timestamps, or None if none of them is there.

    A timestamp that isn't there is skipped rather than raising, so a mixture of present and absent ones still
    yields the newest present one.
    """
    parsed = (parse_timestamp(timestamp) for timestamp in timestamps)
    return max((timestamp for timestamp in parsed if timestamp is not None), default=None)

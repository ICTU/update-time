"""When a release is still too fresh to adopt."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from update_time.primitives.environment import EnvVar
from update_time.primitives.timestamp import days_since

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.primitives.lookup import Lookup

# Private channel that passes --cooldown from the CLI to the updater subprocesses.
COOLDOWN = EnvVar("_UPDATE_TIME_COOLDOWN_DAYS", default=7, parse=int)


def within_cooldown(timestamp: datetime | None, cooldown_days: int) -> bool:
    """Return whether the timestamp falls within a cooldown period of the given number of days.

    Whole days are compared, so a cooldown of more days than a `timedelta` can hold is honoured rather than
    overflowing: the count comes from a marker in a file as well as from the command line.
    """
    if timestamp is None:
        return False
    return days_since(timestamp) < cooldown_days


def past_cooldown(cooldown_days: int, publication: Lookup[datetime], report_undated: Callable[[str], None]) -> bool:
    """Return whether what was published is older than the cooldown, reading its date only where a cooldown applies.

    Where a cooldown applies, it holds back what the source failed to date, and `report_undated` names the reason.
    The cooldown holds back nothing a source does not date at all.
    """
    if cooldown_days == 0:
        return True
    if publication.reason:
        report_undated(publication.reason)
        return False
    return not within_cooldown(publication.value, cooldown_days)


@dataclass
class CooldownWalk:
    """The cooldown applied while walking candidate versions, newest first.

    The walk stops at a candidate the source fails to date while a cooldown applies, and at one the source fails to
    serve what checking it takes, such as the metadata of a PyPI release. The failure, such as a rate limit, would hold
    for every candidate after it too.
    """

    days: int
    stopped: bool = False

    def stop(self) -> None:
        """Stop the walk, so it holds back every candidate after this one."""
        self.stopped = True

    def past(self, publication: Lookup[datetime], report_undated: Callable[[str], None]) -> bool:
        """Return whether the candidate is older than the cooldown, stopping the walk when the source can't date it."""

        def stop_undated(reason: str) -> None:
            """Stop the walk at the candidate the source failed to date, and report why."""
            self.stop()
            report_undated(reason)

        return past_cooldown(self.days, publication, stop_undated)


def cooldown_cutoff(cooldown_days: int) -> str:
    """Return the cooldown as an RFC 3339 cutoff timestamp: releases published after it are still too fresh to adopt.

    A cooldown reaching further back than a date can express is clamped to the earliest instant there is. Such a
    cooldown excludes every release anyway, so clamping keeps the meaning.
    """
    now = datetime.now(UTC)
    earliest = datetime.min.replace(tzinfo=UTC)
    if cooldown_days > (now - earliest).days:
        return earliest.isoformat()
    return (now - timedelta(days=cooldown_days)).isoformat()

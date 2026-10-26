"""When a dependency's newest release is old enough to warn about."""

from typing import TYPE_CHECKING

from update_time.primitives.environment import EnvVar
from update_time.primitives.timestamp import days_since

if TYPE_CHECKING:
    from datetime import datetime

    from update_time.domain.dependency import DependencyVersion, Release

# The threshold to ask for to hear about no stale dependency at all, which switches the check off rather than
# setting a day count. It is no number of days, so nothing is ever older than it.
_NO_STALENESS_CHECK = 0

# Private channel that passes --stale-after from the CLI to the updater subprocesses.
STALE_AFTER = EnvVar("_UPDATE_TIME_STALE_AFTER_DAYS", default=365, parse=int)


def staleness_is_checked(threshold: int) -> bool:
    """Return whether the run checks staleness at the threshold."""
    return threshold != _NO_STALENESS_CHECK


def is_stale(published: datetime, threshold: int) -> bool:
    """Return whether a version published on the given date is stale (older than the threshold in days).

    A threshold that switches the check off makes nothing stale. Whole days are compared (via `days_since`), so a
    fractional day over the threshold is not stale yet.
    """
    if not staleness_is_checked(threshold):
        return False
    return days_since(published) > threshold


def stale_release(version: DependencyVersion, threshold: int) -> Release | None:
    """Return the dependency's newest release when it is old enough to warn about, or None when it is not."""
    newest = version.project.newest
    return newest if newest is not None and is_stale(newest.published, threshold) else None


def release_judged_alike(version: DependencyVersion, threshold: int, run_threshold: int) -> Release | None:
    """Return the dependency's newest release when both thresholds judge its staleness alike, or None otherwise."""
    newest = version.project.newest
    if newest is None or is_stale(newest.published, threshold) != is_stale(newest.published, run_threshold):
        return None
    return newest

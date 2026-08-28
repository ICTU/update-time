"""When a dependency's newest release is old enough to warn about."""

from typing import TYPE_CHECKING

from update_time.primitives.environment import EnvVar
from update_time.primitives.timestamp import days_since

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime
    from pathlib import Path

    from update_time.domain.dependency import DependencyVersion, Release
    from update_time.domain.reference import ResolvedReference

# The threshold to ask for to hear about no stale dependency at all, which switches the check off rather than
# setting a day count. It is no number of days, so nothing is ever older than it.
NO_STALENESS_CHECK = 0

# Private channel that passes --stale-after from the CLI to the updater subprocesses.
STALE_AFTER = EnvVar("_UPDATE_TIME_STALE_AFTER_DAYS", default=365, parse=int)


def is_stale(published: datetime, threshold: int) -> bool:
    """Return whether a version published on the given date is stale (older than the threshold in days).

    A threshold that switches the check off makes nothing stale. Whole days are compared (via `days_since`), so a
    fractional day over the threshold is not stale yet.
    """
    if threshold == NO_STALENESS_CHECK:
        return False
    return days_since(published) > threshold


def stale_release(version: DependencyVersion, threshold: int) -> Release | None:
    """Return the dependency's newest release when it is old enough to warn about, or None when it is not."""
    newest = version.newest
    return newest if newest is not None and is_stale(newest.published, threshold) else None


def warn_about_stale_dependencies(
    files: Iterable[Path],
    newest_releases: Callable[[Path], Iterable[ResolvedReference]],
    warn: Callable[[ResolvedReference, int], None],
) -> None:
    """Warn about each dependency whose newest release is older than the threshold, for a delegated update.

    The resolver leaves out a dependency it resolved no release for. The threshold is the global one, which a
    delegated dependency has no marker to override. Skipped entirely when the check is disabled, so the resolver
    never runs and makes no registry request.
    """
    if (threshold := STALE_AFTER.get()) == NO_STALENESS_CHECK:
        return
    for file in files:
        for resolved in newest_releases(file):
            warn(resolved, threshold)

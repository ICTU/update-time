"""Dependency staleness helpers."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from update_time.primitives.environment import EnvVar
from update_time.primitives.location import Location

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from update_time.domain.version import DependencyName, DependencyVersion

# Private channel that passes --stale-after from the CLI to the updater subprocesses; 0 disables the check.
STALE_AFTER = EnvVar("_UPDATE_TIME_STALE_AFTER_DAYS", default=365, parse=int)


def staleness_days(published: datetime) -> int:
    """Return how many whole days ago the version was published."""
    return (datetime.now(UTC) - published).days


def is_stale(published: datetime | None, threshold: int) -> bool:
    """Return whether a version published on the given date is stale (older than the threshold in days).

    A threshold of 0 disables the check, and an unknown publication date never counts as stale. Whole days are
    compared (via `staleness_days`), so a fractional day over the threshold is not stale yet.
    """
    if threshold == 0 or published is None:
        return False
    return staleness_days(published) > threshold


def newest_datetime(timestamps: Iterable[str]) -> datetime | None:
    """Return the most recent of the ISO-8601 timestamps, or None if there are none.

    Sources derive their "newest release" date (the one the staleness check compares against) from the publication
    dates they list — PyPI file upload times, GitHub release dates, npm publish times — so they share this here.
    """
    return max((datetime.fromisoformat(timestamp) for timestamp in timestamps), default=None)


def warn_about_stale_dependencies(
    files: Iterable[Path],
    newest_releases: Callable[[Path], Iterable[tuple[DependencyName, DependencyVersion | None]]],
    warn: Callable[[DependencyName, DependencyVersion, Location, int], None],
) -> None:
    """Run the staleness pass shared by the updaters that delegate to a package manager.

    Delegating to uv, npm, or pnpm means never calling a source per dependency, so these updaters make their own
    pass. The location carries no line number, since a delegated file surfaces no per-dependency line. The threshold
    is the global one, which a delegated dependency has no marker to override. Skipped entirely when the check is
    disabled, so the resolver never runs and makes no registry request. Callback-driven so `domain` stays free of I/O.
    """
    if (threshold := STALE_AFTER.get()) == 0:
        return
    for file in files:
        for name, release in newest_releases(file):
            if release is not None:
                warn(name, release, Location(file), threshold)

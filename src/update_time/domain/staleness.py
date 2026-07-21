"""Dependency staleness helpers.

A dependency is stale when its newest published version was released a long time ago: the upstream project has
gone quiet, which is a maintenance signal worth surfacing even though there is no newer version to adopt. This
mirrors the cooldown helpers, at the other end of the timeline: the cooldown holds back releases that are too
fresh, while staleness warns about dependencies whose newest release is too old.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from update_time.primitives.environment import EnvVar

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from update_time.domain.version import DependencyName, DependencyVersion

# Private channel that passes --stale-after from the CLI to the updater subprocesses; 0 disables the check.
STALE_AFTER = EnvVar("_UPDATE_TIME_STALE_AFTER_DAYS", default=365, parse=int)


def staleness_days(published: datetime) -> int:
    """Return how many whole days ago the version was published."""
    return (datetime.now(UTC) - published).days


def is_stale(published: datetime | None) -> bool:
    """Return whether a version published on the given date is stale (older than the threshold).

    A threshold of 0 disables the check, and an unknown publication date never counts as stale, so neither ever
    produces a warning. Whole days are compared (via `staleness_days`), the same count the warning reports, so the
    "> threshold" decision and the "N days ago (> threshold)" message never disagree at the fractional boundary.
    """
    if (threshold := STALE_AFTER.get()) == 0 or published is None:
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
    warn: Callable[[DependencyName, DependencyVersion, Path], None],
) -> None:
    """Run the staleness pass the manifest updaters share (`pyproject.toml`, `package.json`).

    The line-by-line updaters check staleness inline (via `references.rewrite`), but a manifest updater delegates
    the update to uv/npm/pnpm and never calls a source per dependency, so it makes its own pass: `newest_releases(file)`
    yields the file's declared dependencies as `(name, newest release)` pairs — resolving each release from a registry,
    None when the dependency isn't a registry package — and `warn(name, release, file)` reports the stale ones
    (typically `Logger.warn_if_stale`). Skipped entirely when the check is disabled, so the resolver never runs and
    makes no registry request. Callback-driven so `domain` stays free of the I/O the resolver and warning perform,
    like `first_eligible`.
    """
    if STALE_AFTER.get() == 0:
        return
    for file in files:
        for name, release in newest_releases(file):
            if release is not None:
                warn(name, release, file)

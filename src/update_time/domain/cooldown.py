"""When a release is still too fresh to adopt."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from update_time.domain.dependency import DependencyVersion, Release
from update_time.primitives.environment import EnvVar

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from _typeshed import SupportsRichComparison

    from update_time.domain.dependency import VersionString
    from update_time.primitives.lookup import Lookup

# Private channel that passes --cooldown from the CLI to the updater subprocesses.
COOLDOWN = EnvVar("_UPDATE_TIME_COOLDOWN_DAYS", default=7, parse=int)


def within_cooldown(timestamp: datetime | None, cooldown_days: int) -> bool:
    """Return whether the timestamp falls within a cooldown period of the given number of days.

    A zero-day cooldown holds nothing back, not even a timestamp in the future.
    """
    if timestamp is None or cooldown_days == 0:
        return False
    return datetime.now(UTC) < cooldown_end(timestamp, cooldown_days)


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

    The walk remembers the newest candidate the cooldown holds back. It stops at a candidate the source fails to date
    while a cooldown applies, and at one the source fails to serve what checking it takes, such as the metadata of a
    PyPI release. The failure, such as a rate limit, would hold for every candidate after it too.
    """

    days: int
    stopped: bool = False
    fresh_release: Release | None = None  # The newest candidate inside the cooldown

    def stop(self) -> None:
        """Stop the walk, so it passes over every candidate after this one but the current one."""
        self.stopped = True

    def past(
        self, version: VersionString, publication: Lookup[datetime], report_undated: Callable[[str], None]
    ) -> bool:
        """Return whether the candidate is older than the cooldown, stopping the walk when the source can't date it."""

        def stop_undated(reason: str) -> None:
            """Stop the walk at the candidate the source failed to date, and report why."""
            self.stop()
            report_undated(reason)

        if past_cooldown(self.days, publication, stop_undated):
            return True
        if publication.value is not None:
            self._remember(version, publication.value)
        return False

    def holds_back(self, version: VersionString, published: datetime) -> bool:
        """Return whether the cooldown holds back a candidate the source dated."""
        if not within_cooldown(published, self.days):
            return False
        self._remember(version, published)
        return True

    def _remember(self, version: VersionString, published: datetime) -> None:
        """Remember the candidate as the newest one inside the cooldown, unless the walk passed a newer one."""
        if self.fresh_release is None:
            self.fresh_release = Release(version, published)


def first_eligible[Candidate: SupportsRichComparison](
    candidates: Iterable[Candidate],
    resolve: Callable[[Candidate, CooldownWalk], DependencyVersion | None],
    is_current: Callable[[Candidate], bool],
    current_version: VersionString,
    cooldown_days: int,
) -> DependencyVersion:
    """Return the highest candidate that resolves to an eligible version, or the current version unchanged.

    The candidates are comparable, so they are walked newest-first. `resolve` fetches what deciding a candidate takes,
    and asks the walk whether the cooldown holds the candidate back. It answers with the version, or with None to try
    the next candidate. Once `resolve` stops the walk, the walk passes over every candidate but the current one. The
    version carries the newest candidate the cooldown held back.
    """
    walk = CooldownWalk(cooldown_days)
    latest = None
    for candidate in sorted(candidates, reverse=True):
        if walk.stopped and not is_current(candidate):
            continue
        if (latest := resolve(candidate, walk)) is not None:
            break
    return replace(latest or DependencyVersion(current_version), fresh_release=walk.fresh_release)


def cooldown_end(published: datetime, cooldown_days: int) -> datetime:
    """Return when the cooldown ends for what was published.

    A cooldown reaching further ahead than a date can express is clamped to the latest instant there is. Such a
    cooldown holds the release back for good anyway, so clamping keeps the meaning.
    """
    latest = datetime.max.replace(tzinfo=UTC)
    if cooldown_days > (latest - published).days:
        return latest
    return published + timedelta(days=cooldown_days)


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

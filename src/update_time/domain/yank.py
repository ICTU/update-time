"""Which sources can observe a yanked version, what they report for one, and the pass over a manager's pins."""

from dataclasses import replace
from typing import TYPE_CHECKING

from update_time.primitives.capability import capability

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from update_time.domain.dependency import DependencyVersion, VersionString, Yank
    from update_time.domain.reference import ResolvedReference

yank_reporting, reports_yanks = capability("reports_yanks")


def with_yank_state(
    latest: DependencyVersion, current_version: VersionString, yank_state: Callable[[VersionString], Yank]
) -> DependencyVersion:
    """Return the version carrying the withdrawal state of the version the run left the reference on.

    A version the run moved to comes back unchanged, since a source skips a withdrawn release when picking a new
    version, so only a version a reference stayed on can be withdrawn. Looking the state up costs a request, so it is
    looked up only where there is something to report.
    """
    if latest.version != current_version:
        return latest
    return replace(latest, yank=yank_state(current_version))


def warn_about_yanked_dependencies(
    files: Iterable[Path],
    pinned_releases: Callable[[Path], Iterable[ResolvedReference]],
    warn: Callable[[ResolvedReference], None],
) -> None:
    """Warn about each pin the run leaves on a withdrawn release, for a delegated update.

    A delegated dependency has no marker to hold the check back, so every pin is checked.
    """
    for file in files:
        for resolved in pinned_releases(file):
            warn(resolved)

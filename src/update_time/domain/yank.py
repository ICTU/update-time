"""Which sources can observe that a version was yanked, what they report for one, and the pass over a manager's pins.

A source whose versions can carry a yank state registers its new-version getter with `yank_reporting`, and
`reports_yanks` reads the fact back, so an `ignore[yanked]` marker on a reference resolved through any other source
can be reported as redundant instead of silently holding nothing back. Only the getters that resolve a marked
reference take part.
"""

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
    """Run the yank pass shared by the updaters that delegate to a package manager.

    An updater that delegates the update never calls a source per dependency, so it makes this pass itself. Each pin
    is located by the resolver that read it back from the file, which is the only party that knows where in the file
    it sits. A delegated dependency has no marker to hold the check back, so every pin is checked. Callback-driven
    so `domain` stays free of I/O.
    """
    for file in files:
        for resolved in pinned_releases(file):
            warn(resolved)

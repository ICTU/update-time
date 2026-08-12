"""Which sources can observe that a version was yanked, and the yank pass over a package manager's pins.

A source whose versions can carry a yank state registers its new-version getter with `yank_reporting`, and
`reports_yanks` reads the fact back, so an `ignore[yanked]` marker on a reference resolved through any other source
can be reported as redundant instead of silently holding nothing back. Only the getters that resolve a marked
reference take part.
"""

from typing import TYPE_CHECKING

from update_time.domain.capability import capability

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from update_time.domain.version import DependencyName, DependencyVersion
    from update_time.primitives.location import Location

yank_reporting, reports_yanks = capability("reports_yanks")


def warn_about_yanked_dependencies(
    files: Iterable[Path],
    pinned_releases: Callable[[Path], Iterable[tuple[DependencyName, DependencyVersion, Location]]],
    warn: Callable[[DependencyName, DependencyVersion, Location], None],
) -> None:
    """Run the yank pass shared by the updaters that delegate to a package manager.

    An updater that delegates the update never calls a source per dependency, so it makes this pass itself. Each pin
    is located by the resolver that read it back from the file, which is the only party that knows where in the file
    it sits. A delegated dependency has no marker to hold the check back, so every pin is checked. Callback-driven
    so `domain` stays free of I/O.
    """
    for file in files:
        for name, release, location in pinned_releases(file):
            warn(name, release, location)

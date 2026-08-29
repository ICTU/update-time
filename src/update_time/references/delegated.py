"""The checks a dependency gets when a package manager updates it rather than Update-time rewriting its line."""

from typing import TYPE_CHECKING

from update_time.domain.reference import resolved_references
from update_time.domain.staleness import NO_STALENESS_CHECK, STALE_AFTER
from update_time.markers.marker import Marker

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from update_time.domain.reference import ReferenceResolver
    from update_time.io.log import Logger

# What a delegated dependency carries, since it has no line of its own to write a marker on: a marker holding
# nothing back, so every check it reports through runs.
_NO_MARKER = Marker()


def warn_about_stale_dependencies(files: Iterable[Path], newest_releases: ReferenceResolver, log: Logger) -> None:
    """Warn about each dependency whose newest release is older than the threshold, for a delegated update.

    The resolver leaves out a dependency it resolved no release for. The threshold is the global one, which a
    delegated dependency has no marker to override. Skipped entirely when the check is disabled, so the resolver
    never runs and makes no registry request.
    """
    if (threshold := STALE_AFTER.get()) == NO_STALENESS_CHECK:
        return
    for resolved in resolved_references(files, newest_releases):
        log.report_staleness(resolved, _NO_MARKER, threshold)


def warn_about_yanked_dependencies(files: Iterable[Path], pinned_releases: ReferenceResolver, log: Logger) -> None:
    """Warn about each pin the run leaves on a withdrawn release, for a delegated update.

    A delegated dependency has no marker to hold the check back, so every pin is checked.
    """
    for resolved in resolved_references(files, pinned_releases):
        log.report_yank(resolved, _NO_MARKER)

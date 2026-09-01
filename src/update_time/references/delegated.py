"""The checks a dependency gets when a package manager updates it rather than Update-time rewriting its line."""

from typing import TYPE_CHECKING

from update_time.domain.reference import resolved_references
from update_time.domain.staleness import STALE_AFTER
from update_time.markers.marker import Marker
from update_time.references.resolve import project_is_checked, report_project

if TYPE_CHECKING:
    from collections.abc import Iterable

    from update_time.domain.reference import ReferenceResolver
    from update_time.io.log import Logger

# What a delegated dependency carries, since it has no line of its own to write a marker on: a marker holding
# nothing back, so every check it reports through runs.
_NO_MARKER = Marker()


def warn_about_projects[FileT](files: Iterable[FileT], projects: ReferenceResolver[FileT], log: Logger) -> None:
    """Ask the resolver about the dependencies the files declare, where a check needs it, and report what it answers."""
    threshold = STALE_AFTER.get()
    checked = [file for file in files if project_is_checked(projects, file, threshold)]
    for resolved in resolved_references(checked, projects):
        report_project(resolved, _NO_MARKER, threshold, log)


def warn_about_yanked_dependencies[FileT](
    files: Iterable[FileT], pinned_releases: ReferenceResolver[FileT], log: Logger
) -> None:
    """Warn about each pin the run leaves on a withdrawn release, for a delegated update."""
    for resolved in resolved_references(files, pinned_releases):
        log.report_yank(resolved, _NO_MARKER)

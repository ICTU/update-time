"""The sample values both README generators use, so the quoted log lines and the screenshot agree.

`log_samples` renders the log lines the README quotes and `generate_log_svg` the screenshot above them. Both show a
run of Update-time, so a value one of them changed on its own would leave the two describing different runs.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from update_time.domain.dependency import NO_PROJECT
from update_time.domain.marker import NO_MARKER
from update_time.domain.reference import Reference, ResolvedReference
from update_time.domain.vulnerability import Vulnerability

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyVersion, Project
    from update_time.domain.marker import Marker
    from update_time.primitives.location import Location

# The advisory the sample vulnerability warning names, rated critical so it is reported at every risk level.
VULNERABILITY: Final[Vulnerability] = Vulnerability(
    "GHSA-2gwj-7jmv-h26r", "SQL Injection in Django", "critical", "https://osv.dev/GHSA-2gwj-7jmv-h26r"
)


def reference(dependency: str, location: Location, version: str = "", marker: Marker = NO_MARKER) -> Reference:
    """Return the reference a sample message names; the vulnerability and floating-tag messages render its version."""
    return Reference(dependency, version, location, marker=marker)


def resolved(
    dependency: str,
    location: Location,
    release: DependencyVersion,
    project: Project = NO_PROJECT,
    marker: Marker = NO_MARKER,
) -> ResolvedReference:
    """Return the resolved reference a sample warning reports on."""
    return ResolvedReference(dependency, "", location, release=release, project=project, marker=marker)


def stale_publication_date() -> datetime:
    """Return a date that renders as `_STALE_DAYS` whole days ago, whenever the samples are generated.

    The extra hour puts it past the whole day the message counts, so the age never renders one day short.
    """
    # How old the sample stale dependency is, well past the default threshold so it is reported whatever that is set to.
    return datetime.now(UTC) - timedelta(days=512, hours=1)

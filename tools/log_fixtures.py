"""The sample values both README generators use, so the quoted log lines and the screenshot agree.

`log_samples` renders the log lines the README quotes and `generate_log_svg` the screenshot above them. Both show a
run of Update-time, so a value one of them changed on its own would leave the two describing different runs.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from update_time.domain.reference import Reference, ResolvedReference
from update_time.domain.vulnerability import Vulnerability

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyVersion
    from update_time.primitives.location import Location

# How old the sample stale dependency is, well past the default threshold so it is reported whatever that is set to.
_STALE_DAYS = 512

# The advisory the sample vulnerability warning names, rated critical so it is reported at every risk level.
VULNERABILITY = Vulnerability(
    "GHSA-2gwj-7jmv-h26r", "SQL Injection in Django", "critical", "https://osv.dev/GHSA-2gwj-7jmv-h26r"
)


def reference(dependency: str, location: Location, version: str = "") -> Reference:
    """Return the reference a sample message names; the vulnerability and floating-tag messages render its version."""
    return Reference(dependency, version, location)


def resolved(dependency: str, location: Location, release: DependencyVersion) -> ResolvedReference:
    """Return the resolved reference a sample staleness or yank message reports on."""
    return ResolvedReference(dependency, "", location, release=release)


def stale_publication_date() -> datetime:
    """Return a date that renders as `_STALE_DAYS` whole days ago, whenever the samples are generated.

    The extra hour puts it past the whole day the message counts, so the age never renders one day short.
    """
    return datetime.now(UTC) - timedelta(days=_STALE_DAYS, hours=1)

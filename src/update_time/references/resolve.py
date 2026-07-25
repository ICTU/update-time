"""Resolve which version a pinned reference should update to, the decision shared by every reference kind.

Given a reference's current version and its `# update-time:` marker, decide which version to update it to and
report on the decision through a `Logger`. The source is injected as a `NewVersionGetter`, so the decision is the
same whatever registry the reference points at; a reference kind with extra concerns (a commit SHA to pin, a
re-pushed digest to adopt) layers those on top of the version this decision resolves.
"""

from typing import TYPE_CHECKING

from update_time.domain.bound import BLOCK_ALL_UPDATES
from update_time.domain.yank import reports_yanks

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.location import Location
    from update_time.domain.marker import Marker
    from update_time.domain.version import DependencyVersion, Reference
    from update_time.io.log import Logger


def latest_version(
    reference: Reference,
    get_new_version: NewVersionGetter,
    marker: Marker,
    location: Location,
    log: Logger,
) -> DependencyVersion | None:
    """Return the latest version to update the reference to, or None to leave it unchanged.

    Warns about a marker that can have no effect — a redundant version bound, or a yank scope on a reference whose
    source has no yank to report — then resolves the latest version through `get_new_version` (passing the marker's
    version bound so the source only picks a version the bound admits), and warns about staleness and about a yanked
    version, each unless the marker holds that one back, in which case the hold-back is logged at the debug level
    instead, so a run reports what a marker actually suppressed. A marker holding the update back passes
    `BLOCK_ALL_UPDATES` instead of its own bound, so the source keeps the reference on its current version and reports
    on the version the reference stays on: a frozen pin that was yanked is still warned about. Returns None only when
    the marker holds the update back — after the staleness and yank checks, which an `ignore[update]` leaves live.
    A resolved version equal to the current one is still returned, not turned into None: it may carry a newer digest
    worth pinning, and weighing that is left out of this version decision.
    """
    dependency, current_version = reference.dependency, reference.current_version
    log.warn_if_redundant_bound(dependency, marker, current_version, location)
    if marker.ignore_yanked and not reports_yanks(get_new_version):
        log.redundant_yank_scope(dependency, marker, location)
    version_bound = BLOCK_ALL_UPDATES if marker.ignore_update else marker.version_bound
    latest = get_new_version(dependency, current_version, version_bound)
    if marker.ignore_stale:
        log.ignored_staleness(dependency, latest, marker, location)
    else:
        log.warn_if_stale(dependency, latest, location)
    if marker.ignore_yanked:
        log.ignored_yank(dependency, latest, marker, location)
    else:
        log.warn_if_yanked(dependency, latest, location)
    return None if marker.ignore_update else latest

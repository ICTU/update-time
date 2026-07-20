"""Resolve which version a pinned reference should update to, the decision shared by every reference kind.

Given a reference's current version and its `# update-time:` marker, decide which version to update it to and
report on the decision through a `Logger`. The source is injected as a `NewVersionGetter`, so the decision is the
same whatever registry the reference points at; a reference kind with extra concerns (a commit SHA to pin, a
re-pushed digest to adopt) layers those on top of the version this decision resolves.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.marker import Marker
    from update_time.domain.version import DependencyVersion, Reference
    from update_time.io.log import Logger


def latest_version(
    reference: Reference,
    get_new_version: NewVersionGetter,
    marker: Marker,
    path: Path,
    log: Logger,
) -> DependencyVersion | None:
    """Return the latest version to update the reference to, or None to leave it unchanged.

    Warns about a redundant bound, resolves the latest version through `get_new_version` (passing the marker's
    version bound so the source only picks a version the bound admits), and warns about staleness unless the
    marker holds that back. Returns None only when the marker holds the update back — after the staleness check,
    which an `ignore[update]` leaves live. A resolved version equal to the current one is still returned, not
    turned into None: it may carry a newer digest worth pinning, and weighing that is left out of this version
    decision.
    """
    dependency, current_version = reference.dependency, reference.current_version
    log.warn_if_redundant_bound(dependency, marker, current_version, path)
    latest = get_new_version(dependency, current_version, marker.version_bound)
    if not marker.ignore_stale:
        log.warn_if_stale(dependency, latest, path)
    return None if marker.ignore_update else latest

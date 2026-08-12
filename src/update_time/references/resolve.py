"""Resolve which version a pinned reference should update to, the decision shared by every reference kind.

Given a reference's current version and its `# update-time:` marker, decide which version to update it to and
report on the decision through a `Logger`. The source is injected as a `NewVersionGetter`, so the decision is the
same whatever registry the reference points at; a reference kind with extra concerns (a commit SHA to pin, a
re-pushed digest to adopt) layers those on top of the version this decision resolves.
"""

from typing import TYPE_CHECKING

from update_time.domain.bound import BLOCK_ALL_UPDATES
from update_time.domain.cooldown import COOLDOWN, honours_cooldown
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.vulnerability import reports_vulnerabilities
from update_time.domain.yank import reports_yanks

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.marker import Marker
    from update_time.domain.version import DependencyVersion, Reference
    from update_time.io.log import Logger
    from update_time.primitives.location import Location


def _warn_about_inverted_items(marker: Marker, dependency: str, location: Location, log: Logger) -> None:
    """Warn about each comparison item whose operator runs the wrong way, so it sets nothing for the reference.

    Every comparison item the marker language has is read here, so an item added to `Marker` is reported by naming
    it and its message rather than by repeating the condition.
    """
    inverted_items = (
        (marker.stale, log.inverted_stale_item),
        (marker.cooldown, log.inverted_cooldown_item),
        (marker.vulnerable, log.inverted_vulnerable_item),
    )
    for threshold, warn in inverted_items:
        if threshold.inverted_item is not None:
            warn(dependency, threshold.inverted_item, location)


def _warn_about_directives_the_source_cannot_honour(
    marker: Marker, get_new_version: NewVersionGetter, dependency: str, location: Location, log: Logger
) -> None:
    """Warn about each directive whose question the reference's source cannot answer, so it holds nothing back.

    Every capability a source can register is read here, so a capability added to `domain` is reported by adding a
    row for it rather than by repeating the condition.
    """
    directives = (
        (marker.ignore_yanked, reports_yanks, log.redundant_yank_scope),
        (marker.suppresses_vulnerabilities, reports_vulnerabilities, log.redundant_vulnerable_source),
        (marker.cooldown.value is not None, honours_cooldown, log.redundant_cooldown_item),
    )
    for is_set, reports, warn in directives:
        if is_set and not reports(get_new_version, dependency):
            warn(dependency, marker, location)


def latest_version(
    reference: Reference,
    get_new_version: NewVersionGetter,
    marker: Marker,
    location: Location,
    log: Logger,
) -> DependencyVersion | None:
    """Return the latest version to update the reference to, or None when the marker holds the update back.

    The source is queried even for a held-back reference, so the staleness and yank warnings still run for the
    version the reference stays on. If the source resolves a version equal to the current one, it is still returned,
    since it may carry a hash worth pinning.
    """
    dependency, current_version = reference.dependency, reference.current_version
    log.warn_if_redundant_bound(dependency, marker, current_version, location)
    _warn_about_directives_the_source_cannot_honour(marker, get_new_version, dependency, location, log)
    _warn_about_inverted_items(marker, dependency, location, log)
    version_bound = BLOCK_ALL_UPDATES if marker.ignore_update else marker.version_bound
    cooldown = marker.cooldown.value_or(COOLDOWN.get())
    latest = get_new_version(dependency, current_version, version_bound, cooldown)
    threshold = marker.stale.value_or(STALE_AFTER.get())
    if marker.ignore_stale:
        log.ignored_staleness(dependency, latest, marker, location, threshold)
    else:
        log.warn_if_stale(dependency, latest, location, threshold)
    if marker.ignore_yanked:
        log.ignored_yank(dependency, latest, marker, location)
    else:
        log.warn_if_yanked(dependency, latest, location)
    return None if marker.ignore_update else latest

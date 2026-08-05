"""Resolve which version a pinned reference should update to, the decision shared by every reference kind.

Given a reference's current version and its `# update-time:` marker, decide which version to update it to and
report on the decision through a `Logger`. The source is injected as a `NewVersionGetter`, so the decision is the
same whatever registry the reference points at; a reference kind with extra concerns (a commit SHA to pin, a
re-pushed digest to adopt) layers those on top of the version this decision resolves.
"""

from typing import TYPE_CHECKING

from update_time.domain.bound import BLOCK_ALL_UPDATES
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.yank import reports_yanks

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.marker import Marker
    from update_time.domain.version import DependencyVersion, Reference
    from update_time.io.log import Logger
    from update_time.primitives.environment import EnvVar
    from update_time.primitives.location import Location


def _days(marker_days: int | None, setting: EnvVar[int]) -> int:
    """Return the day count for this reference: its marker's when it carries one, the run's setting otherwise.

    The cooldown and the staleness threshold are both overridable per reference, and a marker wins over the command
    line for both, so they ask the same question here rather than each spelling out the precedence.
    """
    return setting.get() if marker_days is None else marker_days


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
    if marker.ignore_yanked and not reports_yanks(get_new_version):
        log.redundant_yank_scope(dependency, marker, location)
    if marker.stale.inverted_item is not None:
        log.inverted_stale_item(dependency, marker.stale.inverted_item, location)
    if marker.cooldown.inverted_item is not None:
        log.inverted_cooldown_item(dependency, marker.cooldown.inverted_item, location)
    version_bound = BLOCK_ALL_UPDATES if marker.ignore_update else marker.version_bound
    cooldown = _days(marker.cooldown.days, COOLDOWN)
    latest = get_new_version(dependency, current_version, version_bound, cooldown)
    threshold = _days(marker.stale.days, STALE_AFTER)
    if marker.ignore_stale:
        log.ignored_staleness(dependency, latest, marker, location, threshold)
    else:
        log.warn_if_stale(dependency, latest, location, threshold)
    if marker.ignore_yanked:
        log.ignored_yank(dependency, latest, marker, location)
    else:
        log.warn_if_yanked(dependency, latest, location)
    return None if marker.ignore_update else latest

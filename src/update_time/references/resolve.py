"""Resolve which version a pinned reference should update to, the decision shared by every reference kind.

Given a reference's current version and its `# update-time:` marker, decide which version to update it to and
report on the decision through a `Logger`. The source is injected as a `NewVersionGetter`, so the decision is the
same whatever registry the reference points at; a reference kind with extra concerns (a commit SHA to pin, a
re-pushed digest to adopt) layers those on top of the version this decision resolves.
"""

from typing import TYPE_CHECKING

from update_time.domain.bound import BLOCK_ALL_UPDATES
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.downgrade import downgrades
from update_time.domain.reference import ResolvedReference
from update_time.domain.staleness import STALE_AFTER
from update_time.markers.directive import DIRECTIVES, Reason
from update_time.markers.marker import Scope

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.dependency import DependencyVersion
    from update_time.domain.reference import Reference
    from update_time.io.log import Logger
    from update_time.markers.marker import Marker


def warn_about_inverted_items(marker: Marker, reference: Reference, log: Logger) -> None:
    """Warn about each comparison item whose operator runs the wrong way, so it sets nothing for the reference.

    `inverted_items` pairs each of the marker language's comparison items with the warning it gets.
    """
    inverted_items = (
        (marker.stale, log.inverted_stale_item),
        (marker.cooldown, log.inverted_cooldown_item),
        (marker.vulnerable, log.inverted_vulnerable_item),
    )
    for threshold, warn in inverted_items:
        if threshold.inverted_item is not None:
            warn(reference, threshold.inverted_item)


def _warn_about_directives_the_source_cannot_apply(
    marker: Marker, get_new_version: NewVersionGetter, reference: Reference, log: Logger
) -> None:
    """Warn about each directive the reference's source cannot apply, so it holds nothing back.

    Each warning names the directive to delete, so a bare `ignore` is never reported as redundant: it holds every
    scope back without spelling one out (see `Marker.as_written`).
    """
    as_written = marker.as_written
    for directive in DIRECTIVES:
        if as_written.directive_for(directive.scope) and not directive.is_applied_by(
            get_new_version, reference.dependency
        ):
            log.redundant_directive(reference, as_written.directive_for(directive.scope), directive.reason)


def _floating_pin_redundancy(marker: Marker, latest: DependencyVersion | None) -> Reason | None:
    """Return why the marker's directive to keep the pin floating holds nothing back, or None when it holds it.

    A frozen reference is never pinned, so the directive decides nothing there whatever the tag says. Otherwise the
    source's answer decides, since only a source that resolves a floating pin reports one: a reference whose pin
    names a version is reported, and so is one whose source knows no floating pin at all.
    """
    if not marker.allows(Scope.FLOATING_PIN):
        return None
    if marker.ignores(Scope.UPDATE):
        return Reason.UPDATE_HELD_BACK
    return Reason.NOTHING_FLOATING if latest is not None and latest.floating is None else None


def _warn_if_the_floating_pin_holds_nothing_back(
    marker: Marker, reference: Reference, log: Logger, latest: DependencyVersion | None
) -> None:
    """Warn when the marker's directive to keep the pin floating holds nothing back, saying why.

    `latest` is what the source resolved for the reference, or None where the marker left the source unasked.
    """
    if (reason := _floating_pin_redundancy(marker, latest)) is not None:
        log.redundant_directive(reference, marker.allow_directive(Scope.FLOATING_PIN), reason)


def latest_version(
    reference: Reference,
    get_new_version: NewVersionGetter,
    marker: Marker,
    log: Logger,
) -> DependencyVersion | None:
    """Return the latest version to update the reference to, or None when the marker holds the update back.

    What the marker itself gets wrong is reported whether or not a source is queried: an inverted comparison, a
    redundant bound, a directive the source cannot apply, and a directive keeping a frozen reference's pin floating
    are read off the marker and the source's capabilities alone. The source is queried unless the marker holds back
    the update, the staleness warning, and the yank warning alike, which leaves nothing to query it for; what only
    its answer can judge is reported then. A version equal to the current one is returned like any other, since it
    may carry a hash worth pinning.
    """
    dependency, current_version = reference.dependency, reference.current_version
    if not downgrades(get_new_version, dependency):
        log.warn_if_redundant_bound(reference, marker)
    warn_about_inverted_items(marker, reference, log)
    _warn_about_directives_the_source_cannot_apply(marker, get_new_version, reference, log)
    if marker.holds_back_source_checks:
        _warn_if_the_floating_pin_holds_nothing_back(marker, reference, log, latest=None)
        return None
    version_bound = BLOCK_ALL_UPDATES if marker.ignores(Scope.UPDATE) else marker.version_bound
    cooldown = marker.cooldown.value_or(COOLDOWN.get())
    latest = get_new_version(dependency, current_version, version_bound, cooldown)
    resolved = ResolvedReference(**vars(reference), release=latest)
    log.report_staleness(resolved, marker, marker.stale.value_or(STALE_AFTER.get()))
    log.report_yank(resolved, marker)
    _warn_if_the_floating_pin_holds_nothing_back(marker, reference, log, latest)
    return None if marker.ignores(Scope.UPDATE) else latest

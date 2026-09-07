"""Resolve which version a pinned reference should update to, the decision shared by every reference kind.

Given a reference's current version and its `# update-time:` marker, decide which version to update it to and
report on the decision through a `Logger`. The source is injected as a `NewVersionGetter`, so the decision is the
same whatever registry the reference points at; a reference kind with extra concerns (a commit SHA to pin, a
re-pushed digest to adopt) layers those on top of the version this decision resolves.

A reference the run resolves no update for takes `report_project_checks` instead.
"""

from typing import TYPE_CHECKING

from update_time.domain.archival import archival_is_checked, reports_archival
from update_time.domain.bound import BLOCK_ALL_UPDATES
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion
from update_time.domain.downgrade import downgrades
from update_time.domain.staleness import NO_STALENESS_CHECK, STALE_AFTER
from update_time.markers.directive import DIRECTIVES, Reason
from update_time.markers.marker import Scope
from update_time.markers.reference import SteeredResolvedReference

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.dependency import ProjectGetter
    from update_time.domain.reference import Reference
    from update_time.io.log import Logger
    from update_time.markers.marker import Marker


def warn_about_directives_the_source_cannot_apply(
    marker: Marker, get_new_version: NewVersionGetter, reference: Reference, log: Logger
) -> None:
    """Warn about each directive the reference's source cannot apply, so it holds nothing back."""
    as_written = marker.as_written
    for directive in DIRECTIVES:
        if (written := as_written.directive_for(directive.scope)) and not directive.is_applied_by(
            get_new_version, reference.dependency
        ):
            log.redundant_directive(reference, written, directive.reason)


def floating_pin_redundancy(marker: Marker, *, floats: bool | None) -> Reason | None:
    """Return why the marker's directive to keep the pin floating is redundant, or None when it keeps it floating.

    `floats` says whether the reference's pin floats, and is None where the run resolved no version to tell from.
    """
    if not marker.allows(Scope.FLOATING_PIN):
        return None
    if marker.ignores(Scope.UPDATE):
        return Reason.UPDATE_HELD_BACK
    if floats is False:
        return Reason.PIN_NOT_FLOATING
    return None


def _warn_if_the_floating_pin_is_redundant(
    marker: Marker, reference: Reference, log: Logger, latest: DependencyVersion | None
) -> None:
    """Warn when the marker's directive to keep the pin floating is redundant, saying why."""
    floats = None if latest is None else latest.floating is not None
    if (reason := floating_pin_redundancy(marker, floats=floats)) is not None:
        log.redundant_directive(reference, marker.allow_directive(Scope.FLOATING_PIN), reason)


def staleness_threshold(marker: Marker) -> int:
    """Return the number of days the reference is checked for staleness against: its own, or the run's."""
    return marker.stale.value_or(STALE_AFTER.get())


def latest_version(
    reference: Reference,
    get_new_version: NewVersionGetter,
    marker: Marker,
    log: Logger,
) -> DependencyVersion | None:
    """Return the latest version to update the reference to, or None when the marker holds the update back."""
    dependency, current_version = reference.dependency, reference.current_version
    if not downgrades(get_new_version, dependency):
        log.warn_if_redundant_bound(reference, marker)
    log.report_inverted_items(reference, marker)
    warn_about_directives_the_source_cannot_apply(marker, get_new_version, reference, log)
    if marker.holds_back_source_checks:
        _warn_if_the_floating_pin_is_redundant(marker, reference, log, latest=None)
        return None
    version_bound = BLOCK_ALL_UPDATES if marker.ignores(Scope.UPDATE) else marker.version_bound
    cooldown = marker.cooldown.value_or(COOLDOWN.get())
    latest = get_new_version(dependency, current_version, version_bound, cooldown, check_archival=archival_is_checked())
    resolved = SteeredResolvedReference.from_reference(reference, release=latest, marker=marker)
    report_project(resolved, log)
    log.report_yank(resolved, marker)
    _warn_if_the_floating_pin_is_redundant(marker, reference, log, latest)
    return None if marker.ignores(Scope.UPDATE) else latest


def project_is_checked(source: object, subject: object, threshold: int) -> bool:
    """Return whether a project check runs: the staleness check at this threshold, or the archival check.

    The archival check has no threshold of its own, so `--ignore-archived` is what switches it off.
    """
    return threshold != NO_STALENESS_CHECK or (archival_is_checked() and reports_archival(source, subject))


def report_project(resolved: SteeredResolvedReference, log: Logger) -> None:
    """Report the staleness and the archival of a project a source has already answered for."""
    log.report_staleness(resolved, resolved.marker, staleness_threshold(resolved.marker))
    log.report_archival(resolved, resolved.marker)


def report_project_checks(reference: Reference, marker: Marker, log: Logger, get_project: ProjectGetter) -> None:
    """Ask the source about the reference's project, where a check needs it, and report what it answers."""
    if marker.holds_everything_back:
        return
    threshold = staleness_threshold(marker)
    if not project_is_checked(get_project, reference.dependency, threshold):
        return
    release = DependencyVersion.unpinned(get_project(reference.dependency, check_archival=archival_is_checked()))
    resolved = SteeredResolvedReference.from_reference(reference, release=release, marker=marker)
    report_project(resolved, log)

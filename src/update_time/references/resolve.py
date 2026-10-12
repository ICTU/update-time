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
from update_time.domain.dependency import DependencyName, DependencyVersion
from update_time.domain.downgrade import downgrades
from update_time.domain.staleness import NO_STALENESS_CHECK, STALE_AFTER
from update_time.markers.cooldown import cooldown_days
from update_time.markers.directive import DIRECTIVES, HELD_BACK_OPT_INS, Consequence, ImageNotPulledReason, Reason
from update_time.markers.marker import Scope
from update_time.markers.reference import SteeredResolvedReference

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.dependency import ProjectGetter
    from update_time.domain.reference import Reference
    from update_time.io.log import Logger
    from update_time.markers.directive import RedundancyReason
    from update_time.markers.marker import Marker


def report_directives_that_set_nothing(
    marker: Marker, get_new_version: NewVersionGetter, reference: Reference, log: Logger
) -> None:
    """Report what sets nothing: each item with an inverted comparison, and each directive the source cannot apply."""
    log.report_inverted_items(reference, marker)
    as_written = marker.as_written
    for directive in DIRECTIVES:
        if (written := as_written.directive_for(directive.scope)) and not directive.is_applied_by(
            get_new_version, reference.pinned
        ):
            log.redundant_directive(reference, written, directive.reason)


def opt_in_redundancy(
    marker: Marker, scope: Scope, reason: RedundancyReason | None, *, update_held_back: bool
) -> RedundancyReason | None:
    """Return why the marker's opt-in to the scope is redundant, or None where the opt-in decides something.

    The reference's own `reason` wins, so removing a hold-back never changes it. Without one, the opt-in is redundant
    only where the update is held back.
    """
    if not marker.allows(scope):
        return None
    if reason is not None or not update_held_back:
        return reason
    return HELD_BACK_OPT_INS[scope]


def warn_if_the_opt_in_is_redundant(  # noqa: PLR0913 — whether the update is held back is the caller's to decide
    reference: Reference,
    marker: Marker,
    log: Logger,
    scope: Scope,
    reason: RedundancyReason | None,
    *,
    update_held_back: bool,
) -> None:
    """Warn when the marker's opt-in to the scope is redundant, saying why (see `opt_in_redundancy`)."""
    if (redundancy := opt_in_redundancy(marker, scope, reason, update_held_back=update_held_back)) is not None:
        log.redundant_directive(reference, marker.allow_directive(scope), redundancy)


def _why_the_pin_never_floats(latest: DependencyVersion | None) -> RedundancyReason | None:
    """Return why the reference's pin never floats, or None where it may float or the run did not resolve a version."""
    if latest is None:
        return None
    if latest.image_not_pulled is not None:
        return ImageNotPulledReason(latest.image_not_pulled, Consequence.TAG_NEVER_PINNED)
    return None if latest.floating is not None else Reason.PIN_NOT_FLOATING


def _warn_if_the_floating_pin_is_redundant(
    marker: Marker, reference: Reference, log: Logger, latest: DependencyVersion | None
) -> None:
    """Warn when the marker's directive to keep the pin floating is redundant, saying why."""
    reason = _why_the_pin_never_floats(latest)
    warn_if_the_opt_in_is_redundant(
        reference, marker, log, Scope.FLOATING_PIN, reason, update_held_back=marker.ignores(Scope.UPDATE)
    )


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
    if not downgrades(get_new_version, reference.pinned):
        log.warn_if_redundant_bound(reference, marker)
    report_directives_that_set_nothing(marker, get_new_version, reference, log)
    if marker.holds_back_source_checks:
        _warn_if_the_floating_pin_is_redundant(marker, reference, log, latest=None)
        return None
    version_bound = BLOCK_ALL_UPDATES if marker.ignores(Scope.UPDATE) else marker.version_bound
    latest = get_new_version(
        reference.pinned, version_bound, cooldown_days(marker), check_archival=archival_is_checked()
    )
    resolved = SteeredResolvedReference.from_reference(reference, release=latest, marker=marker)
    report_project(resolved, log)
    log.report_yank(resolved, marker)
    _warn_if_the_floating_pin_is_redundant(marker, reference, log, latest)
    return None if marker.ignores(Scope.UPDATE) else latest


def project_is_checked(source: object, dependency: DependencyName, threshold: int) -> bool:
    """Return whether a project check runs: the staleness check at this threshold, or the archival check.

    The archival check has no threshold of its own, so `--ignore-archived` is what switches it off.
    """
    return threshold != NO_STALENESS_CHECK or (archival_is_checked() and reports_archival(source, dependency))


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

"""Resolve which version a pinned reference should update to, the decision shared by every reference kind.

Given a reference's current version and its `# update-time:` marker, decide which version to update it to and
report on the decision through a `Logger`. The source is injected as a `NewVersionGetter`, so the decision is the
same whatever registry the reference points at; a reference kind with extra concerns (a commit SHA to pin, a
re-pushed digest to adopt) layers those on top of the version this decision resolves.

A reference the run resolves no update for takes `report_project_checks` instead.
"""

from typing import TYPE_CHECKING

from update_time.domain.archival import reports_archival
from update_time.domain.bound import BLOCK_ALL_UPDATES
from update_time.domain.dependency import DependencyName, DependencyVersion
from update_time.domain.image_lookup import why_image_not_looked_up
from update_time.domain.staleness import STALE_AFTER, release_judged_alike, stale_release, staleness_is_checked
from update_time.markers.cooldown import cooldown_days
from update_time.markers.directive import DIRECTIVES, HELD_BACK_OPT_INS, Consequence, ImageNotLookedUpReason, Reason
from update_time.markers.marker import Scope
from update_time.markers.reference import SteeredResolvedReference

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.dependency import ProjectGetter
    from update_time.domain.reference import Reference
    from update_time.io.log import Logger
    from update_time.markers.directive import RedundancyReason
    from update_time.markers.marker import Marker


def report_directives_the_source_cannot_apply(
    marker: Marker, get_new_version: NewVersionGetter, reference: Reference, log: Logger
) -> None:
    """Report each directive the source cannot apply, since it sets nothing."""
    image_not_looked_up = why_image_not_looked_up(get_new_version, reference.pinned)
    for directive in DIRECTIVES:
        if (written := marker.directive_for(directive.scope)) and not directive.is_applied_by(
            get_new_version, reference.pinned
        ):
            log.redundant_directive(reference, written, directive.reason_given(image_not_looked_up))


def _opt_in_redundancy(
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
    """Warn when the marker's opt-in to the scope is redundant, saying why (see `_opt_in_redundancy`)."""
    if (redundancy := _opt_in_redundancy(marker, scope, reason, update_held_back=update_held_back)) is not None:
        log.redundant_directive(reference, marker.allow_directive(scope), redundancy)


def _why_the_pin_never_floats(latest: DependencyVersion) -> RedundancyReason | None:
    """Return why the reference's pin never floats, or None where it may float."""
    if latest.image_not_looked_up is not None:
        return ImageNotLookedUpReason(latest.image_not_looked_up, Consequence.TAG_NEVER_PINNED)
    return None if latest.floating is not None else Reason.PIN_NOT_FLOATING


def _warn_if_the_floating_pin_is_redundant(
    marker: Marker, reference: Reference, log: Logger, latest: DependencyVersion
) -> None:
    """Warn when the marker's directive to keep the pin floating is redundant, saying why."""
    reason = _why_the_pin_never_floats(latest)
    warn_if_the_opt_in_is_redundant(
        reference, marker, log, Scope.FLOATING_PIN, reason, update_held_back=marker.ignores(Scope.UPDATE)
    )


def _staleness_threshold(marker: Marker) -> int:
    """Return the number of days the reference is checked for staleness against: its own, or the run's."""
    return marker.stale.value_or(STALE_AFTER.get())


def latest_version(
    reference: Reference,
    get_new_version: NewVersionGetter,
    marker: Marker,
    log: Logger,
) -> DependencyVersion | None:
    """Return the latest version to update the reference to, or None when the marker holds the update back."""
    log.warn_if_redundant_bound(reference, marker)
    report_directives_the_source_cannot_apply(marker, get_new_version, reference, log)
    version_bound = BLOCK_ALL_UPDATES if marker.ignores(Scope.UPDATE) else marker.version_bound
    days = cooldown_days(marker)
    latest = get_new_version(reference.pinned, version_bound, days)
    if (fresh := latest.fresh_release) is not None:
        log.held_back_by_cooldown(reference, fresh, days, marker.cooldown_directive)
    resolved = SteeredResolvedReference.from_reference(reference, release=latest, marker=marker)
    report_project(resolved, log)
    log.report_yank(resolved, marker)
    _warn_if_the_floating_pin_is_redundant(marker, reference, log, latest)
    return None if marker.ignores(Scope.UPDATE) else latest


def project_is_checked(source: object, dependency: DependencyName, marker: Marker) -> bool:
    """Return whether a project check runs: the run's staleness check, the archival check, or the marker's threshold.

    The marker's threshold is judged for redundancy against the newest release, a threshold of 0 days included.
    """
    if marker.stale.value is not None or staleness_is_checked(STALE_AFTER.get()):
        return True
    return reports_archival(source, dependency)


def report_project(resolved: SteeredResolvedReference, log: Logger) -> None:
    """Report the staleness and the archival of a project a source has already answered for."""
    log.report_staleness(resolved, _staleness_threshold(resolved.marker))
    _log_if_the_staleness_threshold_silences_a_warning(resolved, log)
    _warn_if_the_staleness_threshold_is_redundant(resolved, log)
    log.report_archival(resolved, resolved.marker)


def _log_if_the_staleness_threshold_silences_a_warning(resolved: SteeredResolvedReference, log: Logger) -> None:
    """Log that the marker's staleness threshold keeps quiet about a newest release the run's threshold warns about."""
    threshold = resolved.marker.stale
    if threshold.value is None or stale_release(resolved.release, threshold.value) is not None:
        return
    if stale_release(resolved.release, STALE_AFTER.get()) is not None:
        log.ignored_staleness(resolved, threshold.directive)


def _warn_if_the_staleness_threshold_is_redundant(resolved: SteeredResolvedReference, log: Logger) -> None:
    """Warn when the marker's staleness threshold judges the newest release as the run's threshold does."""
    if (threshold := resolved.marker.stale).value is None:
        return
    run_threshold = STALE_AFTER.get()
    if (newest := release_judged_alike(resolved.release, threshold.value, run_threshold)) is not None:
        log.redundant_stale_threshold(resolved, threshold, newest, run_threshold)


def report_project_checks(reference: Reference, marker: Marker, log: Logger, get_project: ProjectGetter) -> None:
    """Ask the source about the reference's project, where a check needs it, and report what it answers."""
    if not project_is_checked(get_project, reference.dependency, marker):
        return
    release = DependencyVersion.unpinned(get_project(reference.dependency))
    resolved = SteeredResolvedReference.from_reference(reference, release=release, marker=marker)
    report_project(resolved, log)

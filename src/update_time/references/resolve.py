"""Resolve which version a pinned reference should update to, the decision shared by every reference kind.

Given a reference's current version and its `# update-time:` marker, decide which version to update it to and
report on the decision through a `Logger`. The source is injected as the record of its getters, so the decision is
the same whatever registry the reference points at; a reference kind with extra concerns (a commit SHA to pin, a
re-pushed digest to adopt) layers those on top of the version this decision resolves.

A reference the run resolves no update for takes `report_project_checks` instead.
"""

from typing import TYPE_CHECKING

from update_time.domain.bound import BLOCK_ALL_UPDATES
from update_time.domain.dependency import NO_PROJECT, NO_RELEASE, DependencyVersion, Project, Unserved
from update_time.domain.directive import DIRECTIVES, Consequence, ImageNotLookedUpReason, Reason
from update_time.domain.marker import Scope
from update_time.domain.reference import Reference, ResolvedReference
from update_time.domain.staleness import STALE_AFTER, release_judged_alike, stale_release, staleness_is_checked
from update_time.markers.report import warn_if_the_bound_is_redundant, warn_if_the_opt_in_is_redundant

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyName, ImageNotLookedUp, ProjectGetter
    from update_time.domain.directive import RedundancyReason
    from update_time.domain.source import Source
    from update_time.io.log import Logger


def latest_version(reference: Reference, source: Source, log: Logger) -> DependencyVersion | None:
    """Return the latest version to update the reference to, or None when its marker holds the update back."""
    marker = reference.marker
    new_version = source.new_version
    never_updated = source.why_never_updated(reference.pinned)
    _warn_about_redundant_update_directives(reference, never_updated, log)
    report_directives_the_source_cannot_apply(reference, source, log)
    version_bound = BLOCK_ALL_UPDATES if marker.ignores(Scope.UPDATE) else marker.version_bound
    days = reference.cooldown_days
    image_not_looked_up = source.why_image_not_looked_up(reference.pinned)
    if new_version is None or image_not_looked_up is not None:
        latest, project = DependencyVersion(version=reference.current_version), NO_PROJECT
    else:
        latest = new_version(reference.pinned, version_bound, days)
        project = _reported_project(latest, reference, source)
    if (fresh := latest.fresh_release) is not None:
        log.held_back_by_cooldown(reference, fresh, days, marker.cooldown_directive)
    resolved = ResolvedReference.from_reference(reference, release=latest, project=project)
    _report_project(resolved, log)
    report_yank(resolved, log)
    never_floats = _why_the_pin_never_floats(latest, image_not_looked_up)
    warn_if_the_opt_in_is_redundant(reference, Scope.FLOATING_PIN, never_floats, log)
    warn_if_the_opt_in_is_redundant(reference, Scope.HASH_DRIFT, _why_drift_is_never_adopted(reference, source), log)
    return None if marker.ignores(Scope.UPDATE) and never_updated is None else latest


def _warn_about_redundant_update_directives(
    reference: Reference, never_updated: ImageNotLookedUp | None, log: Logger
) -> None:
    """Warn about the marker's bound and `ignore[update]` where they decide nothing, as on an image never updated."""
    marker = reference.marker
    if never_updated is not None:
        reason = ImageNotLookedUpReason(never_updated, Consequence.NEVER_UPDATED)
        for directive in filter(None, (marker.scope_directive(Scope.UPDATE), marker.version_bound_directive)):
            log.redundant_directive(reference, directive, reason)
    else:
        warn_if_the_bound_is_redundant(reference, log)


def report_directives_the_source_cannot_apply(reference: Reference, source: Source, log: Logger) -> None:
    """Report each directive the source cannot apply, since it sets nothing."""
    image_not_looked_up = source.why_image_not_looked_up(reference.pinned)
    for directive in DIRECTIVES:
        written = reference.marker.directive_for(directive.scope)
        if written and not directive.is_applied_by(source, reference.pinned):
            log.redundant_directive(reference, written, directive.reason_given(image_not_looked_up))


def _reported_project(latest: DependencyVersion, reference: Reference, source: Source) -> Project:
    """Return what the source reports about the reference's project, where the source has a say and a check needs it.

    Where the source does not serve the reference's repository, the reference does not get a project.
    """
    if source.project is None or latest.unserved is Unserved.REPOSITORY:
        return NO_PROJECT
    if latest.unserved is Unserved.VERSION:
        return _archival_alone(source.project, reference.dependency)
    if not _project_is_checked(source.project, reference):
        return NO_PROJECT
    return source.project(reference.dependency)


def _archival_alone(get_project: ProjectGetter, dependency: DependencyName) -> Project:
    """Return the project of a dependency whose version the source does not serve, without its newest release.

    The newest release does not date such a dependency, so the source is asked about the project only where the
    source declares the dependency's archival.
    """
    if not get_project.declares_archival(dependency):
        return NO_PROJECT
    return Project(archival=get_project(dependency).archival)


def _project_is_checked(get_project: ProjectGetter, reference: Reference) -> bool:
    """Return whether a project check runs: the run's staleness check, the archival check, or the marker's threshold.

    The marker's threshold is judged for redundancy against the newest release, a threshold of 0 days included.
    """
    if reference.marker.stale.value is not None or staleness_is_checked(STALE_AFTER.get()):
        return True
    return get_project.declares_archival(reference.dependency)


def _report_project(resolved: ResolvedReference, log: Logger) -> None:
    """Report the staleness and the archival of a project a source has already answered for."""
    _warn_if_stale(resolved, log)
    _log_if_the_staleness_threshold_silences_a_warning(resolved, log)
    _warn_if_the_staleness_threshold_is_redundant(resolved, log)
    _report_archival(resolved, log)


def _warn_if_stale(resolved: ResolvedReference, log: Logger) -> None:
    """Warn when the project's newest release is older than the reference's staleness threshold."""
    if (newest := stale_release(resolved.project, resolved.stale_after)) is not None:
        log.stale_dependency(resolved, newest, resolved.stale_after)


def _log_if_the_staleness_threshold_silences_a_warning(resolved: ResolvedReference, log: Logger) -> None:
    """Log that the marker's staleness threshold keeps quiet about a newest release the run's threshold warns about."""
    threshold = resolved.marker.stale
    if threshold.value is None or stale_release(resolved.project, threshold.value) is not None:
        return
    if stale_release(resolved.project, STALE_AFTER.get()) is not None:
        log.ignored_warning(resolved, Scope.STALE, threshold.directive)


def _warn_if_the_staleness_threshold_is_redundant(resolved: ResolvedReference, log: Logger) -> None:
    """Warn when the marker's staleness threshold judges the newest release as the run's threshold does."""
    if (threshold := resolved.marker.stale).value is None:
        return
    run_threshold = STALE_AFTER.get()
    if (newest := release_judged_alike(resolved.project, threshold.value, run_threshold)) is not None:
        log.redundant_stale_threshold(resolved, threshold, newest, run_threshold)


def _report_archival(resolved: ResolvedReference, log: Logger) -> None:
    """Warn about the project's archival, or report the marker that silences it or has nothing to silence.

    A source that does not declare whether it archived the project leaves nothing to report.
    """
    if (archival := resolved.project.archival) is None:
        return
    directive = resolved.marker.directive_for(Scope.ARCHIVED)
    if archival.archived and directive:
        log.ignored_warning(resolved, Scope.ARCHIVED, directive)
    elif archival.archived:
        log.archived_dependency(resolved, archival)
    elif directive:
        log.redundant_ignore(resolved, Scope.ARCHIVED, directive, archival.subject)


def report_yank(resolved: ResolvedReference, log: Logger) -> None:
    """Warn about the version's yank, or report the marker that silences it or has nothing to silence.

    A source that did not say whether it yanked the version leaves nothing to report.
    """
    if (yank := resolved.release.yank) is None:
        return
    directive = resolved.marker.directive_for(Scope.YANKED)
    if yank.yanked and directive:
        log.ignored_warning(resolved, Scope.YANKED, directive)
    elif yank.yanked:
        log.yanked_dependency(resolved)
    elif directive:
        log.redundant_ignore(resolved, Scope.YANKED, directive, resolved.release.version)


def _why_the_pin_never_floats(
    latest: DependencyVersion, image_not_looked_up: ImageNotLookedUp | None
) -> RedundancyReason | None:
    """Return why the reference's pin never floats, or None where it may float."""
    if image_not_looked_up is not None:
        return ImageNotLookedUpReason(image_not_looked_up, Consequence.TAG_NEVER_PINNED)
    return None if latest.floating is not None else Reason.PIN_NOT_FLOATING


def _why_drift_is_never_adopted(reference: Reference, source: Source) -> RedundancyReason | None:
    """Return why the reference cannot adopt drift, whatever its marker says, or None where it can."""
    if not source.adopts_hash_drift:
        return Reason.INTEGRITY_MISMATCH_NEVER_ADOPTED
    if not reference.can_hold_a_hash_pin:
        return Reason.NO_HASH_PIN
    if (image_not_looked_up := source.why_image_not_looked_up(reference.pinned)) is not None:
        return ImageNotLookedUpReason(image_not_looked_up, Consequence.DIGEST_NEVER_CHECKED)
    return None


def report_project_checks(reference: Reference, log: Logger, get_project: ProjectGetter) -> None:
    """Ask the source about the reference's project, where a check needs it, and report what it answers."""
    if not _project_is_checked(get_project, reference):
        return
    resolved = ResolvedReference.from_reference(
        reference, release=NO_RELEASE, project=get_project(reference.dependency)
    )
    _report_project(resolved, log)

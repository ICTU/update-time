"""The checks a dependency gets when a package manager updates it rather than Update-time rewriting its line."""

from functools import partial
from itertools import chain
from typing import TYPE_CHECKING

from update_time.domain.archival import archival_is_checked, archival_reporting, reports_archival
from update_time.domain.dependency import DependencyVersion
from update_time.domain.reference import resolved_references
from update_time.markers.directive import WARNING_DIRECTIVES, Reason
from update_time.markers.marker import Scope
from update_time.markers.reference import SteeredReference, SteeredResolvedReference
from update_time.references.resolve import (
    project_is_checked,
    report_project,
    staleness_threshold,
)
from update_time.references.vulnerability import warn_about_vulnerable_dependencies

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from update_time.domain.dependency import DependencyName, ProjectGetter, VersionString, Yank
    from update_time.domain.reference import ReferenceResolver
    from update_time.io.log import Logger
    from update_time.markers.directive import RedundancyReason
    from update_time.package_managers.policy import ManagerPolicy
    from update_time.sources.osv import Ecosystem


def project_resolver(get_project: ProjectGetter) -> ReferenceResolver[SteeredReference, SteeredResolvedReference]:
    """Return a resolver carrying what the source reports about the project behind each reference.

    The resolver is registered as reporting archival wherever its source is, so the checks can read that off it.
    """

    def resolve(references: Iterable[SteeredReference]) -> Iterable[SteeredResolvedReference]:
        check_archival = archival_is_checked()
        return (
            SteeredResolvedReference.from_reference(
                reference,
                release=DependencyVersion.unpinned(get_project(reference.dependency, check_archival=check_archival)),
            )
            for reference in references
        )

    return archival_reporting(resolve, when=partial(reports_archival, get_project))


def pin_resolver(
    get_yank: Callable[[DependencyName, VersionString], Yank],
) -> ReferenceResolver[SteeredReference, SteeredResolvedReference]:
    """Return a resolver carrying the release each pin names, with the yank state the source reports for it.

    The release is the version the file records rather than the newest the source offers, so it is the version the
    run leaves the pin on, whichever version the package manager settled the pin on.
    """

    def resolve(pins: Iterable[SteeredReference]) -> Iterable[SteeredResolvedReference]:
        return (
            SteeredResolvedReference.from_reference(
                pin, release=DependencyVersion(pin.current_version, yank=get_yank(pin.dependency, pin.current_version))
            )
            for pin in pins
        )

    return resolve


def warn_about_projects[ReferenceT: SteeredReference](
    declared: Iterable[Iterable[ReferenceT]],
    projects: ReferenceResolver[ReferenceT, SteeredResolvedReference],
    log: Logger,
) -> None:
    """Ask the resolver about each reference a check needs it for, and report what it answers.

    A reference carrying a staleness threshold of its own is asked about at that threshold, so it is looked up in a
    run that switches both project checks off.
    """
    checked = [
        references for declared_in_file in declared if (references := _checked_references(projects, declared_in_file))
    ]
    for resolved in resolved_references(checked, projects):
        report_project(resolved, log)


def _asks_its_source(reference: SteeredReference) -> bool:
    """Return whether a check still asks the reference's own source about it.

    A marker holding back every check that source answers leaves nothing to ask it for, so the reference is left
    out and costs no request.
    """
    return not reference.marker.holds_back_source_checks


def _checked_references[ReferenceT: SteeredReference](
    projects: ReferenceResolver[ReferenceT, SteeredResolvedReference], declared: Iterable[ReferenceT]
) -> list[ReferenceT]:
    """Return the file's references a project check runs for, each at the staleness threshold its marker sets."""
    return [
        reference
        for reference in declared
        if _asks_its_source(reference)
        and project_is_checked(projects, reference.dependency, staleness_threshold(reference.marker))
    ]


def _redundant_warning_directives(
    reference: SteeredReference, unchecked: Mapping[Scope, Reason]
) -> Iterator[tuple[str, RedundancyReason]]:
    """Yield each warning scope the reference's marker writes that holds nothing back, with the reason it is redundant.

    A scope that Update-time leaves unchecked takes the reason `unchecked` gives for it. A check about the version a
    reference is left on has none to run on where the reference pins none, so its scope holds nothing back there.
    """
    as_written = reference.marker.as_written
    for directive in WARNING_DIRECTIVES:
        if not (written := as_written.directive_for(directive.scope)):
            continue
        reason = unchecked.get(directive.scope)
        if reason is None and not reference.current_version:
            reason = directive.without_a_version
        if reason is not None:
            yield written, reason


def _redundant_update_directives[ReferenceT: SteeredReference](
    reference: ReferenceT, policy: ManagerPolicy[ReferenceT]
) -> Iterator[tuple[str, RedundancyReason]]:
    """Yield each redundant directive steering the update, with the reason it is redundant.

    An `ignore[update]` holds something back only where the policy freezes the reference's pin. A bound narrows
    candidates the package manager never offers, so it decides nothing whatever the reference pins, and whatever an
    `ignore[update]` beside it holds back.
    """
    as_written = reference.marker.as_written
    if as_written.ignores(Scope.UPDATE) and not policy.freezes_the_pin(reference):
        yield as_written.bound_directive, Reason.MANAGER_RESOLVES_THE_VERSION
    if bound := as_written.version_bound_directive:
        yield bound, Reason.BOUND_DECIDES_NOTHING


def _redundant_cooldown_directive(reference: SteeredReference) -> Iterator[tuple[str, RedundancyReason]]:
    """Yield the cooldown the marker sets, which the package manager applies per run rather than per reference."""
    if cooldown := reference.marker.as_written.cooldown_directive:
        yield cooldown, Reason.COOLDOWN_PER_RUN


def _redundant_floating_pin_directive(reference: SteeredReference) -> Iterator[tuple[str, RedundancyReason]]:
    """Yield the floating-pin directive the reference's marker writes, with the reason it keeps nothing floating."""
    if reference.marker.allows(Scope.FLOATING_PIN):
        reason = Reason.PIN_NOT_FLOATING if reference.current_version else Reason.RANGE_NEVER_PINNED
        yield reference.marker.allow_directive(Scope.FLOATING_PIN), reason


def _redundant_hash_drift_directive(reference: SteeredReference) -> Iterator[tuple[str, RedundancyReason]]:
    """Yield the hash-drift directive the reference's marker writes, with the reason it adopts nothing."""
    if reference.marker.allows(Scope.HASH_DRIFT):
        yield reference.marker.allow_directive(Scope.HASH_DRIFT), Reason.NO_HASH_PIN


def _redundant_directives[ReferenceT: SteeredReference](
    reference: ReferenceT, policy: ManagerPolicy[ReferenceT]
) -> Iterator[tuple[str, RedundancyReason]]:
    """Yield each redundant directive of the reference's marker, with the reason it is redundant."""
    yield from _redundant_update_directives(reference, policy)
    yield from _redundant_cooldown_directive(reference)
    yield from _redundant_floating_pin_directive(reference)
    yield from _redundant_hash_drift_directive(reference)
    yield from _redundant_warning_directives(reference, policy.unchecked_scopes(reference))


def warn_about_redundant_directives[ReferenceT: SteeredReference](
    declared: Iterable[Iterable[ReferenceT]], log: Logger, policy: ManagerPolicy[ReferenceT]
) -> None:
    """Warn about each redundant directive of a reference's marker, saying why it is redundant."""
    for reference in chain.from_iterable(declared):
        for written, reason in _redundant_directives(reference, policy):
            log.redundant_directive(reference, written, reason)


def _warn_about_yanked_dependencies[ReferenceT: SteeredReference](
    declared: Iterable[Iterable[ReferenceT]],
    pinned_releases: ReferenceResolver[ReferenceT, SteeredResolvedReference],
    log: Logger,
) -> None:
    """Warn about each pin the run leaves on a withdrawn release, unless the pin's marker silences the warning."""
    checked = [[pin for pin in pins if _asks_its_source(pin)] for pins in declared]
    for resolved in resolved_references(checked, pinned_releases):
        log.report_yank(resolved, resolved.marker)


def warn_about_delegated_checks[ReferenceT: SteeredReference](
    served: Iterable[Iterable[ReferenceT]],
    log: Logger,
    projects: ReferenceResolver[ReferenceT, SteeredResolvedReference],
    yank: Callable[[DependencyName, VersionString], Yank],
    ecosystem: Ecosystem,
) -> None:
    """Run the project, yank, and vulnerability checks over the dependencies a source serves a release for.

    Each file's dependencies are walked by every check, so each is a list rather than a generator.
    """
    warn_about_projects(served, projects, log)
    pinned = [[reference for reference in references if reference.current_version] for references in served]
    _warn_about_yanked_dependencies(pinned, pin_resolver(yank), log)
    warn_about_vulnerable_dependencies(pinned, ecosystem, log)

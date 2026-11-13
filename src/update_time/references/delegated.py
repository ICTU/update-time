"""The checks a dependency gets when a package manager updates it rather than Update-time rewriting its line."""

from itertools import chain
from typing import TYPE_CHECKING

from update_time.domain.dependency import DependencyVersion
from update_time.domain.directive import WARNING_DIRECTIVES, Reason
from update_time.domain.marker import Scope
from update_time.domain.reference import Reference, ResolvedReference, resolved_references
from update_time.references.resolve import report_project_checks
from update_time.references.vulnerability import warn_about_vulnerable_dependencies

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    from update_time.domain.dependency import ProjectGetter, YankGetter
    from update_time.domain.directive import RedundancyReason
    from update_time.domain.reference import ReferenceResolver
    from update_time.domain.source import Source
    from update_time.io.log import Logger
    from update_time.package_managers.policy import ManagerPolicy


def pin_resolver(
    get_yank: YankGetter,
) -> ReferenceResolver[Reference, ResolvedReference]:
    """Return a resolver carrying the release each pin names, with the yank state the source reports for it.

    The release is the version the file records rather than the newest the source offers, so it is the version the
    run leaves the pin on, whichever version the package manager settled the pin on.
    """

    def resolve(pins: Iterable[Reference]) -> Iterable[ResolvedReference]:
        return (
            ResolvedReference.from_reference(
                pin, release=DependencyVersion(pin.current_version, yank=get_yank(pin.dependency, pin.current_version))
            )
            for pin in pins
        )

    return resolve


def warn_about_projects(declared: Iterable[Iterable[Reference]], get_project: ProjectGetter, log: Logger) -> None:
    """Ask the source about the project of each reference a check needs it for, and report what it answers."""
    for reference in chain.from_iterable(declared):
        report_project_checks(reference, log, get_project)


def _redundant_warning_directives(
    reference: Reference, unchecked: Mapping[Scope, Reason]
) -> Iterator[tuple[str, RedundancyReason]]:
    """Yield each warning scope the reference's marker writes that holds nothing back, with the reason it is redundant.

    A scope that Update-time leaves unchecked takes the reason `unchecked` gives for it. A check about the version a
    reference is left on has none to run on where the reference pins none, so its scope holds nothing back there.
    """
    for directive in WARNING_DIRECTIVES:
        if not (written := reference.marker.directive_for(directive.scope)):
            continue
        reason = unchecked.get(directive.scope)
        if reason is None and not reference.current_version:
            reason = directive.without_a_version
        if reason is not None:
            yield written, reason


def _redundant_update_directives[ReferenceT: Reference](
    reference: ReferenceT, policy: ManagerPolicy[ReferenceT]
) -> Iterator[tuple[str, RedundancyReason]]:
    """Yield each redundant directive steering the update, with the reason it is redundant.

    The policy says why an `ignore[update]` or a bound decides nothing for the reference, whatever an `ignore[update]`
    beside a bound holds back.
    """
    if reference.marker.ignores(Scope.UPDATE) and (reason := policy.hold_back_redundancy(reference)):
        yield reference.marker.bound_directive, reason
    if (bound := reference.marker.version_bound_directive) and (reason := policy.bound_redundancy(reference)):
        yield bound, reason


def _redundant_cooldown_directive(reference: Reference, reason: Reason) -> Iterator[tuple[str, RedundancyReason]]:
    """Yield the cooldown the marker sets, with the reason it holds nothing back."""
    if cooldown := reference.marker.cooldown_directive:
        yield cooldown, reason


def _redundant_floating_pin_directive(reference: Reference) -> Iterator[tuple[str, RedundancyReason]]:
    """Yield the floating-pin directive the reference's marker writes, with the reason it keeps nothing floating."""
    if reference.marker.allows(Scope.FLOATING_PIN):
        reason = Reason.PIN_NOT_FLOATING if reference.current_version else Reason.RANGE_NEVER_PINNED
        yield reference.marker.allow_directive(Scope.FLOATING_PIN), reason


def _redundant_hash_drift_directive(reference: Reference) -> Iterator[tuple[str, RedundancyReason]]:
    """Yield the hash-drift directive the reference's marker writes, with the reason it adopts nothing."""
    if reference.marker.allows(Scope.HASH_DRIFT):
        yield reference.marker.allow_directive(Scope.HASH_DRIFT), Reason.NO_HASH_PIN


def _redundant_directives[ReferenceT: Reference](
    reference: ReferenceT, policy: ManagerPolicy[ReferenceT]
) -> Iterator[tuple[str, RedundancyReason]]:
    """Yield each redundant directive of the reference's marker, with the reason it is redundant."""
    yield from _redundant_update_directives(reference, policy)
    yield from _redundant_cooldown_directive(reference, policy.cooldown_redundancy)
    yield from _redundant_floating_pin_directive(reference)
    yield from _redundant_hash_drift_directive(reference)
    yield from _redundant_warning_directives(reference, policy.unchecked_scopes(reference))


def warn_about_redundant_directives[ReferenceT: Reference](
    declared: Iterable[Iterable[ReferenceT]], log: Logger, policy: ManagerPolicy[ReferenceT]
) -> None:
    """Warn about each redundant directive of a reference's marker, saying why it is redundant."""
    for reference in chain.from_iterable(declared):
        for written, reason in _redundant_directives(reference, policy):
            log.redundant_directive(reference, written, reason)


def _warn_about_yanked_dependencies[ReferenceT: Reference](
    declared: Iterable[Iterable[ReferenceT]],
    pinned_releases: ReferenceResolver[ReferenceT, ResolvedReference],
    log: Logger,
) -> None:
    """Warn about each pin the run leaves on a withdrawn release, unless the pin's marker silences the warning."""
    for resolved in resolved_references(declared, pinned_releases):
        log.report_yank(resolved)


def warn_about_delegated_checks[ReferenceT: Reference](
    served: Sequence[Sequence[ReferenceT]], log: Logger, source: Source
) -> None:
    """Run the project, yank, and vulnerability checks over the dependencies the source serves a release for.

    Each file's dependencies are walked by every check, so each is a list rather than a generator.
    """
    if source.project is not None:
        warn_about_projects(served, source.project, log)
    pinned = [[reference for reference in references if reference.current_version] for references in served]
    warn_about_pinned_versions(pinned, log, source)


def warn_about_pinned_versions[ReferenceT: Reference](
    pinned: Sequence[Sequence[ReferenceT]], log: Logger, source: Source
) -> None:
    """Run the yank and vulnerability checks over the versions the dependencies are pinned to."""
    if source.yank is not None:
        _warn_about_yanked_dependencies(pinned, pin_resolver(source.yank), log)
    warn_about_vulnerable_dependencies(pinned, source, log)

"""The checks a dependency gets when a package manager updates it rather than Update-time rewriting its line."""

from itertools import chain
from typing import TYPE_CHECKING

from update_time.domain.reference import resolved_references
from update_time.markers.directive import WARNING_DIRECTIVES, Reason
from update_time.markers.marker import Scope
from update_time.markers.reference import SteeredReference
from update_time.references.resolve import (
    floating_pin_redundancy,
    project_is_checked,
    report_project,
    staleness_threshold,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from update_time.domain.reference import ReferenceResolver
    from update_time.io.log import Logger
    from update_time.markers.reference import SteeredResolvedReference


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


def _scopes_that_need_a_version(reference: SteeredReference) -> Iterator[tuple[str, Reason]]:
    """Yield each directive whose check needs a version the reference pins none of, with the reason it is redundant.

    A check about the version a reference is left on has none to run on where the reference pins none, so its scope
    holds nothing back there. The scopes with no such reason are the ones a check runs for whatever is pinned.
    """
    if reference.current_version:
        return
    as_written = reference.marker.as_written
    for directive in WARNING_DIRECTIVES:
        if directive.without_a_version is not None and (written := as_written.directive_for(directive.scope)):
            yield written, directive.without_a_version


def _redundant_update_directives(reference: SteeredReference) -> Iterator[tuple[str, Reason]]:
    """Yield each redundant directive steering the update, with the reason it is redundant.

    An `ignore[update]` freezes the version the file records. So it holds something back for a reference that pins
    a version, and nothing for one that pins none. A bound narrows candidates the package manager never offers, so
    it decides nothing whatever the reference pins, and whatever an `ignore[update]` beside it holds back.
    """
    as_written = reference.marker.as_written
    if as_written.ignores(Scope.UPDATE) and not reference.current_version:
        yield as_written.bound_directive, Reason.MANAGER_RESOLVES_THE_VERSION
    if bound := as_written.version_bound_directive:
        yield bound, Reason.BOUND_DECIDES_NOTHING


def _redundant_cooldown_directive(reference: SteeredReference) -> Iterator[tuple[str, Reason]]:
    """Yield the cooldown the marker sets, which the package manager applies per run rather than per reference."""
    if cooldown := reference.marker.as_written.cooldown_directive:
        yield cooldown, Reason.COOLDOWN_PER_RUN


def _redundant_floating_pin_directive(reference: SteeredReference) -> Iterator[tuple[str, Reason]]:
    """Yield the floating-pin directive the reference's marker writes, with the reason it keeps nothing floating.

    The marker is read whole rather than as written, since a bare `ignore` holds the update back without naming it.
    """
    if (floating := floating_pin_redundancy(reference.marker, floats=False)) is not None:
        yield reference.marker.allow_directive(Scope.FLOATING_PIN), floating


def _warning_scopes(reference: SteeredReference, reason: Reason) -> Iterator[tuple[str, Reason]]:
    """Yield each warning scope the reference's marker writes, with the reason no source answers any of them."""
    as_written = reference.marker.as_written
    for directive in WARNING_DIRECTIVES:
        if written := as_written.directive_for(directive.scope):
            yield written, reason


def _redundant_directives(reference: SteeredReference, no_source: Reason | None) -> Iterator[tuple[str, Reason]]:
    """Yield each redundant directive of the reference's marker, with the reason it is redundant."""
    yield from _redundant_update_directives(reference)
    yield from _redundant_cooldown_directive(reference)
    yield from _redundant_floating_pin_directive(reference)
    if no_source is None:
        yield from _scopes_that_need_a_version(reference)
    else:
        yield from _warning_scopes(reference, no_source)


def warn_about_redundant_directives[ReferenceT: SteeredReference](
    declared: Iterable[Iterable[ReferenceT]],
    log: Logger,
    no_source_for: Callable[[ReferenceT], Reason | None],
) -> None:
    """Warn about each redundant directive of a reference's marker, saying why it is redundant.

    `no_source_for` answers why no source reports anything about a reference, or None where one of them does.
    """
    for reference in chain.from_iterable(declared):
        for written, reason in _redundant_directives(reference, no_source_for(reference)):
            log.redundant_directive(reference, written, reason)


def warn_about_yanked_dependencies[ReferenceT: SteeredReference](
    declared: Iterable[Iterable[ReferenceT]],
    pinned_releases: ReferenceResolver[ReferenceT, SteeredResolvedReference],
    log: Logger,
) -> None:
    """Warn about each pin the run leaves on a withdrawn release, unless the pin's marker silences the warning."""
    checked = [[pin for pin in pins if _asks_its_source(pin)] for pins in declared]
    for resolved in resolved_references(checked, pinned_releases):
        log.report_yank(resolved, resolved.marker)

"""Report a reference's marker: what it says, what it gets wrong, and the directives that decide nothing."""

from typing import TYPE_CHECKING

from update_time.domain.directive import Reason, opt_in_redundancy
from update_time.domain.marker import Scope

if TYPE_CHECKING:
    from update_time.domain.directive import RedundancyReason
    from update_time.domain.reference import Reference
    from update_time.io.log import Logger


def report_marker(reference: Reference, log: Logger, *, steers_the_update: bool) -> None:
    """Report the reference's marker, logging an `ignore[update]` as holding the update back where it steers it."""
    marker = reference.marker
    for directive in marker.default_directives:
        log.redundant_directive(reference, directive, Reason.ASKS_FOR_THE_DEFAULT)
    if marker.invalid_item is not None:
        log.invalid_bracket_item(reference, marker.invalid_item)
    if str(marker):
        log.recognised_marker(reference)
    if steers_the_update and marker.ignores(Scope.UPDATE):
        log.ignored(reference)


def warn_if_the_bound_is_redundant(reference: Reference, log: Logger) -> None:
    """Warn where the marker's bound never has an effect, or blocks every update, at the reference's version."""
    if (redundancy := reference.marker.bound_redundancy_at(reference.current_version)) is not None:
        log.redundant_bound(reference, redundancy)


def warn_if_the_opt_in_is_redundant(
    reference: Reference, scope: Scope, reason: RedundancyReason | None, log: Logger
) -> None:
    """Warn where the marker's opt-in to the scope is redundant, saying why (see `opt_in_redundancy`)."""
    marker = reference.marker
    if (redundancy := opt_in_redundancy(marker, scope, reason)) is not None:
        log.redundant_directive(reference, marker.allow_directive(scope), redundancy)

"""Each package manager's policy for markers, and the report of the markers on the dependencies it updates."""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from update_time.io.log import report_marker
from update_time.markers.directive import WARNING_DIRECTIVES
from update_time.markers.marker import Scope
from update_time.markers.reference import SteeredReference

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from update_time.io.log import Logger
    from update_time.markers.directive import Reason
    from update_time.markers.marker import Marker


@dataclass(frozen=True)
class ManagerPolicy[ReferenceT: SteeredReference]:
    """Which directives steer the dependencies a package manager updates.

    `honours_a_freeze` says whether an `ignore[update]` keeps a pinned version where it is. `unchecked_scopes` names
    the warning scopes that Update-time leaves unchecked for a reference, each with the reason.
    """

    honours_a_freeze: bool
    unchecked_scopes: Callable[[ReferenceT], Mapping[Scope, Reason]]

    @staticmethod
    def every_warning_scope(reason: Reason) -> dict[Scope, Reason]:
        """Return every warning scope, each left unchecked for the reason."""
        return {directive.scope: reason for directive in WARNING_DIRECTIVES}

    def freezes_the_pin(self, reference: ReferenceT) -> bool:
        """Return whether an `ignore[update]` keeps the version the reference pins where it is."""
        return self.honours_a_freeze and bool(reference.current_version)


def report_markers[ReferenceT: SteeredReference](
    references: Iterable[ReferenceT], log: Logger, policy: ManagerPolicy[ReferenceT]
) -> list[ReferenceT]:
    """Report each reference's marker, and return the references with each marker frozen where an item is invalid."""
    return [replace(reference, marker=_report_reference_marker(reference, log, policy)) for reference in references]


def _report_reference_marker[ReferenceT: SteeredReference](
    reference: ReferenceT, log: Logger, policy: ManagerPolicy[ReferenceT]
) -> Marker:
    """Report the reference's marker, and return it, frozen where an item is invalid."""
    holds_the_update_back = policy.freezes_the_pin(reference) and reference.marker.ignores(Scope.UPDATE)
    marker = report_marker(
        log, reference.dependency, reference.marker, reference.location, holds_the_update_back=holds_the_update_back
    )
    log.report_inverted_items(reference, reference.marker)
    return marker

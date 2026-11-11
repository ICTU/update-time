"""Each package manager's policy for markers, and the report of the markers on the dependencies it updates."""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from update_time.markers.directive import WARNING_DIRECTIVES
from update_time.markers.marker import Scope
from update_time.markers.reference import SteeredReference

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from update_time.io.log import Logger
    from update_time.markers.directive import Reason


@dataclass(frozen=True)
class ManagerPolicy[ReferenceT: SteeredReference]:
    """Which directives steer the dependencies a package manager updates.

    `hold_back_redundancy` gives the reason an `ignore[update]` keeps nothing for a reference, or None where it keeps
    the reference's version where it is. `bound_redundancy` gives the reason a bound decides nothing for a reference,
    or None where it narrows the versions the package manager updates the reference to. `cooldown_redundancy` gives the
    reason a marker's cooldown holds nothing back. `unchecked_scopes` names the warning scopes that Update-time
    leaves unchecked for a reference, each with the reason.
    """

    hold_back_redundancy: Callable[[ReferenceT], Reason | None]
    bound_redundancy: Callable[[ReferenceT], Reason | None]
    cooldown_redundancy: Reason
    unchecked_scopes: Callable[[ReferenceT], Mapping[Scope, Reason]]

    @staticmethod
    def every_warning_scope(reason: Reason) -> dict[Scope, Reason]:
        """Return every warning scope, each left unchecked for the reason."""
        return {directive.scope: reason for directive in WARNING_DIRECTIVES}

    def policy_for_files_left_as_they_are(
        self, hold_back_reason: Reason, bound_reason: Reason
    ) -> ManagerPolicy[ReferenceT]:
        """Return the policy for a file whose dependencies Update-time leaves as they are."""
        return replace(
            self,
            hold_back_redundancy=lambda _reference: hold_back_reason,
            bound_redundancy=lambda _reference: bound_reason,
            cooldown_redundancy=hold_back_reason,
        )

    def steers_the_update(self, reference: ReferenceT) -> bool:
        """Return whether the reference's marker steers the update, which it does where an `ignore[update]` keeps it."""
        return self.hold_back_redundancy(reference) is None

    def keeps_the_version(self, reference: ReferenceT) -> bool:
        """Return whether the reference's marker carries an `ignore[update]` that keeps its version where it is."""
        return reference.marker.ignores(Scope.UPDATE) and self.steers_the_update(reference)


def report_markers[ReferenceT: SteeredReference](
    references: Iterable[ReferenceT], log: Logger, policy: ManagerPolicy[ReferenceT]
) -> None:
    """Report each reference's marker."""
    for reference in references:
        log.report_marker(
            reference.dependency,
            reference.marker,
            reference.location,
            steers_the_update=policy.steers_the_update(reference),
        )

"""Which of a marker's directives hold nothing back, and why."""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from update_time.domain.archival import reports_archival
from update_time.domain.publication import reports_publication_dates
from update_time.domain.vulnerability import reports_vulnerabilities
from update_time.domain.yank import reports_yanks
from update_time.markers.marker import Scope

if TYPE_CHECKING:
    from update_time.primitives.capability import HasCapability


class Reason(StrEnum):
    """Why a directive is redundant, valued with the words the warning reports it in.

    `Redundancy` names the two further ways a bound can be redundant, once there is a version to judge it against.
    """

    NO_VULNERABILITY_REPORTS = "this dependency's source reports no vulnerabilities"
    NO_COOLDOWN_DATES = "this dependency's source reports no publication date to measure a cooldown against"
    NO_STALENESS_DATES = "this dependency's source reports no publication date to measure staleness against"
    NO_YANK_CONCEPT = "this dependency's source has no yank concept"
    NO_ARCHIVAL_SIGNAL = "this dependency's source publishes no archival signal"
    NO_VERSION_TO_UPDATE = "this requirement pins no version to update"
    BOUND_DECIDES_NOTHING = "the package manager resolves this dependency's version, so a bound decides nothing"
    MANAGER_RESOLVES_THE_VERSION = "the package manager resolves this dependency's version"
    COOLDOWN_PER_RUN = "the package manager applies the cooldown per run rather than per dependency"
    NO_PYPI_RELEASE = "PyPI serves no release for this dependency"
    NO_VERSION_TO_CHECK_FOR_A_YANK = "this requirement pins no version to check for a yank"
    NO_VERSION_TO_CHECK_FOR_A_VULNERABILITY = "this requirement pins no version to check for a vulnerability"
    PIN_NOT_FLOATING = "this reference's pin does not float"
    UPDATE_HELD_BACK = "this reference's update is held back, so its tag is never pinned"


@dataclass(frozen=True)
class _Directive:
    """One of the directives a marker can carry that the reference's source may be unable to apply.

    `scope` is what the directive steers, which the marker is asked for both the directive it carries and whether it
    carries one at all, in whichever of the scope's forms (see `Marker.directive_for`).
    `is_applied_by` tells whether a check can apply the directive. It reads that off the function the check asks,
    which answers per subject (see `capability`).
    `reason` is why the source cannot apply it, and `without_a_version` why it holds nothing back for a reference
    that pins no version, which is None for a directive such a reference still steers something with.
    """

    scope: Scope
    is_applied_by: HasCapability
    reason: Reason
    without_a_version: Reason | None = None


# Every directive a source may be unable to apply, in the order their warnings are reported. `update` is the one
# scope with no row: every source resolves a version, so there is no capability a source could lack for it.
DIRECTIVES = (
    _Directive(
        Scope.YANKED,
        reports_yanks,
        Reason.NO_YANK_CONCEPT,
        Reason.NO_VERSION_TO_CHECK_FOR_A_YANK,
    ),
    _Directive(
        Scope.VULNERABLE,
        reports_vulnerabilities,
        Reason.NO_VULNERABILITY_REPORTS,
        Reason.NO_VERSION_TO_CHECK_FOR_A_VULNERABILITY,
    ),
    _Directive(
        Scope.ARCHIVED,
        reports_archival,
        Reason.NO_ARCHIVAL_SIGNAL,
    ),
    _Directive(
        Scope.COOLDOWN,
        reports_publication_dates,
        Reason.NO_COOLDOWN_DATES,
        Reason.NO_VERSION_TO_UPDATE,  # a cooldown steers an update, and no update is resolved for such a reference
    ),
    _Directive(
        Scope.STALE,
        reports_publication_dates,
        Reason.NO_STALENESS_DATES,
    ),
)

# The directives whose scope steers a warning rather than the update, which is every one but the cooldown.
WARNING_DIRECTIVES = tuple(directive for directive in DIRECTIVES if directive.scope is not Scope.COOLDOWN)

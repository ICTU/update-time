"""The directives a marker can carry that the source resolving a reference may be unable to apply.

Each is declared once here, with the capability its application needs and the reason it holds nothing back without
it, so a check added to the marker language is a row rather than a branch of its own. The caller does the reporting,
which keeps this layer free of I/O.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from update_time.domain.marker import Scope
from update_time.domain.publication import reports_publication_dates
from update_time.domain.vulnerability import reports_vulnerabilities
from update_time.domain.yank import reports_yanks

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.dependency import DependencyName
    from update_time.domain.marker import Marker


class Reason(StrEnum):
    """Why a directive holds nothing back, valued with the words the warning reports it in."""

    NO_VULNERABILITY_REPORTS = "this dependency's source reports no vulnerabilities"
    NO_COOLDOWN_DATES = "this dependency's source reports no publication date to measure a cooldown against"
    NO_STALENESS_DATES = "this dependency's source reports no publication date to measure staleness against"
    NO_YANK_CONCEPT = "this dependency's source has no yank concept"
    NO_VERSION_TO_UPDATE = "this requirement pins no version to update"
    NO_VERSION_TO_CHECK_FOR_A_YANK = "this requirement pins no version to check for a yank"
    NO_VERSION_TO_CHECK_FOR_A_VULNERABILITY = "this requirement pins no version to check for a vulnerability"


@dataclass(frozen=True)
class _Directive:
    """One of the directives a marker can carry that the new-version getter may be unable to apply.

    `is_part_of` reads off the marker whether it carries this directive, in whichever of the directive's forms.
    `is_applied_by` reads off the getter whether it can apply the directive to this dependency (see `capability`).
    `spelling` names the directive as the warning about it should, which is the item the user wrote where the
    language spells it more than one way. `reason` is why the getter cannot apply it, and `without_a_version` why it
    holds nothing back for a reference that pins no version, which is None for a directive such a reference still
    steers something with.
    """

    is_part_of: Callable[[Marker], bool]
    is_applied_by: Callable[[NewVersionGetter, DependencyName], bool]
    spelling: Callable[[Marker], str]
    reason: Reason
    without_a_version: Reason | None = None


# Each directive a getter may be unable to apply, and — where it needs the version a reference pins — why it holds
# nothing back without one. The staleness directive has no such reason: staleness is the one check a reference that
# pins no version still gets. Which getter has which capability is registered by the getters themselves, so no row
# repeats it.
DIRECTIVES = (
    _Directive(
        lambda marker: marker.ignores(Scope.YANKED),
        reports_yanks,
        lambda marker: marker.scope_directive(Scope.YANKED),
        Reason.NO_YANK_CONCEPT,
        Reason.NO_VERSION_TO_CHECK_FOR_A_YANK,
    ),
    _Directive(
        lambda marker: marker.suppresses_vulnerabilities,
        reports_vulnerabilities,
        lambda marker: marker.vulnerable_directives,
        Reason.NO_VULNERABILITY_REPORTS,
        Reason.NO_VERSION_TO_CHECK_FOR_A_VULNERABILITY,
    ),
    _Directive(
        lambda marker: marker.sets_cooldown,
        reports_publication_dates,
        lambda marker: marker.cooldown_directive,
        Reason.NO_COOLDOWN_DATES,
        Reason.NO_VERSION_TO_UPDATE,  # a cooldown steers an update, and no update is resolved for such a reference
    ),
    _Directive(
        lambda marker: marker.decides_staleness,
        reports_publication_dates,
        lambda marker: marker.stale_directive,
        Reason.NO_STALENESS_DATES,
    ),
)

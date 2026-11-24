"""Which directives on a pom's dependencies and plugins decide nothing, and the report of their markers.

Update-time applies Maven's cooldown per artefact, a bound can hold back every version, and the scanned poms and Maven
Central decide which scopes are redundant. So these are functions over a pom's declarations, not a `ManagerPolicy`.
"""

from typing import TYPE_CHECKING, Final

from update_time.domain.directive import Reason
from update_time.domain.marker import Scope
from update_time.io.log import get_logger
from update_time.markers.report import report_marker, warn_if_the_bound_is_redundant, warn_if_the_opt_in_is_redundant
from update_time.sources import maven_central

from .versions_plugin import range_held_back, reason_the_marker_steers_nothing

if TYPE_CHECKING:
    from update_time.io.log import Logger
    from update_time.manifests.pom_xml import Declaration

_LOG: Final[Logger] = get_logger("pom.xml")


def report_markers(declared: list[Declaration], *, pom_names_rules: bool) -> None:
    """Report the marker of each dependency and plugin, named as Maven resolves it, and the range a bound holds back.

    Where Maven does not update the version, or follows rules the pom names, a marker holds nothing back. A bound that
    the rule set cannot express holds every version back, as an `ignore[update]` does.
    """
    for declaration in declared:
        reason = reason_the_marker_steers_nothing(declaration, pom_names_rules=pom_names_rules)
        held_back = range_held_back(declaration) if reason is None else ""
        marker = declaration.marker
        report_marker(declaration, _LOG, steers_the_update=reason is None)
        if held_back is None and not marker.ignores(Scope.UPDATE):
            _LOG.ignored(declaration)
        elif held_back:
            _LOG.holding_back_range(declaration, held_back, marker.version_bound_directive)
        _report_redundant_directives(declaration, reason)


def _report_redundant_directives(declaration: Declaration, reason: Reason | None) -> None:
    """Report as redundant each directive that decides nothing for the declaration, for the reason given, if any."""
    marker = declaration.marker
    if reason:
        for steering in filter(None, (marker.bound_directive, marker.cooldown_directive)):
            _LOG.redundant_directive(declaration, steering, reason)
    else:
        warn_if_the_bound_is_redundant(declaration, _LOG)
    if yanked := marker.directive_for(Scope.YANKED):
        _LOG.redundant_directive(declaration, yanked, Reason.NO_YANK_CONCEPT)
    warn_if_the_opt_in_is_redundant(declaration, Scope.FLOATING_PIN, Reason.PIN_NOT_FLOATING, _LOG)
    warn_if_the_opt_in_is_redundant(declaration, Scope.HASH_DRIFT, Reason.NO_HASH_PIN, _LOG)


def report_redundant_scopes(
    declared: list[Declaration],
    left_to_another: list[Declaration],
    *,
    pom_names_rules: bool,
) -> None:
    """Report the scopes that decide nothing: on a declaration leaving its version to another, and on an artefact."""
    _report_scopes_checked_at_the_managing_declaration(left_to_another)
    _report_scopes_on_artefacts_maven_central_does_not_serve(declared, pom_names_rules=pom_names_rules)
    _report_archived_scopes_without_a_github_repository(declared)


def _report_scopes_checked_at_the_managing_declaration(left_to_another: list[Declaration]) -> None:
    """Report the `stale`, `archived`, and `vulnerable` scopes of each declaration as redundant."""
    for declaration in left_to_another:
        for scope in (Scope.STALE, Scope.ARCHIVED, Scope.VULNERABLE):
            if directive := declaration.marker.directive_for(scope):
                _LOG.redundant_directive(declaration, directive, Reason.CHECKED_AT_THE_MANAGING_DECLARATION)


def _report_scopes_on_artefacts_maven_central_does_not_serve(
    declared: list[Declaration], *, pom_names_rules: bool
) -> None:
    """Report as redundant each `cooldown`, `stale`, and `archived` scope on an artefact Maven Central does not serve.

    A `cooldown` counts only where the marker steers what Maven adopts.
    """
    for declaration in declared:
        steers = reason_the_marker_steers_nothing(declaration, pom_names_rules=pom_names_rules) is None
        cooldown = declaration.marker.cooldown_directive if steers else ""
        stale = declaration.marker.directive_for(Scope.STALE)
        directives = list(filter(None, (cooldown, stale, declaration.marker.directive_for(Scope.ARCHIVED))))
        if directives and not maven_central.serves(declaration.dependency):
            for directive in directives:
                _LOG.redundant_directive(declaration, directive, Reason.NOT_ON_MAVEN_CENTRAL)


def _report_archived_scopes_without_a_github_repository(declared: list[Declaration]) -> None:
    """Report as redundant each `archived` scope on an artefact whose poms do not name a GitHub repository."""
    for declaration in declared:
        archived = declaration.marker.directive_for(Scope.ARCHIVED)
        if archived and maven_central.lacks_a_github_repository(declaration.dependency):
            _LOG.redundant_directive(declaration, archived, Reason.NO_GITHUB_REPOSITORY)

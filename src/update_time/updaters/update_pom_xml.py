"""Find pom.xml files and update the dependencies and plugins they declare with Maven."""

from typing import TYPE_CHECKING

from update_time.domain.archival import archival_is_checked
from update_time.domain.file_type import POM_XML
from update_time.formats import xml
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests import pom_xml as pom_xml_manifest
from update_time.markers.directive import Reason
from update_time.markers.marker import Scope
from update_time.package_managers import maven
from update_time.references.delegated import project_resolver, warn_about_projects
from update_time.references.resolve import warn_if_the_opt_in_is_redundant
from update_time.references.vulnerability import warn_about_vulnerable_dependencies
from update_time.sources import maven_central
from update_time.sources.osv import Ecosystem

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


_LOG = get_logger("pom.xml")


def update_pom_xmls() -> None:
    """Update each pom.xml the scan finds, running Maven over it as a module of its reactor."""
    pom_xmls = list(glob_for(POM_XML))
    scanned_poms = {name: pom_xml for pom_xml in pom_xmls if (name := pom_xml_manifest.pom_name(pom_xml))}
    reactors = maven.reactors(pom_xmls)
    for pom_xml in pom_xmls:
        _LOG.path(pom_xml)
        _update_pom_xml(pom_xml, scanned_poms, reactors[pom_xml])


def _update_pom_xml(pom_xml: Path, scanned_poms: Mapping[str, Path], reactor: maven.Reactor) -> None:
    """Update the dependencies and plugins the pom declares, and report the ones Maven moved.

    Which those are is read off the pom before and after the run, rather than from Maven's own report, which names
    no line to report a change at.
    """
    before = pom_xml_manifest.declarations(pom_xml)
    if before is None:
        _LOG.invalid_file(pom_xml, xml.FORMAT)  # The pom cannot be read, so Maven is not run on it either.
        return
    effective_pom = reactor.update(pom_xml)
    after = pom_xml_manifest.declarations(pom_xml)
    resolved = pom_xml_manifest.declarations(pom_xml, effective_pom)
    if after is None or resolved is None:
        _LOG.invalid_xml_after_update(pom_xml)  # Maven rewrote the pom into something that does not parse.
        return
    if len(before) != len(after):
        # The two readings pair up declaration by declaration, which a reading of another length cannot do.
        _LOG.declarations_changed(pom_xml, len(before), len(after))
        return
    distinct = pom_xml_manifest.one_per_artefact_and_line(resolved)
    _report_markers(distinct)
    maven.report_new_versions(before, after, resolved)
    resolvable = pom_xml_manifest.with_resolved_coordinates(distinct)
    declared = pom_xml_manifest.without_versions_left_to(resolvable, scanned_poms)
    left_to_another = [declaration for declaration in resolvable if declaration not in declared]
    _report_scopes_checked_at_the_managing_declaration(left_to_another)
    _report_scopes_on_artefacts_maven_central_does_not_serve(declared)
    _report_archived_scopes_without_a_github_repository(declared)
    _check_projects(declared)
    _warn_about_vulnerabilities(declared)


def _report_markers(declared: list[pom_xml_manifest.Declaration]) -> None:
    """Report the marker of each dependency and plugin, named as Maven resolves it, and the range a bound holds back."""
    for declaration in declared:
        held_back = maven.range_held_back(declaration) if declaration.maven_updates_the_version else ""
        _LOG.report_marker(
            declaration.dependency,
            declaration.marker,
            declaration.location,
            holds_the_update_back=held_back is None,
        )
        if held_back:
            _LOG.holding_back_range(declaration, held_back, declaration.marker.version_bound_directive)
        _report_redundant_directives(declaration)


def _report_redundant_directives(declaration: pom_xml_manifest.Declaration) -> None:
    """Report as redundant each directive that decides nothing for the declaration."""
    marker = declaration.marker
    if reason := maven.reason_maven_leaves_the_version(declaration):
        for steering in filter(None, (marker.bound_directive, marker.cooldown_directive)):
            _LOG.redundant_directive(declaration, steering, reason)
    else:
        _LOG.warn_if_redundant_bound(declaration, marker)
    if yanked := marker.directive_for(Scope.YANKED):
        _LOG.redundant_directive(declaration, yanked, Reason.NO_YANK_CONCEPT)
    warn_if_the_opt_in_is_redundant(
        declaration, marker, _LOG, Scope.FLOATING_PIN, Reason.PIN_NOT_FLOATING, update_held_back=False
    )
    warn_if_the_opt_in_is_redundant(
        declaration, marker, _LOG, Scope.HASH_DRIFT, Reason.NO_HASH_PIN, update_held_back=False
    )


def _report_scopes_checked_at_the_managing_declaration(left_to_another: list[pom_xml_manifest.Declaration]) -> None:
    """Report the `stale`, `archived`, and `vulnerable` scopes of each declaration as redundant."""
    for declaration in left_to_another:
        for scope in (Scope.STALE, Scope.ARCHIVED, Scope.VULNERABLE):
            if directive := declaration.marker.directive_for(scope):
                _LOG.redundant_directive(declaration, directive, Reason.CHECKED_AT_THE_MANAGING_DECLARATION)


def _report_scopes_on_artefacts_maven_central_does_not_serve(declared: list[pom_xml_manifest.Declaration]) -> None:
    """Report as redundant each `cooldown`, `stale`, and `archived` scope on an artefact Maven Central does not serve.

    A `cooldown` counts only where Maven updates the version, and an `archived` scope only in a run that checks
    archival, since a run switching that check off makes it redundant for its own reason.
    """
    for declaration in declared:
        cooldown = declaration.marker.cooldown_directive if declaration.maven_updates_the_version else ""
        archived = declaration.marker.directive_for(Scope.ARCHIVED) if archival_is_checked() else ""
        check_directives = (declaration.marker.directive_for(Scope.STALE), archived)
        directives = list(filter(None, (cooldown, *check_directives)))
        if directives and not maven_central.serves(declaration.dependency):
            for directive in directives:
                _LOG.redundant_directive(declaration, directive, Reason.NOT_ON_MAVEN_CENTRAL)


def _report_archived_scopes_without_a_github_repository(declared: list[pom_xml_manifest.Declaration]) -> None:
    """Report as redundant each `archived` scope on an artefact whose poms do not name a GitHub repository."""
    if not archival_is_checked():
        return
    for declaration in declared:
        archived = declaration.marker.directive_for(Scope.ARCHIVED)
        if archived and maven_central.lacks_a_github_repository(declaration.dependency):
            _LOG.redundant_directive(declaration, archived, Reason.NO_GITHUB_REPOSITORY)


def _check_projects(declared: list[pom_xml_manifest.Declaration]) -> None:
    """Warn about each dependency and plugin whose newest release is old, or whose source repository is archived."""
    warn_about_projects([declared], project_resolver(maven_central.project), _LOG)


def _warn_about_vulnerabilities(declared: list[pom_xml_manifest.Declaration]) -> None:
    """Warn about each dependency and plugin the run leaves on a version an advisory names.

    OSV needs a version to match an advisory to, so a `vulnerable` scope on a version that Maven's effective pom
    lists unresolved is redundant.
    """
    resolved = []
    for declaration in declared:
        if pom_xml_manifest.fully_resolved(declaration.pinned):
            resolved.append(declaration)
        elif declaration.resolved_by_effective_pom and (
            vulnerable := declaration.marker.directive_for(Scope.VULNERABLE)
        ):
            _LOG.redundant_directive(declaration, vulnerable, Reason.NO_RESOLVED_VERSION_TO_CHECK_FOR_A_VULNERABILITY)
    warn_about_vulnerable_dependencies([resolved], Ecosystem.MAVEN, _LOG)


def main() -> None:  # pragma: no cover
    """Update the dependencies and plugins in the repository's pom.xml files."""
    update_pom_xmls()


if __name__ == "__main__":  # pragma: no cover
    main()

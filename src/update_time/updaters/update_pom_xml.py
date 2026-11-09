"""Find pom.xml files and update the dependencies and plugins they declare with Maven."""

from typing import TYPE_CHECKING

from update_time.domain.file_type import POM_XML
from update_time.formats import xml
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests import pom_xml as pom_xml_manifest
from update_time.markers.directive import Reason
from update_time.markers.marker import Scope
from update_time.package_managers.maven import marker_policy, versions_plugin
from update_time.package_managers.maven.reactor import reactors
from update_time.references.delegated import project_resolver, warn_about_projects
from update_time.references.vulnerability import warn_about_vulnerable_dependencies
from update_time.sources import maven_central
from update_time.sources.osv import Ecosystem

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from update_time.package_managers.maven.reactor import Reactor


_LOG = get_logger("pom.xml")


def update_pom_xmls() -> None:
    """Update each pom.xml the scan finds, running Maven over it as a module of its reactor."""
    pom_xmls = list(glob_for(POM_XML))
    scanned_poms = {name: pom_xml for pom_xml in pom_xmls if (name := pom_xml_manifest.pom_name(pom_xml))}
    reactor_by_pom = reactors(pom_xmls)
    for pom_xml in pom_xmls:
        _LOG.path(pom_xml)
        _update_pom_xml(pom_xml, scanned_poms, reactor_by_pom[pom_xml])


def _update_pom_xml(pom_xml: Path, scanned_poms: Mapping[str, Path], reactor: Reactor) -> None:
    """Update the dependencies and plugins the pom declares, and report the ones Maven moved.

    Which those are is read off the pom before and after the run, rather than from Maven's own report, which names
    no line to report a change at.
    """
    before = pom_xml_manifest.declarations(pom_xml)
    if before is None:
        _LOG.invalid_file(pom_xml, xml.FORMAT)  # The pom cannot be read, so Maven is not run on it either.
        return
    effective_pom = reactor.update(pom_xml)
    versions_plugin.report_options_the_pom_overrides(pom_xml, effective_pom)
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
    pom_names_rules = versions_plugin.names_rules_of_its_own(effective_pom)
    marker_policy.report_markers(distinct, pom_names_rules=pom_names_rules)
    versions_plugin.report_new_versions(before, after, resolved)
    resolvable = pom_xml_manifest.with_resolved_coordinates(distinct)
    declared = pom_xml_manifest.without_versions_left_to(resolvable, scanned_poms)
    left_to_another = [declaration for declaration in resolvable if declaration not in declared]
    marker_policy.report_redundant_scopes(declared, left_to_another, pom_names_rules=pom_names_rules)
    _check_projects(declared)
    _warn_about_vulnerabilities(declared)


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

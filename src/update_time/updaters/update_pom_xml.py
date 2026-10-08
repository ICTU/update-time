"""Find pom.xml files and update the dependencies and plugins they declare with Maven."""

from typing import TYPE_CHECKING

from update_time.domain.dependency import NO_CHANGES, DependencyVersion
from update_time.domain.file_type import POM_XML
from update_time.domain.reference import Reference
from update_time.formats import xml
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger, report_marker
from update_time.manifests import pom_xml as pom_xml_format
from update_time.package_managers import maven
from update_time.references.delegated import project_resolver, warn_about_projects
from update_time.references.vulnerability import warn_about_vulnerable_dependencies
from update_time.sources import maven_central
from update_time.sources.osv import Ecosystem

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from update_time.domain.dependency import DependencyName
    from update_time.manifests.pom_xml import Declaration
    from update_time.primitives.location import Location

_LOG = get_logger("pom.xml")


def update_pom_xmls() -> None:
    """Update each pom.xml the scan finds, running Maven over it."""
    pom_xmls = list(glob_for(POM_XML))
    scanned_poms = {name: pom_xml for pom_xml in pom_xmls if (name := pom_xml_format.pom_name(pom_xml))}
    for pom_xml in pom_xmls:
        _LOG.path(pom_xml)
        _update_pom_xml(pom_xml, scanned_poms)


def _update_pom_xml(pom_xml: Path, scanned_poms: Mapping[str, Path]) -> None:
    """Update the dependencies and plugins the pom declares, and report the ones Maven moved.

    Which those are is read off the pom before and after the run, rather than from Maven's own report, which names
    no line to report a change at.
    """
    before = pom_xml_format.declarations(pom_xml)
    if before is None:
        _LOG.invalid_file(pom_xml, xml.FORMAT)  # The pom cannot be read, so Maven is not run on it either.
        return
    effective_pom = maven.update_pom_xml(pom_xml)
    after = pom_xml_format.declarations(pom_xml)
    resolved = pom_xml_format.declarations(pom_xml, effective_pom)
    if after is None or resolved is None:
        _LOG.invalid_xml_after_update(pom_xml)  # Maven rewrote the pom into something that does not parse.
        return
    if len(before) != len(after):
        # The two readings pair up declaration by declaration, which a reading of another length cannot do.
        _LOG.declarations_changed(pom_xml, len(before), len(after))
        return
    _report_markers(resolved)
    _report_new_versions(before, after, resolved)
    resolvable = pom_xml_format.with_resolved_coordinates(resolved)
    declared = pom_xml_format.without_versions_left_to(resolvable, scanned_poms)
    _check_projects(declared)
    _warn_about_vulnerabilities(declared)


def _report_markers(declared: list[Declaration]) -> None:
    """Report the marker of each dependency and plugin, named as Maven resolves it.

    A marker steering the update holds the update back where the declaring pom gives the version, because the rule
    set holds back every version of the artefact. Two declarations of one artefact naming one property share that
    property's line, so the marker on that line is reported once.
    """
    reported: set[tuple[DependencyName, Location]] = set()
    for declaration in declared:
        if (declaration.dependency, declaration.location) in reported:
            continue
        reported.add((declaration.dependency, declaration.location))
        report_marker(
            _LOG,
            declaration.dependency,
            declaration.marker,
            declaration.location,
            holds_the_update_back=declaration.versioned_by_its_pom and declaration.marker.steers_the_update,
        )
        _LOG.report_inverted_items(declaration, declaration.marker)


def _check_projects(declared: list[Declaration]) -> None:
    """Warn about each dependency and plugin whose newest release is old, or whose source repository is archived."""
    warn_about_projects([declared], project_resolver(maven_central.project), _LOG)


def _warn_about_vulnerabilities(declared: list[Declaration]) -> None:
    """Warn about each dependency and plugin the run leaves on a version an advisory names."""
    resolved = [declaration for declaration in declared if pom_xml_format.fully_resolved(declaration.pinned)]
    warn_about_vulnerable_dependencies([resolved], Ecosystem.MAVEN, _LOG)


def _report_new_versions(before: list[Declaration], after: list[Declaration], resolved: list[Declaration]) -> None:
    """Report each dependency and plugin whose version differs between the two readings, with its changes.

    The name is the one the effective pom gives the declaration, where it resolves one.
    """
    for old, new, named in zip(before, after, resolved, strict=True):
        if old.current_version == new.current_version:
            continue
        updated = Reference(named.dependency, old.current_version, new.location)
        is_resolved = pom_xml_format.fully_resolved(named.pinned)
        changes = maven_central.get_changes(named.dependency, new.current_version) if is_resolved else NO_CHANGES
        _LOG.new_version(updated, DependencyVersion(new.current_version, changes))


def main() -> None:  # pragma: no cover
    """Update the dependencies and plugins in the repository's pom.xml files."""
    update_pom_xmls()


if __name__ == "__main__":  # pragma: no cover
    main()

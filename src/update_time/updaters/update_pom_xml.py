"""Find pom.xml files and update the dependencies they declare with Maven."""

from typing import TYPE_CHECKING

from update_time.domain.dependency import DependencyVersion
from update_time.domain.file_type import POM_XML
from update_time.domain.reference import Reference
from update_time.formats import xml
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests import pom_xml as pom_xml_format
from update_time.markers.reference import SteeredReference
from update_time.package_managers import maven
from update_time.references.delegated import project_resolver, warn_about_projects
from update_time.references.vulnerability import warn_about_vulnerable_dependencies
from update_time.sources import maven_central
from update_time.sources.osv import Ecosystem

if TYPE_CHECKING:
    from pathlib import Path

_LOG = get_logger("pom.xml")


def update_pom_xmls() -> None:
    """Update each pom.xml the scan finds, with one Maven run per pom."""
    for pom_xml in glob_for(POM_XML):
        _LOG.path(pom_xml)
        _update_pom_xml(pom_xml)


def _update_pom_xml(pom_xml: Path) -> None:
    """Update the dependencies the pom declares, and report the ones Maven moved.

    Which those are is read off the pom before and after the run, rather than from Maven's own report, which names
    no line to report a change at.
    """
    before = pom_xml_format.dependencies(pom_xml)
    if before is None:
        _LOG.invalid_file(pom_xml, xml.FORMAT)  # The pom cannot be read, so Maven is not run on it either.
        return
    maven.update_pom_xml(pom_xml)
    after = pom_xml_format.dependencies(pom_xml)
    if after is None:
        _LOG.invalid_xml_after_update(pom_xml)  # Maven rewrote the pom into something that does not parse.
        return
    if len(before) != len(after):
        # The two readings pair up declaration by declaration, which a reading of another length cannot do.
        _LOG.declarations_changed(pom_xml, len(before), len(after))
        return
    _report_new_versions(before, after)
    _check_projects(pom_xml_format.artefact_references(pom_xml))
    _warn_about_vulnerabilities(after)


def _check_projects(declared: list[Reference]) -> None:
    """Warn about each dependency and plugin whose newest release is old, or whose source repository is archived."""
    steered = [SteeredReference.from_reference(declaration) for declaration in declared]
    warn_about_projects([steered], project_resolver(maven_central.project), _LOG)


def _warn_about_vulnerabilities(declared: list[Reference]) -> None:
    """Warn about each dependency the run leaves on a version an advisory names."""
    steered = [
        SteeredReference.from_reference(declaration)
        for declaration in declared
        if pom_xml_format.fully_resolved(declaration)
    ]
    warn_about_vulnerable_dependencies([steered], Ecosystem.MAVEN, _LOG)


def _report_new_versions(before: list[Reference], after: list[Reference]) -> None:
    """Report each dependency whose version differs between the two readings."""
    for old, new in zip(before, after, strict=True):
        if old.current_version == new.current_version:
            continue
        updated = Reference(new.dependency, old.current_version, new.location)
        _LOG.new_version(updated, DependencyVersion(new.current_version))


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's pom.xml files."""
    update_pom_xmls()


if __name__ == "__main__":  # pragma: no cover
    main()

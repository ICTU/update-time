"""Find pom.xml files and update the dependencies and plugins they declare with Maven."""

from typing import TYPE_CHECKING, Final

from update_time.domain.directive import Reason
from update_time.domain.file_type import POM_XML
from update_time.domain.marker import Scope
from update_time.formats.xml import FORMAT
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.pom_xml import (
    Declaration,
    declarations,
    fully_resolved,
    pom_name,
    with_resolved_coordinates,
    without_versions_left_to,
)
from update_time.package_managers.maven.marker_policy import report_markers, report_redundant_scopes
from update_time.package_managers.maven.reactor import reactors
from update_time.package_managers.maven.versions_plugin import (
    names_rules_of_its_own,
    report_new_versions,
    report_options_the_pom_overrides,
)
from update_time.primitives.iterables import unique
from update_time.references.delegated import warn_about_pinned_versions, warn_about_projects
from update_time.sources import maven_central

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from update_time.io.log import Logger
    from update_time.package_managers.maven.reactor import Reactor


_LOG: Final[Logger] = get_logger("pom.xml")


def _update_pom_xmls() -> None:
    """Update each pom.xml the scan finds, running Maven over it as a module of its reactor."""
    pom_xmls = list(glob_for(POM_XML))
    scanned_poms = {name: pom_xml for pom_xml in pom_xmls if (name := pom_name(pom_xml))}
    reactor_by_pom = reactors(pom_xmls)
    for pom_xml in pom_xmls:
        _LOG.path(pom_xml)
        _update_pom_xml(pom_xml, scanned_poms, reactor_by_pom[pom_xml])


def _update_pom_xml(pom_xml: Path, scanned_poms: Mapping[str, Path], reactor: Reactor) -> None:
    """Update the dependencies and plugins the pom declares, and report the ones Maven moved.

    Which those are is read off the pom before and after the run, rather than from Maven's own report, which names
    no line to report a change at.
    """
    before = declarations(pom_xml)
    if before is None:
        _LOG.invalid_file(pom_xml, FORMAT)  # The pom cannot be read, so Maven is not run on it either.
        return
    effective_pom = reactor.update(pom_xml)
    report_options_the_pom_overrides(pom_xml, effective_pom)
    after = declarations(pom_xml)
    resolved = declarations(pom_xml, effective_pom)
    if after is None or resolved is None:
        _LOG.invalid_xml_after_update(pom_xml)  # Maven rewrote the pom into something that does not parse.
        return
    if len(before) != len(after):
        # The two readings pair up declaration by declaration, which a reading of another length cannot do.
        _LOG.declarations_changed(pom_xml, len(before), len(after))
        return
    # Two declarations of one artefact naming one property share that property's line, which makes them equal.
    distinct = unique(resolved)
    pom_names_rules = names_rules_of_its_own(effective_pom)
    report_markers(distinct, pom_names_rules=pom_names_rules)
    report_new_versions(before, after, resolved)
    resolvable = with_resolved_coordinates(distinct)
    declared = without_versions_left_to(resolvable, scanned_poms)
    left_to_another = [declaration for declaration in resolvable if declaration not in declared]
    report_redundant_scopes(declared, left_to_another, pom_names_rules=pom_names_rules)
    warn_about_projects([declared], maven_central.project, _LOG)
    _warn_about_vulnerabilities(declared)


def _warn_about_vulnerabilities(declared: list[Declaration]) -> None:
    """Warn about each dependency and plugin the run leaves on a version an advisory names.

    OSV needs a version to match an advisory to, so a `vulnerable` scope on a version that Maven's effective pom
    lists unresolved is redundant.
    """
    resolved = []
    for declaration in declared:
        if fully_resolved(declaration.pinned):
            resolved.append(declaration)
        elif declaration.resolved_by_effective_pom and (
            vulnerable := declaration.marker.directive_for(Scope.VULNERABLE)
        ):
            _LOG.redundant_directive(declaration, vulnerable, Reason.NO_RESOLVED_VERSION_TO_CHECK_FOR_A_VULNERABILITY)
    warn_about_pinned_versions([resolved], _LOG, maven_central.SOURCE)


if __name__ == "__main__":
    _update_pom_xmls()

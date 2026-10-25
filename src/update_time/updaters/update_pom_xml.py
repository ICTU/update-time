"""Find pom.xml files and update the dependencies and plugins they declare with Maven."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from update_time.domain.archival import archival_is_checked
from update_time.domain.dependency import DependencyVersion
from update_time.domain.file_type import POM_XML
from update_time.domain.reference import Reference
from update_time.formats import xml
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests import pom_xml as pom_xml_format
from update_time.markers.directive import Reason
from update_time.markers.marker import Scope
from update_time.package_managers import maven
from update_time.primitives.iterables import unique
from update_time.references.delegated import project_resolver, warn_about_projects
from update_time.references.resolve import warn_if_the_opt_in_is_redundant
from update_time.references.vulnerability import warn_about_vulnerable_dependencies
from update_time.sources import maven_central
from update_time.sources.osv import Ecosystem

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from update_time.manifests.pom_xml import Declaration

_LOG = get_logger("pom.xml")


@dataclass(frozen=True)
class _Update:
    """A declaration Maven updated, as the pom held it before and after the update and as the effective pom lists it."""

    old: Declaration
    new: Declaration
    effective: Declaration

    @property
    def reference(self) -> Reference:
        """Return the reference at the version it had, named as the effective pom names it."""
        return Reference(self.effective.dependency, self.old.current_version, self.new.location)

    @property
    def is_resolvable(self) -> bool:
        """Return whether the effective pom names the artefact whole, so a repository can be asked about it."""
        return pom_xml_format.is_resolved(self.effective.dependency)


def update_pom_xmls() -> None:
    """Update each pom.xml the scan finds, running Maven over it as a module of its reactor."""
    pom_xmls = list(glob_for(POM_XML))
    scanned_poms = {name: pom_xml for pom_xml in pom_xmls if (name := pom_xml_format.pom_name(pom_xml))}
    reactors = _reactors(pom_xmls)
    for pom_xml in pom_xmls:
        _LOG.path(pom_xml)
        _update_pom_xml(pom_xml, scanned_poms, reactors[pom_xml])


def _reactors(pom_xmls: list[Path]) -> dict[Path, maven.Reactor]:
    """Return the reactor each pom is part of, which holds its poms in the scan's order."""
    aggregators = _aggregators(pom_xmls)
    pom_xmls_by_root: dict[Path, list[Path]] = {}
    for pom_xml in pom_xmls:
        pom_xmls_by_root.setdefault(_reactor_root(pom_xml.parent, aggregators), []).append(pom_xml)
    reactors: dict[Path, maven.Reactor] = {}
    for root, reactor_pom_xmls in pom_xmls_by_root.items():
        reactors |= dict.fromkeys(reactor_pom_xmls, maven.Reactor(root, reactor_pom_xmls))
    return reactors


def _aggregators(pom_xmls: list[Path]) -> dict[Path, Path]:
    """Return the directory of the pom listing each module, keyed by the module's directory."""
    return {module: pom_xml.parent for pom_xml in pom_xmls for module in pom_xml_format.module_directories(pom_xml)}


def _reactor_root(directory: Path, aggregators: Mapping[Path, Path]) -> Path:
    """Return the directory of the topmost pom whose modules reach the directory, or the directory itself.

    The climb stops at a directory it reached before, where poms list each other as modules.
    """
    reached = {directory}
    while (aggregator := aggregators.get(directory)) is not None and aggregator not in reached:
        reached.add(aggregator)
        directory = aggregator
    return directory


def _update_pom_xml(pom_xml: Path, scanned_poms: Mapping[str, Path], reactor: maven.Reactor) -> None:
    """Update the dependencies and plugins the pom declares, and report the ones Maven moved.

    Which those are is read off the pom before and after the run, rather than from Maven's own report, which names
    no line to report a change at.
    """
    before = pom_xml_format.declarations(pom_xml)
    if before is None:
        _LOG.invalid_file(pom_xml, xml.FORMAT)  # The pom cannot be read, so Maven is not run on it either.
        return
    effective_pom = reactor.update(pom_xml)
    after = pom_xml_format.declarations(pom_xml)
    resolved = pom_xml_format.declarations(pom_xml, effective_pom)
    if after is None or resolved is None:
        _LOG.invalid_xml_after_update(pom_xml)  # Maven rewrote the pom into something that does not parse.
        return
    if len(before) != len(after):
        # The two readings pair up declaration by declaration, which a reading of another length cannot do.
        _LOG.declarations_changed(pom_xml, len(before), len(after))
        return
    distinct = pom_xml_format.one_per_artefact_and_line(resolved)
    _report_markers(distinct)
    _report_new_versions(before, after, resolved)
    resolvable = pom_xml_format.with_resolved_coordinates(distinct)
    declared = pom_xml_format.without_versions_left_to(resolvable, scanned_poms)
    left_to_another = [declaration for declaration in resolvable if declaration not in declared]
    _report_scopes_checked_at_the_managing_declaration(left_to_another)
    _report_scopes_on_artefacts_maven_central_does_not_serve(declared)
    _report_archived_scopes_without_a_github_repository(declared)
    _check_projects(declared)
    _warn_about_vulnerabilities(declared)


def _report_markers(declared: list[Declaration]) -> None:
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
        _LOG.report_inverted_items(declaration, declaration.marker)
        _report_redundant_directives(declaration)


def _report_redundant_directives(declaration: Declaration) -> None:
    """Report as redundant each directive that decides nothing for the declaration."""
    as_written = declaration.marker.as_written
    if reason := _why_maven_leaves_the_version(declaration):
        for steering in filter(None, (as_written.bound_directive, as_written.cooldown_directive)):
            _LOG.redundant_directive(declaration, steering, reason)
    else:
        _LOG.warn_if_redundant_bound(declaration, declaration.marker)
    if yanked := as_written.directive_for(Scope.YANKED):
        _LOG.redundant_directive(declaration, yanked, Reason.NO_YANK_CONCEPT)
    marker = declaration.marker
    warn_if_the_opt_in_is_redundant(
        declaration, marker, _LOG, Scope.FLOATING_PIN, Reason.PIN_NOT_FLOATING, update_held_back=False
    )
    warn_if_the_opt_in_is_redundant(
        declaration, marker, _LOG, Scope.HASH_DRIFT, Reason.NO_HASH_PIN, update_held_back=False
    )


def _why_maven_leaves_the_version(declaration: Declaration) -> Reason | None:
    """Return why Maven does not update the declaration's version, or None where it does."""
    if declaration.maven_updates_the_version:
        return None
    return Reason.PLUGIN_NOT_UPDATED if declaration.literal_plugin_version else Reason.VERSION_HELD_ELSEWHERE


def _report_scopes_checked_at_the_managing_declaration(left_to_another: list[Declaration]) -> None:
    """Report the `stale`, `archived`, and `vulnerable` scopes of each declaration as redundant."""
    for declaration in left_to_another:
        as_written = declaration.marker.as_written
        for scope in (Scope.STALE, Scope.ARCHIVED, Scope.VULNERABLE):
            if directive := as_written.directive_for(scope):
                _LOG.redundant_directive(declaration, directive, Reason.CHECKED_AT_THE_MANAGING_DECLARATION)


def _report_scopes_on_artefacts_maven_central_does_not_serve(declared: list[Declaration]) -> None:
    """Report as redundant each `cooldown`, `stale`, and `archived` scope on an artefact Maven Central does not serve.

    A `cooldown` counts only where Maven updates the version.
    """
    for declaration in declared:
        as_written = declaration.marker.as_written
        cooldown = as_written.cooldown_directive if declaration.maven_updates_the_version else ""
        check_directives = (as_written.directive_for(Scope.STALE), as_written.directive_for(Scope.ARCHIVED))
        directives = list(filter(None, (cooldown, *check_directives)))
        if directives and not maven_central.serves(declaration.dependency):
            for directive in directives:
                _LOG.redundant_directive(declaration, directive, Reason.NOT_ON_MAVEN_CENTRAL)


def _report_archived_scopes_without_a_github_repository(declared: list[Declaration]) -> None:
    """Report as redundant each `archived` scope on an artefact whose poms do not name a GitHub repository."""
    if not archival_is_checked():
        return
    for declaration in declared:
        archived = declaration.marker.as_written.directive_for(Scope.ARCHIVED)
        if archived and maven_central.lacks_a_github_repository(declaration.dependency):
            _LOG.redundant_directive(declaration, archived, Reason.NO_GITHUB_REPOSITORY)


def _check_projects(declared: list[Declaration]) -> None:
    """Warn about each dependency and plugin whose newest release is old, or whose source repository is archived."""
    warn_about_projects([declared], project_resolver(maven_central.project), _LOG)


def _warn_about_vulnerabilities(declared: list[Declaration]) -> None:
    """Warn about each dependency and plugin the run leaves on a version an advisory names.

    OSV needs a version to match an advisory to, so a `vulnerable` scope on a version that Maven's effective pom
    lists unresolved is redundant.
    """
    resolved = []
    for declaration in declared:
        if pom_xml_format.fully_resolved(declaration.pinned):
            resolved.append(declaration)
        elif declaration.listed_in_effective_pom and (
            vulnerable := declaration.marker.as_written.directive_for(Scope.VULNERABLE)
        ):
            _LOG.redundant_directive(declaration, vulnerable, Reason.NO_RESOLVED_VERSION_TO_CHECK_FOR_A_VULNERABILITY)
    warn_about_vulnerable_dependencies([resolved], Ecosystem.MAVEN, _LOG)


def _report_new_versions(before: list[Declaration], after: list[Declaration], resolved: list[Declaration]) -> None:
    """Report each dependency and plugin whose version differs between the two readings, with its changes and date.

    The name is the one the effective pom gives the declaration, where it resolves one. Two declarations of one
    artefact naming one property read the same in each reading, so they are reported once. The new version of an
    artefact Maven Central does not serve goes unreported, where a served artefact names its property too.
    """
    updates = [
        _Update(old, new, effective)
        for old, new, effective in unique(zip(before, after, resolved, strict=True))
        if old.current_version != new.current_version
    ]
    unreportable = _unreportable_updates(updates)
    for update in updates:
        reference, new = update.reference, update.new
        if update in unreportable:
            _LOG.new_version_not_reported(reference, new.version_property, new.current_version)
            continue
        _LOG.new_version(reference, _new_version(update))


def _new_version(update: _Update) -> DependencyVersion:
    """Return the version Maven moved to, with its changes and publication date where the coordinates resolve."""
    artefact, version = update.effective.dependency, update.new.current_version
    if not pom_xml_format.fully_resolved(update.effective.pinned):
        return DependencyVersion(version)
    changes = maven_central.get_changes(artefact, version)
    return DependencyVersion(version, changes, publication=maven_central.publication(artefact, version))


def _unreportable_updates(updates: list[_Update]) -> list[_Update]:
    """Return the updates of an artefact Maven Central does not serve, whose property a served artefact names too.

    Maven moves a property to a version that every artefact naming it has, so the move says nothing about an artefact
    whose versions Maven does not find.
    """
    candidates = [update for update in updates if update.new.version_property and update.is_resolvable]
    artefacts = {update.effective.dependency for update in candidates}
    off_central = {artefact for artefact in artefacts if not maven_central.serves(artefact)}
    properties_on_central = {
        update.new.version_property for update in candidates if update.effective.dependency not in off_central
    }
    return [
        update
        for update in candidates
        if update.effective.dependency in off_central and update.new.version_property in properties_on_central
    ]


def main() -> None:  # pragma: no cover
    """Update the dependencies and plugins in the repository's pom.xml files."""
    update_pom_xmls()


if __name__ == "__main__":  # pragma: no cover
    main()

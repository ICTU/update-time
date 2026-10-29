"""Find package.json files, update their dependencies with the project's Node package manager, and check them."""

from typing import TYPE_CHECKING

from update_time.domain.file_type import PACKAGE_JSON
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.package_json import PackageJson
from update_time.markers.directive import Reason
from update_time.package_managers import node
from update_time.package_managers.policy import report_markers
from update_time.references.delegated import (
    project_resolver,
    warn_about_delegated_checks,
    warn_about_redundant_directives,
)
from update_time.sources import npmjs
from update_time.sources.osv import Ecosystem

if TYPE_CHECKING:
    from update_time.markers.reference import SteeredReference

_LOG = get_logger("package.json")


def update_package_jsons() -> None:
    """Find all package.json files, update each with its (supported) package manager, and check its dependencies.

    The package manager runs only for a package.json that declares a dependency, since it writes a lockfile beside
    any package.json it runs for.
    """
    supported = []
    # npm rewrites the ranges of a workspace's members as it updates the root, so the root runs first and logs them:
    for package_json in sorted(glob_for(PACKAGE_JSON), key=lambda path: len(path.parts)):
        if (file := PackageJson.readable(package_json, _LOG.unreadable_package_json)) is None:
            continue
        if (manager := node.SUPPORTED_MANAGERS.get(name := node.manager_name(file))) is None:
            _LOG.unsupported_package_manager(package_json, name, " and ".join(node.SUPPORTED_MANAGERS))
            continue
        if file.declares_a_dependency():
            checked_package_json = manager.update_package_json(file)
        else:
            _LOG.no_dependencies(package_json, name, "install")
            checked_package_json = file
        if checked_package_json is not None:
            supported.append(checked_package_json)
    _warn_about_dependencies(supported)


def _warn_about_dependencies(files: list[PackageJson]) -> None:
    """Report the marker of each dependency, and check the dependencies of each package.json."""
    declarations = [list(file.declarations()) for file in files]
    check_targets = [report_markers(file_declarations, _LOG, node.POLICY) for file_declarations in declarations]
    warn_about_redundant_directives(declarations, _LOG, node.POLICY)
    _report_undeclared_references(files)
    registry_declarations = [[target for target in targets if target.names_a_release] for targets in check_targets]
    warn_about_delegated_checks(
        registry_declarations, _LOG, project_resolver(npmjs.project), npmjs.deprecation, Ecosystem.NPM
    )


def _report_undeclared_references(files: list[PackageJson]) -> None:
    """Report the marker of each dependency that the `update-time` field names and its section does not declare."""
    for file in files:
        for reference in file.undeclared_references():
            _report_undeclared_reference(reference)


def _report_undeclared_reference(reference: SteeredReference) -> None:
    """Report the marker of a dependency its section does not declare, and one holding directives as redundant."""
    marker = reference.marker
    _LOG.report_marker(reference.dependency, marker, reference.location, holds_the_update_back=False)
    if marker.invalid_item is None and marker.raw:
        _LOG.redundant_directive(reference, marker.raw, Reason.UNDECLARED_DEPENDENCY)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's package.json files."""
    update_package_jsons()


if __name__ == "__main__":  # pragma: no cover
    main()

"""Find package.json files, update their dependencies with the project's Node package manager, and check them.

Check the catalogs of the pnpm-workspace.yaml and .yarnrc.yml files as well.
"""

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from update_time.domain.directive import Reason
from update_time.domain.file_type import PACKAGE_JSON, PNPM_WORKSPACE_YAML, YARNRC_YML
from update_time.io.filesystem import glob_for
from update_time.io.log import FileContent, get_logger
from update_time.manifests.catalogs_yaml import CatalogsYaml
from update_time.manifests.package_json import PackageJson
from update_time.markers.report import report_marker
from update_time.package_managers.node.manager_choice import SUPPORTED_MANAGERS, manager_choice
from update_time.package_managers.node.marker_policy import (
    MARKER_POLICY,
    MORE_THAN_ONE_MANAGER_POLICY,
    CheckedPackageJson,
)
from update_time.package_managers.node.pnpm_workspace import update_pnpm_workspace
from update_time.package_managers.policy import report_markers
from update_time.primitives.text import prose_list
from update_time.references.delegated import warn_about_delegated_checks, warn_about_redundant_directives
from update_time.sources import npmjs

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.reference import Reference
    from update_time.manifests.npm_declaration import Declaration
    from update_time.package_managers.policy import ManagerPolicy

_LOG = get_logger("package.json")

# The warnings that a package.json, or a file holding catalogs, cannot be read, naming what was looked for.
_UNREADABLE_PACKAGE_JSON = partial(_LOG.unreadable_file, content=FileContent.DEPENDENCY_LINES)
_UNREADABLE_CATALOGS = partial(_LOG.unreadable_file, content=FileContent.CATALOGS)


@dataclass(frozen=True)
class _CheckedFile:
    """The declarations of a file to check, and the policy their markers are judged by."""

    declarations: list[Declaration]
    policy: ManagerPolicy[Declaration]


def _update_package_jsons() -> None:
    """Find all package.json files, update each with its package manager, and check them and the catalogs.

    The package manager runs only for a package.json that declares a dependency or roots a workspace, since it writes
    a lockfile beside any package.json it runs for. pnpm also runs for a workspace root that does not hold a
    package.json. A manager runs only where the project's files name one manager.
    """
    # The run for a workspace root updates its members, so the roots go first, and a member is read as that run left it:
    catalog_policies = {path: update_pnpm_workspace(path) for path in glob_for(PNPM_WORKSPACE_YAML)}
    supported: list[CheckedPackageJson] = []
    for package_json in sorted(glob_for(PACKAGE_JSON), key=lambda path: len(path.parts)):
        _LOG.path(package_json)
        if (file := PackageJson.readable(package_json, _UNREADABLE_PACKAGE_JSON)) is None:
            continue
        choice = manager_choice(file)
        if (manager := SUPPORTED_MANAGERS.get(name := choice.name)) is None:
            _LOG.unsupported_package_manager(package_json, name, prose_list(SUPPORTED_MANAGERS))
            continue
        if not (file.declares_a_dependency() or manager.roots_a_workspace(file)):
            _LOG.no_dependencies(package_json, name, "install")
            checked_package_json: CheckedPackageJson | None = CheckedPackageJson(file, MARKER_POLICY)
        elif choice.names_more_than_one_manager():
            _LOG.files_of_more_than_one_manager(package_json, choice.files)
            checked_package_json = CheckedPackageJson(file, MORE_THAN_ONE_MANAGER_POLICY)
        else:
            checked_package_json = manager.update_package_json(file)
        if checked_package_json is not None:
            supported.append(
                checked_package_json.judged_by(manager.checked_catalogs(checked_package_json.package_json))
            )
    catalog_policies |= _yarn_catalog_policies(supported)
    files_to_check = [_CheckedFile(list(each.package_json.declarations()), each.policy) for each in supported]
    _warn_about_dependencies(files_to_check + _checked_workspace_files(catalog_policies))
    _report_undeclared_references([each.package_json for each in supported])


def _yarn_catalog_policies(checked: list[CheckedPackageJson]) -> dict[Path, ManagerPolicy[Declaration]]:
    """Return the policy for the markers of each .yarnrc.yml: the policy of the package.json beside it, if checked."""
    policies = {each.package_json.path.parent: each.policy for each in checked}
    return {path: policies.get(path.parent, MARKER_POLICY) for path in glob_for(YARNRC_YML)}


def _checked_workspace_files(catalog_policies: dict[Path, ManagerPolicy[Declaration]]) -> list[_CheckedFile]:
    """Return each file holding catalogs to check, with its policy, warning about each that cannot be read."""
    files: list[_CheckedFile] = []
    for path, policy in catalog_policies.items():
        _LOG.path(path)
        if (workspace_file := CatalogsYaml.readable(path, _UNREADABLE_CATALOGS)) is not None:
            files.append(_CheckedFile(list(workspace_file.declarations()), policy))
    return files


def _warn_about_dependencies(files: list[_CheckedFile]) -> None:
    """Report the marker of each dependency, and check the dependencies each file declares, by the file's policy."""
    for file in files:
        report_markers(file.declarations, _LOG, file.policy)
    for file in files:
        warn_about_redundant_directives([file.declarations], _LOG, file.policy)
    registry_declarations = [
        [declaration for declaration in file.declarations if declaration.names_a_release] for file in files
    ]
    warn_about_delegated_checks(registry_declarations, _LOG, npmjs.SOURCE)


def _report_undeclared_references(files: list[PackageJson]) -> None:
    """Report the marker of each dependency that the `update-time` field names and its section does not declare."""
    for file in files:
        for reference in file.undeclared_references():
            _report_undeclared_reference(reference)


def _report_undeclared_reference(reference: Reference) -> None:
    """Report the marker of a dependency its section does not declare, and as redundant where it reads every item."""
    marker = reference.marker
    report_marker(reference, _LOG, steers_the_update=False)
    if marker.invalid_item is None and marker.raw:
        _LOG.redundant_directive(reference, marker.raw, Reason.UNDECLARED_DEPENDENCY)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's package.json files."""
    _update_package_jsons()


if __name__ == "__main__":  # pragma: no cover
    main()

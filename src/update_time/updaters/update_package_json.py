"""Find package.json files, update their dependencies with the project's Node package manager, and check them."""

from typing import TYPE_CHECKING

from update_time.domain.file_type import PACKAGE_JSON
from update_time.formats import json as json_format
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger, report_marker
from update_time.manifests import package_json as package_json_format
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
    from pathlib import Path

    from update_time.formats.json import JsonFile
    from update_time.markers.reference import SteeredReference

_LOG = get_logger("package.json")
# Files that signal which package manager a project uses, checked when there is no corepack `packageManager` field.
# pnpm is handled like npm; yarn and bun are detected only to be skipped (see node.SUPPORTED_MANAGERS). npm's
# `package-lock.json` is deliberately absent: npm is the default when nothing else matches, so listing it would be a
# no-op, and leaving it out lets a real non-npm lockfile win over a stale `package-lock.json` left by a migration.
_MANAGER_FILES = {"pnpm-lock.yaml": "pnpm", "pnpm-workspace.yaml": "pnpm", "yarn.lock": "yarn", "bun.lockb": "bun"}


def _package_manager(package_json: JsonFile) -> str:
    """Return the name of the project's package manager (npm, pnpm, yarn, bun), defaulting to npm.

    The nearest directory that names a manager decides, from the package.json's own upwards, so a workspace member
    takes the manager of its workspace root. In each directory, the corepack `packageManager` field of its
    package.json (e.g. `"pnpm@9.15.0"`) wins over a lockfile or a workspace file.
    """
    directory = package_json.path.parent
    if manager := _declared_manager(package_json) or _signalled_manager(directory):
        return manager
    for ancestor in directory.parents:
        if manager := _declared_manager_in(ancestor) or _signalled_manager(ancestor):
            return manager
    return "npm"


def _declared_manager(package_json: JsonFile) -> str:
    """Return the name of the manager that the package.json's corepack `packageManager` field names, if any."""
    contents = package_json.contents
    declared = contents.get("packageManager") if isinstance(contents, dict) else None
    return declared.split("@", maxsplit=1)[0] if isinstance(declared, str) else ""


def _declared_manager_in(directory: Path) -> str:
    """Return the name of the manager that the package.json in the directory names, if any."""
    package_json = directory / "package.json"
    if not package_json.exists():
        return ""
    try:
        return _declared_manager(json_format.read(package_json))
    except (OSError, ValueError) as error:
        _LOG.unreadable_manager_declaration(package_json, error)
        return ""


def _signalled_manager(directory: Path) -> str:
    """Return the name of the manager that a lockfile or a workspace file in the directory signals, if any."""
    return next((manager for name, manager in _MANAGER_FILES.items() if (directory / name).exists()), "")


def update_package_jsons() -> None:
    """Find all package.json files, update each with its (supported) package manager, and check its dependencies.

    The package manager runs only for a package.json that declares a dependency, since it writes a lockfile beside
    any package.json it runs for.
    """
    supported = []
    for package_json in glob_for(PACKAGE_JSON):
        file = json_format.read(package_json)
        if (manager := node.SUPPORTED_MANAGERS.get(name := _package_manager(file))) is None:
            _LOG.unsupported_package_manager(package_json, name, " and ".join(node.SUPPORTED_MANAGERS))
            continue
        if package_json_format.declares_a_dependency(file):
            manager.update_package_json(package_json)
        else:
            _LOG.no_dependencies(package_json, name, "install")
        supported.append(package_json)
    _warn_about_dependencies(supported)


def _warn_about_dependencies(package_jsons: list[Path]) -> None:
    """Report the marker of each dependency, and check the dependencies of each package.json."""
    files = [json_format.read(package_json) for package_json in package_jsons]
    declarations = [list(package_json_format.declarations(file)) for file in files]
    check_targets = [report_markers(file_declarations, _LOG, node.POLICY) for file_declarations in declarations]
    warn_about_redundant_directives(declarations, _LOG, node.POLICY)
    _report_undeclared_references(files)
    registry_declarations = [[target for target in targets if target.names_a_release] for targets in check_targets]
    warn_about_delegated_checks(
        registry_declarations, _LOG, project_resolver(npmjs.project), npmjs.deprecation, Ecosystem.NPM
    )


def _report_undeclared_references(files: list[JsonFile]) -> None:
    """Report the marker of each dependency that the `update-time` field names and its section does not declare."""
    for file in files:
        for reference in package_json_format.undeclared_references(file):
            _report_undeclared_reference(reference)


def _report_undeclared_reference(reference: SteeredReference) -> None:
    """Report the marker of a dependency its section does not declare, and one holding directives as redundant."""
    marker = reference.marker
    report_marker(_LOG, reference.dependency, marker, reference.location, holds_the_update_back=False)
    if marker.invalid_item is None and marker.raw:
        _LOG.redundant_directive(reference, marker.raw, Reason.UNDECLARED_DEPENDENCY)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's package.json files."""
    update_package_jsons()


if __name__ == "__main__":  # pragma: no cover
    main()

"""Find package.json files, update their dependencies with the project's Node package manager, and check them."""

from typing import TYPE_CHECKING

from update_time.domain.file_type import PACKAGE_JSON
from update_time.formats import json as json_format
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests import package_json as package_json_format
from update_time.package_managers import node
from update_time.references.delegated import (
    pin_resolver,
    project_resolver,
    warn_about_projects,
    warn_about_yanked_dependencies,
)
from update_time.references.vulnerability import warn_about_vulnerable_dependencies
from update_time.sources import npmjs
from update_time.sources.osv import Ecosystem

if TYPE_CHECKING:
    from pathlib import Path

_LOG = get_logger("package.json")
# Lockfiles that signal which package manager a project uses, checked when there is no corepack `packageManager`
# field. pnpm is handled like npm; yarn and bun are detected only to be skipped (see node.SUPPORTED_MANAGERS). npm's
# `package-lock.json` is deliberately absent: npm is the default when nothing else matches, so listing it would be a
# no-op, and leaving it out lets a real non-npm lockfile win over a stale `package-lock.json` left by a migration.
_LOCKFILES = {"pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "bun.lockb": "bun"}


def _package_manager(package_json: Path) -> str:
    """Return the name of the project's package manager (npm, pnpm, yarn, bun), defaulting to npm.

    The corepack `packageManager` field (e.g. `"pnpm@9.15.0"`) is authoritative; otherwise a sibling lockfile is
    used as a fallback signal.
    """
    if declared := json_format.read(package_json).contents.get("packageManager", ""):
        return declared.split("@", maxsplit=1)[0]
    for lockfile, manager in _LOCKFILES.items():
        if (package_json.parent / lockfile).exists():
            return manager
    return "npm"


def update_package_jsons() -> None:
    """Find all package.json files, update each with its (supported) package manager, and check its dependencies."""
    supported = []
    for package_json in glob_for(PACKAGE_JSON):
        if (manager := node.SUPPORTED_MANAGERS.get(name := _package_manager(package_json))) is None:
            _LOG.unsupported_package_manager(package_json, name, " and ".join(node.SUPPORTED_MANAGERS))
        else:
            manager.update_package_json(package_json)
            supported.append(package_json)
    _warn_about_dependencies(supported)


def _warn_about_dependencies(package_jsons: list[Path]) -> None:
    """Check the dependencies of each package.json."""
    declared = [list(package_json_format.declared_dependencies(package_json)) for package_json in package_jsons]
    warn_about_projects(declared, project_resolver(npmjs.project), _LOG)
    pinned = [[reference for reference in references if reference.current_version] for references in declared]
    warn_about_yanked_dependencies(pinned, pin_resolver(npmjs.deprecation), _LOG)
    warn_about_vulnerable_dependencies(pinned, Ecosystem.NPM, _LOG)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's package.json files."""
    update_package_jsons()


if __name__ == "__main__":  # pragma: no cover
    main()

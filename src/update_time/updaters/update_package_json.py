"""Find package.json files and update their dependencies with the project's Node package manager."""

from typing import TYPE_CHECKING

from update_time.domain.dependency import DependencyVersion
from update_time.domain.reference import ResolvedReference
from update_time.domain.staleness import warn_about_stale_dependencies
from update_time.file_formats import package_json as package_json_format
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.package_managers import node
from update_time.sources import npmjs

if TYPE_CHECKING:
    from collections.abc import Iterable
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
    if declared := package_json_format.read(package_json).get("packageManager", ""):
        return declared.split("@", maxsplit=1)[0]
    for lockfile, manager in _LOCKFILES.items():
        if (package_json.parent / lockfile).exists():
            return manager
    return "npm"


def update_package_jsons() -> None:
    """Find all package.json files and update each with its (supported) package manager, skipping the rest."""
    supported = []
    for package_json in glob("package.json"):
        if (manager := node.SUPPORTED_MANAGERS.get(name := _package_manager(package_json))) is None:
            _LOG.unsupported_package_manager(package_json, name, " and ".join(node.SUPPORTED_MANAGERS))
        else:
            manager.update_package_json(package_json)
            supported.append(package_json)
    warn_about_stale_dependencies(supported, _newest_releases, _LOG.warn_if_stale)


def _newest_releases(package_json: Path) -> Iterable[ResolvedReference]:
    """Yield each declared dependency the npm registry has a newest release for, at each line declaring it.

    The reference names no version: a `package.json` declares a range, and which version it resolves to is recorded
    in the lock file. Staleness is measured against the package's newest release, so the range is not needed.
    """
    return (
        ResolvedReference(name, "", location, release=DependencyVersion.unpinned(newest))
        for name, locations in package_json_format.dependency_locations(package_json).items()
        for location in locations
        if (newest := npmjs.newest_release(name)) is not None
    )


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's package.json files."""
    update_package_jsons()


if __name__ == "__main__":  # pragma: no cover
    main()

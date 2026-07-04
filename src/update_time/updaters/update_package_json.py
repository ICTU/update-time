"""Find package.json files and update their dependencies with the project's Node package manager."""

import sys
from typing import TYPE_CHECKING

from update_time.file_formats import package_json as package_json_format
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.package_managers import node

if TYPE_CHECKING:
    from pathlib import Path

LOG = get_logger("package.json")
# Lockfiles that signal which package manager a project uses, checked when there is no corepack `packageManager`
# field. pnpm is handled like npm; yarn and bun are detected only to be skipped (see node.SUPPORTED_MANAGERS). npm's
# `package-lock.json` is deliberately absent: npm is the default when nothing else matches, so listing it would be a
# no-op, and leaving it out lets a real non-npm lockfile win over a stale `package-lock.json` left by a migration.
LOCKFILES = {"pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "bun.lockb": "bun"}


def package_manager(package_json: Path) -> str:
    """Return the name of the project's package manager (npm, pnpm, yarn, bun), defaulting to npm.

    The corepack `packageManager` field (e.g. `"pnpm@9.15.0"`) is authoritative; otherwise a sibling lockfile is
    used as a fallback signal.
    """
    if declared := package_json_format.read(package_json).get("packageManager", ""):
        return declared.split("@", maxsplit=1)[0]
    for lockfile, manager in LOCKFILES.items():
        if (package_json.parent / lockfile).exists():
            return manager
    return "npm"


def update_package_jsons() -> int:
    """Find all package.json files and update each with its (supported) package manager, skipping the rest."""
    for package_json in glob("package.json"):
        if (manager := node.SUPPORTED_MANAGERS.get(name := package_manager(package_json))) is None:
            LOG.unsupported_package_manager(package_json, name, "npm and pnpm")
        else:
            manager.update_package_json(package_json)
    return 0


def main() -> int:  # pragma: no cover
    """Update the dependencies in the repository's package.json files."""
    return update_package_jsons()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

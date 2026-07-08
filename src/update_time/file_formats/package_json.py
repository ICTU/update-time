"""Read package.json files.

This module owns parsing the package.json *format*. What the parsed contents mean (which package manager, which
engines, which dependencies to update) is the caller's concern.
"""

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# The dependency sections whose direct dependencies npm/pnpm install for this project (peerDependencies are
# constraints on the consumer, not installed here, so they are left out). pnpm's `list --json` output splits its
# installed dependencies over the same sections, so `package_managers.node` reads them from here too.
DEPENDENCY_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")


def read(path: Path) -> dict:
    """Return the parsed package.json."""
    return json.loads(path.read_text())


def dependency_names(path: Path) -> list[str]:
    """Return the names of the direct registry dependencies across the dependency sections, without duplicates.

    Only plain semver-range specs are included; git, file, link, workspace, alias, and github-shorthand references
    (recognisable by a `:` or `/` in the spec) are skipped, since they don't resolve to an npm registry release.
    """
    config = read(path)
    names = [
        name
        for section in DEPENDENCY_SECTIONS
        for name, spec in config.get(section, {}).items()
        if isinstance(spec, str) and ":" not in spec and "/" not in spec
    ]
    return list(dict.fromkeys(names))

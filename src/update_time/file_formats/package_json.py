"""Read package.json files.

This module owns parsing the package.json *format*. What the parsed contents mean (which package manager, which
engines, which dependencies to update) is the caller's concern.
"""

import json
import re
from typing import TYPE_CHECKING

from update_time.primitives.location import Location
from update_time.primitives.text import line_number

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.dependency import DependencyName

# The dependency sections whose direct dependencies npm/pnpm install for this project (peerDependencies are
# constraints on the consumer, not installed here, so they are left out). pnpm's `list --json` output splits its
# installed dependencies over the same sections, so `package_managers.node` reads them from here too.
DEPENDENCY_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")


def read(path: Path) -> dict:
    """Return the parsed package.json."""
    return json.loads(path.read_text())


def dependency_locations(path: Path) -> dict[DependencyName, list[Location]]:
    """Return each direct registry dependency and the locations of the entries declaring it.

    The dependency sections are read in turn, leaving out a dependency whose spec resolves to no registry release.
    A name declared in several of them carries a location per entry, so none of the lines it sits on is lost.
    """
    contents = path.read_text()
    config = json.loads(contents)
    locations: dict[DependencyName, list[Location]] = {}
    for section in DEPENDENCY_SECTIONS:
        for name, spec in config.get(section, {}).items():
            if _is_registry_spec(spec):
                locations.setdefault(name, []).append(_entry_location(path, contents, section, name))
    return locations


def _is_registry_spec(spec: object) -> bool:
    """Return whether the spec is a plain semver range that resolves to an npm registry release.

    A git, file, link, workspace, alias, or github-shorthand reference does not, and is recognisable by the `:` or
    `/` it carries.
    """
    return isinstance(spec, str) and ":" not in spec and "/" not in spec


def _entry_location(path: Path, contents: str, section: str, name: str) -> Location:
    """Return where the section declares the dependency, or the file alone when its entry cannot be found.

    The name is looked for as a key inside the section's own object, so a key of the same name elsewhere in the
    file — in `peerDependencies` or `overrides`, say — is never taken for it, and neither is a spec naming the
    package as a value. Where the search comes up empty, the dependency is located at the file rather than at a
    line guessed from elsewhere.
    """
    entry = rf'"{re.escape(name)}"\s*:'
    # A dependency section is a flat object, so `[^}]` cannot reach past its closing brace: the entry matched is
    # one of this section's own.
    match = re.search(rf'"{section}"\s*:\s*\{{[^}}]*?({entry})', contents)
    if match is None:
        return Location(path)
    return Location(path, line_number(contents, match.start(1)))

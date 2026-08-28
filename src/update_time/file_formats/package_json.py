"""Read package.json files.

This module owns parsing the package.json *format*. What the parsed contents mean (which package manager, which
engines, which dependencies to update) is the caller's concern.
"""

from typing import TYPE_CHECKING

from update_time.file_formats import json as json_format
from update_time.markers.marker import Marker, ReferenceMarker, parse_directives
from update_time.primitives.location import Location
from update_time.primitives.text import column, line_number

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.dependency import DependencyName
    from update_time.file_formats.json import JsonFile

# The dependency sections whose direct dependencies npm/pnpm install for this project (peerDependencies are
# constraints on the consumer, not installed here, so they are left out). pnpm's `list --json` output splits its
# installed dependencies over the same sections, so `package_managers.node` reads them from here too.
DEPENDENCY_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")

# The field a package.json names its markers in. JSON has no comments, so a marker cannot sit beside the reference
# it steers, and npm keeps a field it does not know, so the file can carry one.
_MARKER_FIELD = "update-time"


def reference_marker(package_json: JsonFile, section: str, name: DependencyName) -> ReferenceMarker:
    """Return where the section declares the name, and the marker the `update-time` field names for that entry."""
    location = _entry_location(package_json, section, name)
    return ReferenceMarker(location, _marker(package_json.contents, section, name))


def _marker(contents: dict, section: str, name: DependencyName) -> Marker:
    """Return the marker the `update-time` field names for the reference, or an empty one where it names none.

    The field mirrors the file's own sections, so `{"update-time": {"engines": {"node": "ignore"}}}` steers the
    Node engine, and a reference is named the way the file itself names it. The value is the directive list a
    comment carries behind its `# update-time:` prefix. A file names no marker by leaving any of the three out.
    Whatever a file holds reaches here, so each step down is checked: a level that is not an object, and a value
    that is not a directive list, are both a shape the language cannot read, and leave the reference as it is
    rather than ending the run.
    """
    field = contents.get(_MARKER_FIELD, {})
    if not isinstance(field, dict):
        return _unreadable_field(section, name)
    references = field.get(section, {})
    if not isinstance(references, dict):
        return _unreadable_field(section, name)
    directives = references.get(name, "")
    return parse_directives(directives) if isinstance(directives, str) else _unreadable_field(section, name)


def _unreadable_field(section: str, name: DependencyName) -> Marker:
    """Return the marker for a field the language cannot read: an invalid item naming where the marker would sit."""
    return Marker(invalid_item=f"{_MARKER_FIELD}.{section}.{name}")


def dependency_locations(path: Path) -> dict[DependencyName, list[Location]]:
    """Return each direct registry dependency and the locations of the entries declaring it.

    The dependency sections are read in turn, leaving out a dependency whose spec resolves to no registry release.
    A name declared in several of them carries a location per entry, so none of the lines it sits on is lost.
    """
    package_json = json_format.read(path)
    locations: dict[DependencyName, list[Location]] = {}
    for section in DEPENDENCY_SECTIONS:
        for name, spec in package_json.contents.get(section, {}).items():
            if _is_registry_spec(spec):
                locations.setdefault(name, []).append(_entry_location(package_json, section, name))
    return locations


def _is_registry_spec(spec: object) -> bool:
    """Return whether the spec is a plain semver range that resolves to an npm registry release.

    A git, file, link, workspace, alias, or github-shorthand reference does not, and is recognisable by the `:` or
    `/` it carries.
    """
    return isinstance(spec, str) and ":" not in spec and "/" not in spec


def _entry_location(package_json: JsonFile, section: str, name: str) -> Location:
    """Return where the section declares the name, down to the column its entry starts on.

    Where the file declares no such entry, it is located at the file rather than at a line guessed from elsewhere.
    """
    text = package_json.text
    offset = json_format.entry_offset(text, section, name)
    if offset is None:
        return Location(package_json.path)
    return Location(package_json.path, line_number(text, offset), column(text, offset))

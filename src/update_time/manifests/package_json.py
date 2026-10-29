"""Read package.json files: their dependency entries, Node engine, package manager, and `update-time` field."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from update_time.domain.reference import Reference
from update_time.formats import json as json_format
from update_time.markers.marker import Marker, ReferenceMarker, parse_directives
from update_time.markers.reference import SteeredReference
from update_time.primitives.location import Location
from update_time.primitives.text import column, line_number

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from update_time.domain.dependency import DependencyName

# The dependency sections whose entries Update-time checks. pnpm's `list --json` output splits its installed
# dependencies over the same sections, so `package_managers.node` reads them from here too.
DEPENDENCY_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")

# The dependency sections npm and pnpm install from. A peer dependency states a range its consumer has to satisfy,
# so Update-time does not check it, but npm and pnpm install it all the same.
_INSTALLED_SECTIONS = (*DEPENDENCY_SECTIONS, "peerDependencies")

# The field a package.json names its markers in. JSON has no comments, so a marker cannot sit beside the reference
# it steers, and npm keeps a field it does not know, so the file can carry one.
_MARKER_FIELD = "update-time"

# The section a package.json declares the Node engine in, and the name it declares it under. Its marker is named
# the same way, in the file's `update-time` field.
_ENGINES = "engines"
NODE_ENGINE = "node"
NODE_ENGINE_REFERENCE = re.compile(rf'"(?P<dependency>{NODE_ENGINE})": "(?P<version>[\d\.]+)"')

# A spec that pins a dependency to one version, such as 1.3.0, =v1.3.0, or 1.3.0+build. npm ignores the build.
_EXACT_VERSION = re.compile(r"=?v?(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)(?:\+[0-9A-Za-z.-]+)?")


def _marker(contents: dict, section: str, name: DependencyName) -> Marker:
    """Return the marker the `update-time` field names for the reference, or an empty one where it names none.

    The field mirrors the file's own sections, so `{"update-time": {"engines": {"node": "ignore"}}}` steers the
    Node engine, and a reference is named the way the file itself names it. The value is the directive list a
    comment carries behind its `# update-time:` prefix. A file names no marker by leaving any of the three out.
    Whatever a file holds reaches here, so each step down is checked: a level that is not an object, and a value
    that is not a directive list, are both a shape the language cannot read, and leave the reference as it is
    rather than ending the run.
    """
    references = _section_markers(contents, section)
    if references is None:
        return _unreadable_field(section, name)
    directives = references.get(name, "")
    return parse_directives(directives) if isinstance(directives, str) else _unreadable_field(section, name)


def _section_markers(contents: dict, section: str) -> dict | None:
    """Return what the `update-time` field holds for the section, or None where either is not an object.

    A file that leaves either out reads as naming an empty object for the section.
    """
    field = contents.get(_MARKER_FIELD, {})
    references = field.get(section, {}) if isinstance(field, dict) else None
    return references if isinstance(references, dict) else None


def _unreadable_field(section: str, name: DependencyName) -> Marker:
    """Return the marker for a field the language cannot read: an invalid item naming where the marker would sit."""
    return Marker(invalid_item=f"{_MARKER_FIELD}.{section}.{name}")


@dataclass(frozen=True, kw_only=True)
class Declaration(SteeredReference):
    """A dependency declared in a package.json section, and whether its spec names a release on the npm registry."""

    names_a_release: bool


def _is_registry_spec(spec: object) -> bool:
    """Return whether the spec is a plain semver range that resolves to an npm registry release.

    A git, file, link, workspace, alias, or github-shorthand reference does not, and is recognisable by the `:` or
    `/` it carries.
    """
    return isinstance(spec, str) and ":" not in spec and "/" not in spec


@dataclass(frozen=True)
class PackageJson(json_format.JsonFile):
    """A package.json: its dependency entries, its Node engine, and the markers its `update-time` field names."""

    @classmethod
    def readable(cls, path: Path, report: Callable[[Path, OSError | ValueError], None] | None = None) -> Self | None:
        """Return the package.json, or None where it cannot be read, after reporting why where a report is given."""
        try:
            return cls.read(path)
        except (OSError, ValueError) as error:
            if report is not None:
                report(path, error)
            return None

    def node_engine_marker(self) -> ReferenceMarker:
        """Return the entry declaring the Node engine, and the marker the package.json names for it.

        The engine is the entry the `engines` section declares under `node`. A `node` version another section
        declares — a Volta pin, a dependency of that name — is neither taken for the engine nor steered by the
        engine's marker.
        """
        return self._reference_marker(_ENGINES, NODE_ENGINE)

    def declares_a_node_engine(self) -> bool:
        """Return whether the package.json declares a Node engine."""
        engines = self.contents.get(_ENGINES)
        return isinstance(engines, dict) and NODE_ENGINE in engines

    def declared_manager(self) -> str:
        """Return the name of the manager the corepack `packageManager` field names, or an empty string if none."""
        declared = self.contents.get("packageManager")
        return declared.split("@", maxsplit=1)[0] if isinstance(declared, str) else ""

    def declares_a_dependency(self) -> bool:
        """Return whether the package.json declares a dependency for npm and pnpm to install."""
        return any(self.contents.get(section) for section in _INSTALLED_SECTIONS)

    def declarations(self) -> Iterator[Declaration]:
        """Yield a declaration for each entry of the dependency sections, at the exact version it pins if any.

        Each declaration carries the marker the `update-time` field names for its entry.
        """
        for section in DEPENDENCY_SECTIONS:
            for name, spec in self.contents.get(section, {}).items():
                names_a_release = _is_registry_spec(spec)
                match = _EXACT_VERSION.fullmatch(spec) if names_a_release else None
                version = match["version"] if match else ""
                steered = self._reference_marker(section, name)
                yield Declaration(
                    name, version, steered.reference_location, marker=steered.marker, names_a_release=names_a_release
                )

    def undeclared_references(self) -> Iterator[SteeredReference]:
        """Yield a reference to each name the `update-time` field gives under a section that does not declare it.

        Each reference is located at the line in the field that names it.
        """
        for section in DEPENDENCY_SECTIONS:
            entries = self.contents.get(section, {})
            for name in _section_markers(self.contents, section) or {}:
                if name not in entries:
                    location = self._entry_location(_MARKER_FIELD, section, name)
                    yield SteeredReference(name, "", location, marker=_marker(self.contents, section, name))

    def dependency_locations(self) -> dict[DependencyName, list[Location]]:
        """Return each direct registry dependency and the locations of the entries declaring it.

        A name declared in several sections carries a location per entry, so none of the lines it sits on is lost.
        """
        locations: dict[DependencyName, list[Location]] = {}
        for declaration in self.declarations():
            if declaration.names_a_release:
                locations.setdefault(declaration.dependency, []).append(declaration.location)
        return locations

    def declared_ranges(self) -> dict[tuple[str, DependencyName], Reference]:
        """Return a reference to the range each registry dependency declares, keyed by its section and name."""
        return {
            (section, name): Reference(name, spec, self._entry_location(section, name))
            for section in DEPENDENCY_SECTIONS
            for name, spec in self.contents.get(section, {}).items()
            if _is_registry_spec(spec)
        }

    def _reference_marker(self, section: str, name: DependencyName) -> ReferenceMarker:
        """Return where the section declares the name, and the marker the `update-time` field names for that entry."""
        return ReferenceMarker(self._entry_location(section, name), _marker(self.contents, section, name))

    def _entry_location(self, *keys: str) -> Location:
        """Return the location of the entry that the keys name, down to the column.

        Where the file declares no such entry, it is located at the file rather than at a line guessed from elsewhere.
        """
        offset = json_format.entry_offset(self.text, *keys)
        if offset is None:
            return Location(self.path)
        return Location(self.path, line_number(self.text, offset), column(self.text, offset))

"""Read package.json files: their dependency entries, Node engine, package manager, and `update-time` field."""

import json
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from update_time.domain.marker import Marker
from update_time.domain.reference import Reference
from update_time.formats import json as json_format
from update_time.io.file import File
from update_time.manifests.npm_declaration import CatalogEntry, Catalogs, catalog_name, declaration, is_registry_spec
from update_time.markers.parser import parse_directives
from update_time.primitives.location import Location
from update_time.primitives.text import column, line_number

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from update_time.domain.dependency import DependencyName
    from update_time.manifests.npm_declaration import Declaration

# The dependency sections whose entries Update-time checks.
DEPENDENCY_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")

# A path of keys from the top of a package.json to an object in it, such as a dependency section.
type _KeyPath = tuple[str, ...]

# The paths of the dependency sections.
_DEPENDENCY_SECTION_PATHS: tuple[_KeyPath, ...] = tuple((section,) for section in DEPENDENCY_SECTIONS)

# The object of a package.json that holds its catalogs where it holds a catalog at all. bun reads the top level
# instead where this object holds neither.
_WORKSPACES: _KeyPath = ("workspaces",)

# The key of the unnamed catalog, and the key that holds the named catalogs.
_CATALOG, _NAMED_CATALOGS = "catalog", "catalogs"

# The dependency sections npm, pnpm, yarn, and bun install from. A peer dependency states a range its consumer has to
# satisfy, so Update-time does not check it, but the package managers install it all the same.
_INSTALLED_SECTIONS = (*DEPENDENCY_SECTIONS, "peerDependencies")

# The field a package.json names its markers in. JSON has no comments, so a marker cannot sit beside the reference
# it steers, and npm keeps a field it does not know, so the file can carry one.
_MARKER_FIELD = "update-time"

# The section a package.json declares the Node engine in, and the name it declares it under. Its marker is named
# the same way, in the file's `update-time` field.
_ENGINES = "engines"
NODE_ENGINE = "node"
NODE_ENGINE_REFERENCE = re.compile(rf'"(?P<dependency>{NODE_ENGINE})": "(?P<version>[\d\.]+)"')


def _marker(contents: dict, section: _KeyPath, name: DependencyName) -> Marker:
    """Return the marker the `update-time` field names for the reference, or an empty one where it names none.

    The field mirrors the file's own sections, so `{"update-time": {"engines": {"node": "ignore"}}}` steers the
    Node engine, and a reference is named the way the file itself names it. The value is the directive list a
    comment carries behind its `# update-time:` prefix. Where the file leaves a level out, the reference gets an
    empty marker. Whatever a file holds reaches here, so each step down is checked: a level that is not an object,
    and a value that is not a directive list, are both a shape the language cannot read, which becomes an invalid
    item rather than ending the run. The marker is spelled as the JSON of the whole field.
    """
    references = _section_markers(contents, section)
    directives = references.get(name, "") if references is not None else None
    if not isinstance(directives, str):
        return Marker(invalid_item=".".join((_MARKER_FIELD, *section, name)), spelling=_field_spelling(contents))
    marker = parse_directives(directives)
    return replace(marker, spelling=_field_spelling(contents)) if marker.raw else marker


def _section_markers(contents: dict, section: _KeyPath) -> dict | None:
    """Return what the `update-time` field holds for the section, or None where a level on its path is not an object."""
    return _object_at(contents, (_MARKER_FIELD, *section))


def _object_at(contents: dict, keys: tuple[str, ...]) -> dict | None:
    """Return the object the keys lead to, or None where a level on their path is not an object.

    Where the contents leave a level out, the level reads as an empty object.
    """
    level: object = contents
    for key in keys:
        level = level.get(key, {}) if isinstance(level, dict) else None
    return level if isinstance(level, dict) else None


def _field_spelling(contents: dict) -> str:
    """Return the `update-time` field as JSON, which spells each marker the field names."""
    return json.dumps(contents[_MARKER_FIELD], ensure_ascii=False)


@dataclass(frozen=True)
class PackageJson(json_format.JsonFile, File):
    """A package.json: its dependency entries, its Node engine, and the markers its `update-time` field names."""

    def node_engine(self) -> Reference:
        """Return the reference to the entry declaring the Node engine, steered by the marker the package.json names.

        The engine is the entry the `engines` section declares under `node`. A `node` version another section
        declares — a Volta pin, a dependency of that name — is neither taken for the engine nor steered by the
        engine's marker.
        """
        return self._entry_reference((_ENGINES,), NODE_ENGINE)

    def declares_a_node_engine(self) -> bool:
        """Return whether the package.json declares a Node engine."""
        engines = self.contents.get(_ENGINES)
        return isinstance(engines, dict) and NODE_ENGINE in engines

    def declared_manager(self) -> str:
        """Return the name of the manager the corepack `packageManager` field names, or an empty string if none."""
        declared = self.contents.get("packageManager")
        return declared.split("@", maxsplit=1)[0] if isinstance(declared, str) else ""

    def declares_a_dependency(self) -> bool:
        """Return whether the package.json declares a dependency for its package manager to install."""
        return any(self.contents.get(section) for section in _INSTALLED_SECTIONS)

    def declares_workspaces(self) -> bool:
        """Return whether the package.json declares the members of a workspace."""
        return bool(self.workspace_patterns())

    def workspace_patterns(self) -> list[str]:
        """Return the glob patterns naming the workspace's members, in an array or in the `packages` of an object."""
        workspaces = self.contents.get("workspaces")
        patterns = workspaces.get("packages") if isinstance(workspaces, dict) else workspaces
        return [pattern for pattern in patterns if isinstance(pattern, str)] if isinstance(patterns, list) else []

    def declarations(self) -> Iterator[Declaration]:
        """Yield a declaration for each entry of the dependency sections and the catalogs.

        Each declaration carries the exact version its entry pins, if any, and the marker the `update-time` field
        names for its entry.
        """
        yield from self._declarations(self._declaring_sections())

    def _declarations(self, sections: Iterable[_KeyPath]) -> Iterator[Declaration]:
        """Yield a declaration for each entry of the sections."""
        for section in sections:
            for name, spec in self._entries(section).items():
                location = self._entry_location(*section, name)
                yield declaration(name, spec, location, _marker(self.contents, section, name))

    def undeclared_references(self) -> Iterator[Reference]:
        """Yield a reference to each name the `update-time` field gives under a section that does not declare it.

        Each reference is located at the line in the field that names it.
        """
        for section in self._declaring_sections():
            entries = self._entries(section)
            for name in _section_markers(self.contents, section) or {}:
                if name not in entries:
                    location = self._entry_location(_MARKER_FIELD, *section, name)
                    yield Reference(name, "", location, marker=_marker(self.contents, section, name))

    def dependency_references(self, catalogs: Catalogs) -> dict[DependencyName, list[Reference]]:
        """Return a reference to each entry declaring a direct registry dependency, keyed by the name it declares.

        A name declared in several sections carries a reference per entry, so none of the lines it sits on is lost. A
        dependency that takes its version from a catalog is located at the catalog's entry. The reference names the
        registry package, which is the one an alias names.
        """
        references: dict[DependencyName, list[Reference]] = {}
        for dependency_declaration in self._declarations(_DEPENDENCY_SECTION_PATHS):
            if dependency_declaration.names_a_release:
                location: Location | None = dependency_declaration.location
            else:
                location = catalogs.location_of(dependency_declaration)
            if location is not None:
                reference = Reference(dependency_declaration.dependency, "", location)
                references.setdefault(dependency_declaration.declared_name, []).append(reference)
        return references

    def catalog_dependencies(self) -> list[CatalogEntry]:
        """Return the catalog entry each dependency that takes its version from a catalog takes it from."""
        return [
            CatalogEntry(declaration.catalog, declaration.dependency)
            for declaration in self._declarations(_DEPENDENCY_SECTION_PATHS)
            if declaration.catalog is not None
        ]

    def declared_ranges(self) -> dict[tuple[_KeyPath, DependencyName], Reference]:
        """Return a reference to the range each registry dependency declares, keyed by its section and name."""
        return {
            (section, name): Reference(name, spec, self._entry_location(*section, name))
            for section in _DEPENDENCY_SECTION_PATHS
            for name, spec in self._entries(section).items()
            if is_registry_spec(spec)
        }

    def catalogs(self) -> Catalogs:
        """Return the catalogs the package.json holds."""
        return Catalogs(
            {
                CatalogEntry(catalog, name): Reference(name, spec, self._entry_location(*section, name))
                for catalog, section in self._catalogs()
                for name, spec in self._entries(section).items()
            }
        )

    def _declaring_sections(self) -> Iterator[_KeyPath]:
        """Yield the path of each section whose entries are declarations: the dependency sections and the catalogs."""
        yield from _DEPENDENCY_SECTION_PATHS
        yield from (section for _catalog, section in self._catalogs())

    def _catalogs(self) -> Iterator[tuple[str, _KeyPath]]:
        """Yield the name of each catalog, empty for the unnamed one, and the path of its section.

        A named catalog is yielded where the file or its `update-time` field names it.
        """
        holder = self._catalog_holder()
        yield "", (*holder, _CATALOG)
        named_catalogs = (*holder, _NAMED_CATALOGS)
        declared = _object_at(self.contents, named_catalogs) or {}
        marked = _section_markers(self.contents, named_catalogs) or {}
        yield from ((catalog_name(name), (*named_catalogs, name)) for name in declared | marked)

    def _catalog_holder(self) -> _KeyPath:
        """Return the path of the object holding the catalogs: `workspaces` where it holds one, else the top level."""
        workspaces = _object_at(self.contents, _WORKSPACES) or {}
        return _WORKSPACES if _CATALOG in workspaces or _NAMED_CATALOGS in workspaces else ()

    def _entries(self, section: _KeyPath) -> dict:
        """Return the entries of the section, or an empty object where a level of its path is not an object."""
        return _object_at(self.contents, section) or {}

    def _entry_reference(self, section: _KeyPath, name: DependencyName) -> Reference:
        """Return the reference to the entry declaring the name, steered by the marker the `update-time` field names."""
        return Reference(name, "", self._entry_location(*section, name), marker=_marker(self.contents, section, name))

    def _entry_location(self, *keys: str) -> Location:
        """Return the location of the entry that the keys name, down to the column.

        Where the file declares no such entry, it is located at the file rather than at a line guessed from elsewhere.
        """
        offset = json_format.entry_offset(self.text, *keys)
        if offset is None:
            return Location(self.path)
        return Location(self.path, line_number(self.text, offset), column(self.text, offset))

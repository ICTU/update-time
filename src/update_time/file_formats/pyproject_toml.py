"""Read, edit, and rewrite pyproject.toml files, preserving their formatting.

An inline script metadata block declares its dependencies as the same TOML, so reading and rewriting those go
through here too. The `DependencyTomlFile` the caller hands over says where in the file that TOML sits, so
nothing here knows which of the two kinds of file it is looking at.
"""

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import tomlkit
import tomlkit.items
from packaging.requirements import InvalidRequirement, Requirement

from update_time.domain.dependency import is_valid, normalized_python_name
from update_time.domain.line import Line, located_lines
from update_time.file_formats import toml
from update_time.markers.marker import Marker, Scope, parse_marker
from update_time.markers.reference import SteeredReference
from update_time.primitives.location import Location
from update_time.primitives.text import line_number

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, MutableSequence
    from pathlib import Path

    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.file_formats.dependency_file import DependencyTomlFile


type _DeclarationPosition = int


def _placeholder(position: _DeclarationPosition) -> str:
    """Return the text put in a spec's place to find the line declaring it, distinct from every other spec's.

    The position is wrapped rather than appended, so one placeholder is never the start of another.
    """
    return f"update-time-spec-{position}-update-time-spec"


def tool_key(path: Path, table: str, key: str) -> tuple[str, str] | None:
    """Return the (value, trailing-comment) of `[tool.<table>] <key>`, or None when it isn't set.

    The trailing comment is returned verbatim (with its leading `#`, or empty when there is none), so a caller can
    recognise a line it wrote itself.
    """
    document = tomlkit.parse(path.read_text())
    item = document.get("tool", {}).get(table, {}).get(key)
    if item is None:
        return None
    return str(item), item.trivia.comment


def set_tool_key(path: Path, table: str, key: str, value: str, *, comment: str = "") -> None:
    """Set `[tool.<table>] <key> = value` (with an optional trailing comment) and write the file back."""
    document = tomlkit.parse(path.read_text())
    tool = document.setdefault("tool", tomlkit.table(is_super_table=True))
    if table not in tool:
        tool[table] = tomlkit.table()
    item = tomlkit.item(value)
    if comment:
        item.comment(comment)
    tool[table][key] = item
    path.write_text(tomlkit.dumps(document))


def rewrite_pinned_versions(file: DependencyTomlFile, versions: dict[Declaration, VersionString]) -> None:
    """Rewrite each declaration to the version `versions` holds for it; write the file if changed.

    One declaration of a name is rewritten while another declaration of that name keeps its pinned version. The
    declarations are the ones `declared_dependencies` read from this file.
    """
    declarations = {declaration.position: declaration for declaration in versions}

    def new_spec(position: _DeclarationPosition, spec: str) -> str | None:
        if (declaration := declarations.get(position)) is None:
            return None
        rewritten = _rewritten_spec(spec, declaration, versions[declaration])
        return rewritten if rewritten != spec else None

    if (rewritten_contents := _replaced_specs(file, file.read(), new_spec)) is not None:
        file.write(rewritten_contents)


def _replaced_specs(
    file: DependencyTomlFile, contents: str, replace: Callable[[_DeclarationPosition, str], str | None]
) -> str | None:
    """Return the file's text with each declared spec `replace` gives a replacement for, or None when it gives none.

    Every call walks the arrays in the same order, so a spec has the same position in each of them.

    Only the specs in the dependency arrays change. A string spelled like a spec elsewhere in the file, and the
    comments, quoting, and whitespace around the specs, come back as they were. A file that is not valid TOML
    comes back as None as well, so one malformed file does not abort the run.
    """
    if (document := toml.parse_document(file.toml(contents))) is None:
        return None
    replaced = False
    position = 0
    for array in _dependency_arrays(document):
        for index, spec in enumerate(array):
            if not isinstance(spec, tomlkit.items.String):
                continue
            position += 1
            if (new_spec := replace(position, spec)) is not None:
                array[index] = toml.string(new_spec, quoted_as=spec)
                replaced = True
    return file.with_toml(contents, tomlkit.dumps(document)) if replaced else None


def _config(toml_text: str) -> dict:
    """Return the TOML parsed, or an empty config when it isn't valid TOML."""
    return toml.parse(toml_text) or {}


def _uv_table(config: Mapping) -> Mapping:
    """Return the config's `[tool.uv]` table, or an empty table when it has none."""
    return config.get("tool", {}).get("uv", {})


def _uv_source_names(config: dict) -> set[DependencyName]:
    """Return the names of the dependencies uv resolves from a source of its own, each normalised.

    uv matches a `sources` key to a dependency by the normalised name, so a key spells the dependency any of the
    ways that normalize to the same name.
    """
    return {normalized_python_name(name) for name in _uv_table(config).get("sources", {})}


def _dependency_arrays(config: Mapping) -> list[MutableSequence]:
    """Return every array of dependency specs the config declares.

    A pyproject.toml declares them in the project's own array, in an array per extra, in an array per dependency
    group, in uv's legacy `[tool.uv] dev-dependencies`, and in the `[build-system]` requirements. uv still resolves
    that legacy array, so it is read like the rest. An inline script metadata block declares them in one array of
    its own.
    """
    project = config.get("project", {})
    return [
        project.get("dependencies", []),
        *project.get("optional-dependencies", {}).values(),
        *config.get("dependency-groups", {}).values(),
        _uv_table(config).get("dev-dependencies", []),
        config.get("build-system", {}).get("requires", []),
        config.get("dependencies", []),
    ]


@dataclass(frozen=True, kw_only=True)
class Declaration(SteeredReference):
    """A dependency a file declares, and where the file says its release comes from.

    `uv_sourced` says the file names this dependency in its `[tool.uv] sources` table, so uv resolves it from a
    path, a workspace member, a git repository, or an index of its own. `direct_url` says the declaration points
    at a URL or a git repository rather than at a release. What follows from naming no release is the caller's.
    `position` says which spec of the file this declaration is, counting from one.
    """

    uv_sourced: bool
    direct_url: bool
    position: _DeclarationPosition

    @property
    def pins_a_version(self) -> bool:
        """Return whether the declaration pins an exact version, so a check about that version has one to run on."""
        return bool(self.current_version)

    @property
    def names_no_release(self) -> bool:
        """Return whether the declaration points at something other than a release of the package it names."""
        return self.direct_url or self.uv_sourced

    @property
    def updatable(self) -> bool:
        """Return whether the declaration's own marker leaves its version to be updated.

        A declaration that pins no exact version has no pin to freeze, so an `ignore[update]` on it holds nothing
        back.
        """
        return not (self.pins_a_version and self.marker.ignores(Scope.UPDATE))


def declared_dependencies(file: DependencyTomlFile) -> list[Declaration]:
    """Return every dependency the file declares, each parsed once and located and marked at the line declaring it.

    Only the dependency arrays are read, so a string spelled like a spec elsewhere in the file is not one. A spec
    another declaration spells identically, and one TOML spells with an escape, are each located at their own line
    too. A spec that does not parse is left out, without shifting the positions of the declarations after it.
    """
    contents = file.read()
    sourced = _uv_source_names(_config(file.toml(contents)))
    specs: dict[_DeclarationPosition, str] = {}

    def place(position: _DeclarationPosition, spec: str) -> str:
        specs[position] = spec
        return _placeholder(position)

    placed = _replaced_specs(file, contents, place)
    if placed is None:
        return []
    requirements = {
        position: requirement for position, spec in specs.items() if (requirement := _requirement(spec)) is not None
    }
    lines = located_lines(file.path, file.toml(placed).split("\n"))
    locations = {
        position: Location(file.path, line_number(placed, placed.index(_placeholder(position))))
        for position in requirements
    }
    return [
        Declaration(
            requirement.name,
            _pinned_version(requirement),
            locations[position],
            uv_sourced=normalized_python_name(requirement.name) in sourced,
            direct_url=bool(requirement.url),
            position=position,
            marker=_marker(lines, position, locations[position]),
        )
        for position, requirement in requirements.items()
    ]


def _marker(lines: list[Line], position: _DeclarationPosition, location: Location) -> Marker:
    """Return the marker steering the declaration at the position, read from the line holding its placeholder.

    The lines are the TOML's rather than the file's, so a marker inside a `# /// script` block is read without the
    `#` that comments the block out. They are located from the TOML's own start, so the declaration's location
    replaces the one its line was given.
    """
    line = next(line for line in lines if _placeholder(position) in line.text)
    return parse_marker(replace(line, location=location))


def _requirement(spec: str) -> Requirement | None:
    """Return the requirement the spec declares, or None when the spec does not parse as one.

    A spec with a typo in it — `pkg=1.0`, say — reads back as None, which keeps one bad declaration from aborting
    the run.
    """
    try:
        return Requirement(spec)
    except InvalidRequirement:
        return None


def _pinned_version(requirement: Requirement) -> VersionString:
    """Return the version the requirement pins exactly, or the empty string when it pins none.

    A requirement pins one when its only specifier is an `==` naming a version rather than a wildcard, whatever the
    declaration spells around it: an extra, an environment marker, and spaces are all part of the declaration rather
    than of the version. Arbitrary equality (`===`) names a string the source need not resolve to a version, so it
    pins none either.
    """
    specifiers = list(requirement.specifier)
    if len(specifiers) == 1 and specifiers[0].operator == "==" and is_valid(specifiers[0].version):
        return specifiers[0].version
    return ""


def _rewritten_spec(spec: str, declaration: Declaration, new_version: VersionString) -> str:
    """Return the spec with the version the declaration pins replaced by the new one.

    A declaration pinning no exact version has none to replace, so its spec comes back as it was. Only the version
    is replaced, so whatever the declaration spells around it stays as it is.
    """
    if not (current := declaration.current_version):
        return spec
    return re.sub(rf"(==\s*){re.escape(current)}", lambda match: match[1] + new_version, spec, count=1)

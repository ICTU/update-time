"""Read and rewrite the dependencies a file declares in TOML: a pyproject.toml, or a PEP 723 script block.

The dependency TOML sits in the whole file or in the script's block, which the file's class says, so reading and
rewriting the dependencies works alike for both kinds of file. Formatting, comments, and quoting are preserved.
"""

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet

from update_time.domain.dependency import is_valid, normalized_python_name
from update_time.domain.line import Line, located_lines
from update_time.formats import toml
from update_time.markers.directive import Reason
from update_time.markers.marker import Marker, Scope, parse_marker
from update_time.markers.reference import SteeredReference
from update_time.primitives.location import Location
from update_time.primitives.text import line_number

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, MutableSequence
    from pathlib import Path

    from update_time.domain.dependency import DependencyName, VersionString


type _DeclarationPosition = int


# The manifests read these tables, each named by the keys that lead to it. Each follows the table that holds it, so
# that table is known to be a table by the time the one inside it is read.
_TABLES = (
    ("project",),
    ("project", "optional-dependencies"),
    ("dependency-groups",),
    ("tool",),
    ("tool", "uv"),
    ("tool", "uv", "workspace"),
    ("build-system",),
)


def _placeholder(position: _DeclarationPosition) -> str:
    """Return the text put in a spec's place to find the line declaring it, distinct from every other spec's.

    The position is wrapped rather than appended, so one placeholder is never the start of another.
    """
    return f"update-time-spec-{position}-update-time-spec"


def parsed_config(toml_text: str) -> dict | None:
    """Return the TOML parsed, or None when it isn't valid TOML or a table the manifests read is not a table."""
    if (config := toml.parse(toml_text)) is None or not is_well_formed(config):
        return None
    return config


def is_well_formed(config: Mapping) -> bool:
    """Return whether each table the manifests read is a table, and each dependency array an array, where declared."""
    if not all(_is_table_or_absent(config, keys) for keys in _TABLES):
        return False
    return all(isinstance(array, list) for array in _dependency_arrays(config))


def _is_table_or_absent(config: Mapping, keys: tuple[str, ...]) -> bool:
    """Return whether the keys lead to a table, or to nothing the config declares."""
    *parents, key = keys
    table = config
    for parent in parents:
        table = table.get(parent, {})
    return isinstance(table.get(key, {}), dict)


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
    """Return every array of dependency specs the config declares: the ones uv resolves, and the build requirements."""
    return [*uv_dependency_arrays(config), config.get("build-system", {}).get("requires", [])]


def uv_dependency_arrays(config: Mapping) -> list[MutableSequence]:
    """Return every array of dependency specs that uv resolves.

    A pyproject.toml declares them in the project's own array, in an array per extra, in an array per dependency
    group, and in uv's legacy `[tool.uv] dev-dependencies`. uv still resolves that legacy array, so it is read like
    the rest. An inline script metadata block declares them in one array of its own.
    """
    project = config.get("project", {})
    return [
        project.get("dependencies", []),
        *project.get("optional-dependencies", {}).values(),
        *config.get("dependency-groups", {}).values(),
        _uv_table(config).get("dev-dependencies", []),
        config.get("dependencies", []),
    ]


@dataclass(frozen=True, kw_only=True)
class Declaration(SteeredReference):
    """A dependency a file declares, and where the file says its release comes from.

    `uv_sourced` says the file names this dependency in its `[tool.uv] sources` table, so uv resolves it from a
    path, a workspace member, a git repository, or an index of its own. `direct_url` says the declaration points
    at a URL or a git repository rather than at a release. `build_requirement` says the declaration sits in the
    `[build-system] requires` array, which uv does not lock. `environment_marker` is the condition after the `;`,
    such as `python_version < "3.13"`, or the empty string where the declaration has none.
    `position` says which spec of the file this declaration is, counting from one.
    """

    uv_sourced: bool
    direct_url: bool
    build_requirement: bool
    environment_marker: str
    position: _DeclarationPosition

    @property
    def pins_a_version(self) -> bool:
        """Return whether the declaration pins an exact version, so a check about that version has one to run on."""
        return bool(self.current_version)

    @property
    def names_a_release(self) -> bool:
        """Return whether the declaration points at a release of the package it names."""
        return not (self.direct_url or self.uv_sourced)

    @property
    def narrowable(self) -> bool:
        """Return whether Update-time narrows the requirement uv locks for the declaration by its marker's bound."""
        return self.unnarrowable_reason is None

    @property
    def unnarrowable_reason(self) -> Reason | None:
        """Return why Update-time does not narrow the requirement uv locks for the declaration, if it does not."""
        if self.build_requirement:
            return Reason.BUILD_REQUIREMENT_NOT_LOCKED
        if self.direct_url:
            return Reason.URL_RATHER_THAN_VERSION
        if not self.pins_a_version:
            return None
        if self.environment_marker:
            return Reason.ENVIRONMENT_MARKER_PIN_KEPT
        return None if self.updatable else Reason.BOUND_ON_A_HELD_BACK_UPDATE

    @property
    def updatable(self) -> bool:
        """Return whether the declaration's own marker leaves the version it pins to be updated."""
        return not self.marker.ignores(Scope.UPDATE)


@dataclass(frozen=True)
class PythonDependencyToml:
    """A file that declares its Python dependencies in TOML.

    The contents are read per operation rather than held, so a file the run has rewritten reads back as it now is.
    """

    path: Path

    def read(self) -> str:
        """Return the file's contents."""
        return self.path.read_text()

    def write(self, contents: str) -> None:
        """Write the contents to the file."""
        self.path.write_text(contents)

    def write_if_changed(self, contents: str) -> None:
        """Write the contents to the file, where it holds other contents."""
        if self.read() != contents:
            self.write(contents)

    def toml(self, contents: str) -> str:
        """Return the TOML the contents declare the dependencies in."""
        raise NotImplementedError

    def with_toml(self, contents: str, /, toml_text: str) -> str:
        """Return the contents with the TOML declaring the dependencies replaced."""
        raise NotImplementedError

    def declarations(self) -> list[Declaration]:
        """Return every dependency the file declares, each parsed once and located and marked at the line declaring it.

        Only the dependency arrays are read, so a string spelled like a spec elsewhere in the file is not one. A spec
        another declaration spells identically, and one TOML spells with an escape, are each located at their own line
        too. A spec that does not parse is left out, without shifting the positions of the declarations after it.
        """
        contents = self.read()
        config = parsed_config(self.toml(contents)) or {}
        sourced = _uv_source_names(config)
        # The arrays uv resolves come before the build requirements, so the specs in them take the first positions.
        uv_resolved = sum(isinstance(spec, str) for array in uv_dependency_arrays(config) for spec in array)
        specs: dict[_DeclarationPosition, str] = {}

        def place(position: _DeclarationPosition, spec: str) -> str:
            specs[position] = spec
            return _placeholder(position)

        placed = self._replaced_specs(contents, place)
        if placed is None:
            return []
        requirements = {
            position: requirement for position, spec in specs.items() if (requirement := _requirement(spec)) is not None
        }
        lines = located_lines(self.path, self.toml(placed).split("\n"))
        locations = {
            position: Location(self.path, line_number(placed, placed.index(_placeholder(position))))
            for position in requirements
        }
        return [
            Declaration(
                requirement.name,
                _pinned_version(requirement),
                locations[position],
                uv_sourced=normalized_python_name(requirement.name) in sourced,
                direct_url=bool(requirement.url),
                build_requirement=position > uv_resolved,
                environment_marker=str(requirement.marker or ""),
                position=position,
                marker=_marker(lines, position, locations[position]),
            )
            for position, requirement in requirements.items()
        ]

    def with_pinned_versions(self, contents: str, versions: Mapping[Declaration, VersionString]) -> str:
        """Return the contents with the exact pin of each declaration rewritten to the version `versions` holds for it.

        One declaration of a name is rewritten while another declaration of that name keeps its pinned version. Each
        declaration is one that `declarations` read from the contents.
        """
        by_position = {declaration.position: declaration for declaration in versions}

        def new_spec(position: _DeclarationPosition, spec: str) -> str | None:
            if (declaration := by_position.get(position)) is None:
                return None
            rewritten = _rewritten_spec(spec, declaration, versions[declaration])
            return rewritten if rewritten != spec else None

        return self._replaced_specs(contents, new_spec) or contents

    def with_locked_requirements(self, contents: str, bounds: Mapping[Declaration, str]) -> str:
        """Return the contents with the requirement of each declaration narrowed by the specifier `bounds` holds for it.

        An exact pin is relaxed from `==version` to `>=version` first. The name, the extras, and the environment marker
        stay, spelled the way `packaging` spells a requirement.
        """
        by_position = {declaration.position: declaration for declaration in bounds}

        def new_spec(position: _DeclarationPosition, spec: str) -> str | None:
            if (declaration := by_position.get(position)) is None:
                return None
            requirement = Requirement(spec)
            if current_version := declaration.current_version:
                requirement.specifier = SpecifierSet(f">={current_version}")
            requirement.specifier &= SpecifierSet(bounds[declaration])
            return str(requirement)

        return self._replaced_specs(contents, new_spec) or contents

    def _replaced_specs(self, contents: str, replace: Callable[[_DeclarationPosition, str], str | None]) -> str | None:
        """Return the file's text with each declared spec `replace` gives a replacement for, or None when it gives none.

        Every call walks the arrays in the same order, so a spec has the same position in each of them.

        Only the specs in the dependency arrays change. A string spelled like a spec elsewhere in the file, and the
        comments, quoting, and whitespace around the specs, come back as they were. A file that is not valid TOML
        comes back as None as well, so one malformed file does not abort the run.
        """
        if (document := toml.parse_document(self.toml(contents))) is None:
            return None
        replaced = False
        position = 0
        for array in _dependency_arrays(document):
            for index, spec in enumerate(array):
                if not toml.is_string(spec):
                    continue
                position += 1
                if (new_spec := replace(position, spec)) is not None:
                    array[index] = toml.string(new_spec, quoted_as=spec)
                    replaced = True
        return self.with_toml(contents, toml.dumps(document)) if replaced else None


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
    """Return the spec with the version the declaration pins exactly replaced by the new version.

    A declaration pinning no exact version has none to replace, so its spec comes back as it was. Only the version is
    replaced, so whatever the declaration spells around it stays as it is.
    """
    if not (current := declaration.current_version):
        return spec
    return re.sub(rf"(==\s*){re.escape(current)}", lambda match: match[1] + new_version, spec, count=1)

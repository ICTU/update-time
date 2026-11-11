"""The config of a pyproject.toml or of PEP 723 inline script metadata, and the parse that checks its shape."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from update_time.domain.dependency import normalized_python_name
from update_time.formats import toml

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from update_time.domain.dependency import DependencyName


type _Keys = tuple[str, ...]


def _is_table(value: object) -> bool:
    """Return whether the value is a table."""
    return isinstance(value, dict)


def _is_array_of_strings(value: object) -> bool:
    """Return whether the value is an array holding strings alone."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


# The config is read from these tables and arrays of strings, each named by the keys that lead to it and paired with
# the check its value has to pass. Each follows the table that holds it, so that table is known to be a table by the
# time the value inside it is checked.
_SHAPE: tuple[tuple[_Keys, Callable[[object], bool]], ...] = (
    (("project",), _is_table),
    (("project", "optional-dependencies"), _is_table),
    (("project", "dynamic"), _is_array_of_strings),
    (("dependency-groups",), _is_table),
    (("tool",), _is_table),
    (("tool", "uv"), _is_table),
    (("tool", "uv", "sources"), _is_table),
    (("tool", "uv", "workspace"), _is_table),
    (("tool", "uv", "workspace", "members"), _is_array_of_strings),
    (("tool", "uv", "workspace", "exclude"), _is_array_of_strings),
    (("build-system",), _is_table),
)

# The `[project]` fields that a project can list as `dynamic`, leaving the build backend to fill in the dependencies
# that uv then resolves.
_DYNAMIC_DEPENDENCY_FIELDS = ("dependencies", "optional-dependencies")


@dataclass(frozen=True)
class _DependencyArray:
    """An array of dependency specs, and the keys that lead to it. An item that is not a string is not a spec."""

    keys: _Keys
    items: tuple[object, ...]

    @property
    def specs(self) -> list[str]:
        """Return the specs in the array."""
        return [item for item in self.items if isinstance(item, str)]


@dataclass(frozen=True)
class UvWorkspace:
    """A `[tool.uv.workspace]` table, which names the globs of the projects it includes and of the ones it excludes."""

    members: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class _UvTable:
    """A `[tool.uv]` table: whether uv manages the project, the dependencies it sources, and the workspace it roots.

    The sources are the names of the dependencies uv resolves from a source of its own, each normalised, since uv
    matches a `sources` key to a dependency by the normalised name.
    """

    managed: bool = True
    sources: frozenset[DependencyName] = frozenset()
    workspace: UvWorkspace | None = None


@dataclass(frozen=True)
class PyprojectConfig:
    """The config of a pyproject.toml or of inline script metadata.

    `uv_dependency_arrays` are the arrays of dependency specs that uv resolves, in the order the config is read in.
    `build_requirements` holds the `[build-system] requires` array, where the config declares one. `tools` names the
    tables under `[tool]`.
    """

    uv_dependency_arrays: tuple[_DependencyArray, ...] = ()
    build_requirements: tuple[_DependencyArray, ...] = ()
    requires_python: str | None = None
    dynamic: tuple[str, ...] = ()
    tools: frozenset[str] = frozenset()
    uv: _UvTable = field(default_factory=_UvTable)

    @property
    def dependency_arrays(self) -> tuple[_DependencyArray, ...]:
        """Return every array of dependency specs: the ones uv resolves, and then the build requirements."""
        return (*self.uv_dependency_arrays, *self.build_requirements)

    @property
    def declares_a_dependency(self) -> bool:
        """Return whether the config declares a dependency uv resolves, in an array or through the build backend."""
        in_an_array = any(array.items for array in self.uv_dependency_arrays)
        return in_an_array or any(name in self.dynamic for name in _DYNAMIC_DEPENDENCY_FIELDS)


def read(path: Path) -> PyprojectConfig | None:
    """Return the file's config, or None when it can't be read, isn't UTF-8, isn't valid TOML, or isn't well formed."""
    table = toml.read(path)
    return None if table is None else from_table(table)


def parse(toml_text: str) -> PyprojectConfig | None:
    """Return the config the TOML declares, or None when it isn't valid TOML or isn't well formed."""
    table = toml.parse(toml_text)
    return None if table is None else from_table(table)


def from_table(table: Mapping) -> PyprojectConfig | None:
    """Return the config the parsed TOML declares, or None when a table or an array it reads is of another type."""
    if not _is_well_formed(table):
        return None
    project = table.get("project", {})
    uv = table.get("tool", {}).get("uv", {})
    workspace = uv.get("workspace")
    return PyprojectConfig(
        uv_dependency_arrays=tuple(_DependencyArray(keys, tuple(array)) for keys, array in _uv_arrays(table)),
        build_requirements=tuple(_DependencyArray(keys, tuple(array)) for keys, array in _build_requirements(table)),
        # A value that is not a string, such as the number 3.10, is turned into one, and is not a specifier either.
        requires_python=None if (specifier := project.get("requires-python")) is None else str(specifier),
        dynamic=tuple(project.get("dynamic", [])),
        tools=frozenset(table.get("tool", {})),
        uv=_UvTable(
            managed=uv.get("managed") is not False,
            sources=frozenset(normalized_python_name(name) for name in uv.get("sources", {})),
            workspace=None
            if workspace is None
            else UvWorkspace(tuple(workspace.get("members", [])), tuple(workspace.get("exclude", []))),
        ),
    )


def _is_well_formed(table: Mapping) -> bool:
    """Return whether each table and each array the config is read from holds a value of its type, where declared."""
    if not all(is_valid(value) for keys, is_valid in _SHAPE for _, value in _declared(table, keys)):
        return False
    return all(isinstance(array, list) for _, array in [*_uv_arrays(table), *_build_requirements(table)])


def _uv_arrays(table: Mapping) -> list[tuple[_Keys, Any]]:
    """Return each array of dependency specs that uv resolves, with the keys leading to it.

    A pyproject.toml declares them in the project's own array, in an array per extra, in an array per dependency
    group, and in uv's legacy `[tool.uv] dev-dependencies`. uv still resolves that legacy array, so it is read like
    the rest. Inline script metadata declares them in one array of its own.
    """
    return [
        *_declared(table, ("project", "dependencies")),
        *_declared_per_key(table, ("project", "optional-dependencies")),
        *_declared_per_key(table, ("dependency-groups",)),
        *_declared(table, ("tool", "uv", "dev-dependencies")),
        *_declared(table, ("dependencies",)),
    ]


def _build_requirements(table: Mapping) -> list[tuple[_Keys, Any]]:
    """Return the `[build-system] requires` array, with the keys leading to it, or nothing where it is not declared."""
    return _declared(table, ("build-system", "requires"))


def _declared(table: Mapping, keys: _Keys) -> list[tuple[_Keys, Any]]:
    """Return the value the keys lead to, with the keys, or nothing where the table does not declare it."""
    *parents, key = keys
    for parent in parents:
        table = table.get(parent, {})
    return [(keys, table[key])] if key in table else []


def _declared_per_key(table: Mapping, keys: _Keys) -> list[tuple[_Keys, Any]]:
    """Return each value of the table the keys name, with the keys of that value."""
    return [((*keys, key), value) for _, values in _declared(table, keys) for key, value in values.items()]

"""Read, edit, and rewrite pyproject.toml files, preserving their formatting.

An inline script metadata block declares its dependencies as the same TOML, so reading and rewriting those go
through here too. The `DependencyTomlFile` the caller hands over says where in the file that TOML sits, so
nothing here knows which of the two kinds of file it is looking at.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import tomlkit
import tomlkit.items
from packaging.requirements import InvalidRequirement, Requirement

from update_time.domain.dependency import is_valid
from update_time.domain.reference import Reference
from update_time.file_formats import toml
from update_time.primitives.location import Location
from update_time.primitives.text import line_number

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, MutableSequence
    from pathlib import Path

    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.file_formats.dependency_file import DependencyTomlFile


def _marker(number: int) -> str:
    """Return the text put in a spec's place to find the line declaring it, distinct from every other spec's.

    The number is wrapped rather than appended, so one marker is never the start of another.
    """
    return f"update-time-spec-{number}-update-time-spec"


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


def rewrite_pinned_versions(file: DependencyTomlFile, versions: dict[DependencyName, VersionString]) -> None:
    """Rewrite each declared pin to the version `versions` holds for it; write the file if changed.

    The mapping is keyed by the name exactly as this file spells that name, so a name the mapping does not
    hold keeps its pinned version.
    """

    def new_version(spec: str) -> str | None:
        return rewritten if (rewritten := _rewritten_spec(spec, versions)) != spec else None

    if (rewritten_contents := _respelled_specs(file, file.read(), new_version)) is not None:
        file.write(rewritten_contents)


def _respelled_specs(file: DependencyTomlFile, contents: str, respell: Callable[[str], str | None]) -> str | None:
    """Return the file with each declared spec `respell` gives a new spelling for, or None when it gives none.

    The file comes back in its own layout. tomlkit rewrites the arrays it parsed, so a string spelled like a spec
    elsewhere in the file is out of reach, and every line holds what it held. The comments, quoting, and whitespace
    around the specs come back untouched. A file that is not valid TOML reads back as None, so one malformed file
    does not abort the run.
    """
    if (document := toml.parse_document(file.toml(contents))) is None:
        return None
    respelled = False
    for array in _dependency_arrays(document):
        for index, spec in enumerate(array):
            if isinstance(spec, tomlkit.items.String) and (new_spec := respell(spec)) is not None:
                array[index] = toml.string(new_spec, quoted_as=spec)
                respelled = True
    return file.with_toml(contents, tomlkit.dumps(document)) if respelled else None


def _config(toml_text: str) -> dict:
    """Return the TOML parsed, or an empty config when it isn't valid TOML."""
    return toml.parse(toml_text) or {}


def _uv_table(config: Mapping) -> Mapping:
    """Return the config's `[tool.uv]` table, or an empty table when it has none."""
    return config.get("tool", {}).get("uv", {})


def _uv_source_names(config: dict) -> set[DependencyName]:
    """Return the names of the dependencies uv resolves from a source of its own."""
    return set(_uv_table(config).get("sources", {}))


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
class Declaration(Reference):
    """A dependency a file declares, and where the file says its release comes from.

    `uv_sourced` says the file names this dependency in its `[tool.uv] sources` table, so uv resolves it from a
    path, a workspace member, a git repository, or an index of its own. `direct_url` says the declaration points
    at a URL or a git repository rather than at a release. What follows from either is the caller's.
    """

    uv_sourced: bool
    direct_url: bool


def declared_dependencies(file: DependencyTomlFile) -> list[Declaration]:
    """Return every dependency the file declares, each parsed once and located at the line declaring it.

    Each spec is marked in a copy of the file, which comes back in the file's own layout, so the line a marker
    lands on is the line declaring the spec it replaced. Nothing is searched for in the file's text, so a string
    spelled like a spec elsewhere never takes its line, and a spec TOML spells with an escape is located like the
    rest. A spec declared twice keeps a marker per declaration, and so a line of its own for each.
    """
    contents = file.read()
    sourced = _uv_source_names(_config(file.toml(contents)))
    specs: list[str] = []

    def mark(spec: str) -> str:
        specs.append(spec)
        return _marker(len(specs))

    marked = _respelled_specs(file, contents, mark)
    if marked is None:
        return []
    located = ((spec, _marker(number)) for number, spec in enumerate(specs, start=1))
    return [
        Declaration(
            requirement.name,
            _pinned_version(requirement),
            Location(file.path, line_number(marked, marked.index(m))),
            uv_sourced=requirement.name in sourced,
            direct_url=bool(requirement.url),
        )
        for spec, m in located
        if (requirement := _requirement(spec)) is not None
    ]


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


def _rewritten_spec(spec: str, versions: dict[DependencyName, VersionString]) -> str:
    """Return the spec with its pinned version replaced by the one `versions` holds for its name.

    A spec pinning no version, and one whose name the mapping does not hold, come back as they were. Only the
    version is replaced, so whatever the declaration spells around it stays as it is.
    """
    requirement = _requirement(spec)
    if requirement is None or not (current := _pinned_version(requirement)):
        return spec
    new_version = versions.get(requirement.name, current)
    return re.sub(rf"(==\s*){re.escape(current)}", lambda match: match[1] + new_version, spec, count=1)

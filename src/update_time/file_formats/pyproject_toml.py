"""Read, edit, and rewrite pyproject.toml files, preserving their formatting.

This module owns everything that knows the pyproject.toml *format*: parsing it with the standard library's tomllib,
and reading or editing individual `[tool.<name>]` keys and pinned dependency versions with tomlkit, which keeps the
rest of the file — comments, ordering, and whitespace — untouched. What those edits *mean* (which cooldown to
write, which versions to bump to) is the caller's concern, not this module's.

An inline script metadata block declares its dependencies as the same TOML, so reading those goes through here
too, once `inline_script_metadata` has uncommented the block.
"""

import re
import tomllib
from typing import TYPE_CHECKING

import tomlkit
from packaging.requirements import InvalidRequirement, Requirement

from update_time.domain.reference import Reference
from update_time.file_formats import inline_script_metadata
from update_time.primitives.location import Location
from update_time.primitives.text import line_number

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.dependency import DependencyName, VersionString

# A pinned dependency spec as it appears in a dependencies array, e.g. `"package==1.0"`, capturing the distribution
# name and the pinned version. Only `==` pins are matched; specs with other clauses (`<=`, `~=`, …) are left alone.
_PINNED_SPEC = re.compile(r'"(?P<name>[A-Za-z0-9_.\-]+)==(?P<version>[A-Za-z0-9_.\-]+)"')


def _parse(text: str) -> dict | None:
    """Return the TOML parsed into a dict, or None when the text isn't valid TOML."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def read(path: Path) -> dict | None:
    """Return the parsed pyproject.toml, or None when the file can't be read or isn't valid TOML."""
    try:
        text = path.read_text()
    except OSError:
        return None
    return _parse(text)


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
    """Set `[tool.<table>] <key> = value` (with an optional trailing comment) and write the file back.

    The `[tool]` and `[tool.<table>]` tables are created when absent; everything else in the file — other keys,
    comments, ordering, and whitespace — is preserved.
    """
    document = tomlkit.parse(path.read_text())
    tool = document.setdefault("tool", tomlkit.table(is_super_table=True))
    if table not in tool:
        tool[table] = tomlkit.table()
    item = tomlkit.item(value)
    if comment:
        item.comment(comment)
    tool[table][key] = item
    path.write_text(tomlkit.dumps(document))


def rewrite_pinned_versions(path: Path, versions: dict[DependencyName, VersionString]) -> None:
    """Rewrite each `"name==<old>"` pin to the version `versions` holds for it; write the file if changed.

    The mapping is keyed by the name exactly as this file spells it. A name absent from it keeps its pinned version,
    so only dependencies with a known newer version are touched, and only the captured spec is rewritten — the rest
    of the file is left exactly as it was.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        version = versions.get(name, match.group("version"))
        return f'"{name}=={version}"'

    current = path.read_text()
    rewritten = _PINNED_SPEC.sub(replace, current)
    if rewritten != current:
        path.write_text(rewritten)


def _config(contents: str) -> dict:
    """Return the TOML the file declares its dependencies in, parsed, or an empty config when it declares none.

    A script declares them in its inline script metadata block, which comments its TOML out; every other file is
    TOML throughout. Contents that are not valid TOML — a script whose block is malformed, say — declare nothing
    that can be read.
    """
    toml_text = inline_script_metadata.toml_block(contents)
    return _parse(contents if toml_text is None else toml_text) or {}


def _uv_table(config: dict) -> dict:
    """Return the config's `[tool.uv]` table, or an empty table when it has none."""
    return config.get("tool", {}).get("uv", {})


def _uv_source_names(config: dict) -> set[DependencyName]:
    """Return the names of the dependencies uv resolves from a source of its own.

    A `[tool.uv.sources]` entry points a dependency at a path, a workspace member, a git repository, or an index of
    its own, so PyPI holds no release of it to report on.
    """
    return set(_uv_table(config).get("sources", {}))


def _declared_specs(config: dict) -> list[str]:
    """Return the requirement spec of every dependency the config's dependency arrays declare.

    A pyproject.toml declares them in the project's own array, in an array per extra, in an array per dependency
    group, in uv's legacy `[tool.uv] dev-dependencies`, and in the `[build-system]` requirements. uv still resolves
    that legacy array, so it is read like the rest. An inline script metadata block declares them in one array of
    its own. A group's `include-group` entry names another group rather than a dependency, so only the strings are
    returned.
    """
    project = config.get("project", {})
    arrays = [
        project.get("dependencies", []),
        *project.get("optional-dependencies", {}).values(),
        *config.get("dependency-groups", {}).values(),
        _uv_table(config).get("dev-dependencies", []),
        config.get("build-system", {}).get("requires", []),
        config.get("dependencies", []),
    ]
    return [spec for array in arrays for spec in array if isinstance(spec, str)]


def _spec_location(path: Path, contents: str, spec: str) -> Location:
    """Return where the file declares the spec, or the file alone when the declaration cannot be found.

    The spec is searched for as the file spells it, quotes and all, so a spec that is another's tail — `rich` in
    `rich-click` — is never taken for it.
    """
    for quote in ('"', "'"):
        if (index := contents.find(f"{quote}{spec}{quote}")) != -1:
            return Location(path, line_number(contents, index))
    return Location(path)


def _registry_requirement(spec: str, sourced: set[DependencyName]) -> Requirement | None:
    """Return the requirement the spec declares, or None when it names no PyPI release to look up.

    A spec with a typo in it — `pkg=1.0`, say — reads back as None, which keeps one bad declaration from aborting
    the run. A spec that points at a URL reads back as None too, as does one whose name uv resolves from a source
    of its own, since PyPI serves a release for neither.
    """
    try:
        requirement = Requirement(spec)
    except InvalidRequirement:
        return None
    return None if requirement.url or requirement.name in sourced else requirement


def _loose_dependencies(path: Path) -> list[Reference]:
    """Return every dependency the file declares without an exact pin, with where it sits.

    A dependency declared as `humanize>=4`, or with no specifier at all, pins no version, so the reference names
    none either. A spec that names no PyPI release to look up is left out: one that points at a URL, one uv
    resolves from a source of its own, and one that does not parse at all. The exact `==` pins are
    `pinned_versions`' to return.
    """
    contents = path.read_text()
    config = _config(contents)
    sourced = _uv_source_names(config)
    return [
        Reference(requirement.name, "", _spec_location(path, contents, spec))
        for spec in _declared_specs(config)
        if "==" not in spec and (requirement := _registry_requirement(spec, sourced)) is not None
    ]


def declared_dependencies(path: Path) -> list[Reference]:
    """Return every dependency the file declares, the exact pins first and the rest after them.

    A pin carries the version it names, a looser declaration carries none, so the two are told apart by whether the
    reference names a version.
    """
    return pinned_versions(path) + _loose_dependencies(path)


def pinned_versions(path: Path) -> list[Reference]:
    """Return every exact `name==version` pin in the file, with where it sits.

    Matches the same `==` specs `rewrite_pinned_versions` rewrites (looser specifiers like `<=` are excluded),
    across every dependency array. A name pinned in more than one array is returned once per pin, so a pin is
    never hidden by another pin of the same name.
    """
    contents = path.read_text()
    return [
        Reference(match["name"], match["version"], Location(path, line_number(contents, match.start())))
        for match in _PINNED_SPEC.finditer(contents)
    ]

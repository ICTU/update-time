"""Read, edit, and rewrite pyproject.toml files, preserving their formatting.

This module owns everything that knows the pyproject.toml *format*: parsing it (via the standard library's tomllib)
and reading or editing individual `[tool.<name>]` keys and pinned dependency versions while keeping the rest of the
file — comments, ordering, and whitespace — untouched (via tomlkit). What those edits *mean* (which cooldown to
write, which versions to bump to) is the caller's concern, not this module's.
"""

import re
import tomllib
from typing import TYPE_CHECKING

import tomlkit

if TYPE_CHECKING:
    from pathlib import Path

# A pinned dependency spec as it appears in a dependencies array, e.g. `"package==1.0"`, capturing the distribution
# name and the pinned version. Only `==` pins are matched; specs with other clauses (`<=`, `~=`, …) are left alone.
_PINNED_SPEC = re.compile(r'"(?P<name>[A-Za-z0-9_.\-]+)==(?P<version>[A-Za-z0-9_.\-]+)"')


def read(path: Path) -> dict | None:
    """Return the parsed pyproject.toml, or None if it can't be read or isn't valid TOML.

    Returning None (rather than raising) lets callers probe files that may not exist — e.g. walking up the tree for a
    workspace root — or tolerate an unrelated, malformed pyproject.toml without aborting the run.
    """
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


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


def rewrite_pinned_versions(path: Path, versions: dict[str, str]) -> None:
    """Rewrite each `"name==<old>"` pin to `versions[name]` (name matched case-insensitively); write if changed.

    Names absent from the mapping keep their pinned version, so only dependencies with a known newer version are
    touched, and only the captured spec is rewritten — the rest of the file is left exactly as it was.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        version = versions.get(name.lower(), match.group("version"))
        return f'"{name}=={version}"'

    current = path.read_text()
    rewritten = _PINNED_SPEC.sub(replace, current)
    if rewritten != current:
        path.write_text(rewritten)

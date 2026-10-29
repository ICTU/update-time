"""Read a JSON file, and find where a JSON document declares an entry.

Note: `import json` below resolves to the standard library's module, not this one — imports are absolute.
"""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# What reads one JSON value and reports where it ends, and the characters JSON ignores between two values.
_DECODER = json.JSONDecoder()
_WHITESPACE = " \t\n\r"

# Why a file holding valid JSON other than an object, such as `[]`, cannot be read as one.
_NOT_AN_OBJECT = "the JSON is not an object"


@dataclass(frozen=True)
class JsonFile:
    """A JSON file read once: where it sits, its text, and the contents parsed from that text.

    Finding an entry reads the text and reading a value reads the parsed contents, so one read answers both.
    """

    path: Path
    text: str
    contents: dict

    @classmethod
    def read(cls, path: Path) -> Self:
        """Return the JSON file at the path, read and parsed, raising a `ValueError` where the JSON is not an object."""
        text = path.read_text()
        if not isinstance(contents := json.loads(text), dict):
            raise ValueError(_NOT_AN_OBJECT)  # noqa: TRY004 — the error JSON that does not parse raises too
        return cls(path, text, contents)


def entry_offset(contents: str, *keys: str) -> int | None:
    """Return the offset of the entry that the keys name, one level per key, or None where the document lacks it.

    Each key names a member of the object the key before it leads to, the first one a member of the document
    itself. The document's structure decides which entry that is, so a key of the same name at another depth or in
    another object is passed over, and so is a value that spells the name.
    """
    offset = None
    value_offset = _skip_whitespace(contents, 0)
    for key in keys:
        members = _members(contents, value_offset)
        found = next(((name_offset, value) for member, name_offset, value in members if member == key), None)
        if found is None:
            return None
        offset, value_offset = found
    return offset


def _members(contents: str, start: int) -> Iterator[tuple[str, int, int]]:
    """Yield each member of the JSON object opening at `start`: its name, and where the name and its value sit.

    A `start` that opens no object yields nothing, so a section declared as anything but an object holds no entry.
    The decoder reads each name and each value, so a name spelled with an escape is the name it spells, and a
    nested object is stepped over whole rather than descended into.
    """
    if start >= len(contents) or contents[start] != "{":
        return
    index = _skip_whitespace(contents, start + 1)
    while index < len(contents) and contents[index] == '"':
        name_offset = index
        name, index = _DECODER.raw_decode(contents, index)
        colon = _skip_whitespace(contents, index)
        value_offset = _skip_whitespace(contents, colon + 1)
        yield name, name_offset, value_offset
        _value, index = _DECODER.raw_decode(contents, value_offset)
        index = _skip_whitespace(contents, index)
        if index < len(contents) and contents[index] == ",":
            index = _skip_whitespace(contents, index + 1)


def _skip_whitespace(contents: str, index: int) -> int:
    """Return the index of the first character at or after `index` that JSON does not ignore."""
    while index < len(contents) and contents[index] in _WHITESPACE:
        index += 1
    return index

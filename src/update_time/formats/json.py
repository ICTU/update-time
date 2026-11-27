"""Find where a JSON document declares an entry.

Note: `import json` below resolves to the standard library's module, not this one — imports are absolute.
"""

import json
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

# What reads one JSON value and reports where it ends, and the characters JSON ignores between two values.
_DECODER: Final[json.JSONDecoder] = json.JSONDecoder()
# The name of the format, as a message about a document that does not parse names it.
FORMAT: Final[str] = "JSON"

# Any value of a JSON document: an object, an array, a string, a number, a boolean, or null.
type JSON = Mapping[str, JSON] | Sequence[JSON] | str | int | float | bool | None


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


def _skip_whitespace(contents: str, index: int) -> int:
    """Return the index of the first character at or after `index` that JSON does not ignore."""
    whitespace: str = " \t\n\r"
    while index < len(contents) and contents[index] in whitespace:
        index += 1
    return index


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
        value_offset = _skip_whitespace(contents, _skip_whitespace(contents, index) + 1)
        yield name, name_offset, value_offset
        _value, index = _DECODER.raw_decode(contents, value_offset)
        index = _skip_whitespace(contents, index)
        if index < len(contents) and contents[index] == ",":
            index = _skip_whitespace(contents, index + 1)

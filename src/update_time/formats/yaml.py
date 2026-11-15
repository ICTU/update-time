"""Read YAML files into documents to look values up in.

Note: `import yaml` below resolves to the third-party PyYAML package, not this module — imports are absolute.
"""

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# What this format is called in a message about a file that does not parse.
FORMAT = "YAML"


@dataclass(frozen=True)
class Document:
    """A parsed YAML document, or a node in one: a mapping, a sequence, a scalar, or None for an empty file."""

    _value: object = None

    def __getitem__(self, key: str) -> Document:
        """Return the node under the key, or an empty node where this node is not a mapping holding the key."""
        return Document(self._value.get(key)) if isinstance(self._value, dict) else Document()

    def __contains__(self, key: str) -> bool:
        """Return whether this node is a mapping holding the key."""
        return isinstance(self._value, dict) and key in self._value

    def string(self) -> str | None:
        """Return this node's value where it is a string."""
        return self._value if isinstance(self._value, str) else None

    def __iter__(self) -> Iterator[str]:
        """Return an iterator over the keys of this node where it is a mapping."""
        return iter(self._value if isinstance(self._value, dict) else [])

    def values(self) -> list[Document]:
        """Return the values of this node where it is a mapping."""
        return [Document(value) for value in self._value.values()] if isinstance(self._value, dict) else []

    def elements(self) -> list[Document]:
        """Return the elements of this node where it is a sequence."""
        return [Document(element) for element in self._value] if isinstance(self._value, list) else []

    def keyed_values(self) -> list[KeyedValue]:
        """Return the values of this node with their keys, where this node is a mapping."""
        return list(self._value.keyed_values.values()) if isinstance(self._value, _Mapping) else []

    def nodes(self) -> Iterator[Document]:
        """Return this node and every node under it, each mapping and sequence once however many aliases name it."""
        returned_ids: set[int] = set()
        pending = [self._value]
        while pending:
            value = pending.pop()
            if id(value) in returned_ids:
                continue
            if isinstance(value, dict | list):
                returned_ids.add(id(value))
            yield Document(value)
            pending.extend(_children(value))


@dataclass(frozen=True)
class KeyedValue:
    """A value of a YAML mapping, with its key and its text as the file writes them, and where the key starts.

    The text leaves out the quotes the file may write around a scalar, and is None where the value is not a scalar.
    The line is 1-based.
    """

    key: str
    value: Document
    text: str | None
    line_number: int
    column: int


def _children(value: object) -> list[object]:
    """Return the values of a mapping, or the items of a sequence."""
    if isinstance(value, dict):
        return list(value.values())
    return list(value) if isinstance(value, list) else []


class _Mapping(dict):
    """A mapping of a YAML document, which keeps each of its values with how the file writes it."""

    def __init__(self) -> None:
        """Start without entries."""
        super().__init__()
        self.keyed_values: dict[object, KeyedValue] = {}


class _Loader(yaml.SafeLoader):
    """A safe loader that reads a value under a tag it does not know as if the tag were not there."""


def _mapping(loader: _Loader, node: yaml.MappingNode) -> Iterator[_Mapping]:
    """Construct the mapping the node holds, keeping each of its values with how the file writes it.

    The mapping is yielded before its entries are constructed, so an alias inside it can refer to it. A merge key
    adds the entries of the mapping it names, each with the position of the key that mapping writes.
    """
    mapping = _Mapping()
    yield mapping
    mapping.update(loader.construct_mapping(node))
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        text = value_node.value if isinstance(value_node, yaml.ScalarNode) else None
        start = key_node.start_mark
        mapping.keyed_values[key] = KeyedValue(
            key_node.value, Document(mapping[key]), text, start.line + 1, start.column
        )


def _value_under_unknown_tag(loader: _Loader, suffix: str, node: yaml.Node) -> object:
    """Return the node constructed under the tag its value resolves to, rather than the unknown one."""
    del suffix
    # A copy, because the node itself is mid-construction and constructing it again reads as a recursive node.
    resolved = copy.copy(node)
    resolved.tag = loader.resolve(type(node), node.value, (True, False))
    return loader.construct_object(resolved, deep=True)


# A Compose override file writes `!reset` and `!override`, which the loader would otherwise refuse the document
# over. The prefix is `!`, so `!!python/object` still reaches no constructor and the loader stays safe.
_Loader.add_multi_constructor("!", _value_under_unknown_tag)
_Loader.add_constructor("tag:yaml.org,2002:map", _mapping)


def read(path: Path) -> Document | None:
    """Return the parsed YAML document the file holds, or None when the YAML does not parse."""
    return parse(path.read_text())


def parse(text: str) -> Document | None:
    """Return the parsed YAML document the text holds, or None when the YAML does not parse.

    Building the loader already refuses a character that YAML does not allow, such as a control character. A value
    that YAML reads as a date, such as 2023-13-45, raises a ValueError where the date does not exist.
    """
    try:
        loader = _Loader(text)
    except yaml.YAMLError:
        return None
    try:
        return Document(loader.get_single_data())
    except yaml.YAMLError, ValueError:
        return None
    finally:
        loader.dispose()

"""Read YAML files (CI configs and manifests) into documents to look values up in.

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

    def values(self) -> list[Document]:
        """Return the values of this node where it is a mapping."""
        return [Document(value) for value in self._value.values()] if isinstance(self._value, dict) else []

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


def _children(value: object) -> list[object]:
    """Return the values of a mapping, or the items of a sequence."""
    if isinstance(value, dict):
        return list(value.values())
    return list(value) if isinstance(value, list) else []


class _Loader(yaml.SafeLoader):
    """A safe loader that reads a value under a tag it does not know as if the tag were not there."""


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


def read(path: Path) -> Document | None:
    """Return the parsed YAML document, or None when the YAML does not parse."""
    loader = _Loader(path.read_text())
    try:
        return Document(loader.get_single_data())
    except yaml.YAMLError:
        return None
    finally:
        loader.dispose()

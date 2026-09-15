"""Read YAML files (CI configs and manifests).

Note: `import yaml` below resolves to the third-party PyYAML package, not this module — imports are absolute.
"""

import copy
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path


# A parsed YAML document: a mapping, a sequence, a scalar, or None for an empty file. Nothing narrower than `object`
# covers those, so this names the concept rather than narrowing it.
type Document = object

# What `read` answers with for a file whose YAML does not parse. A document is a dict or list, a scalar, or None for
# an empty file, so saying "did not parse" needs a value of its own.
UNPARSABLE = object()


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


def read(path: Path) -> Document:
    """Return the parsed YAML document: a dict or list, a scalar, None for an empty file, or UNPARSABLE."""
    loader = _Loader(path.read_text())
    try:
        return loader.get_single_data()
    except yaml.YAMLError:
        return UNPARSABLE
    finally:
        loader.dispose()

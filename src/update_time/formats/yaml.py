"""Read YAML files (CI configs and manifests).

Note: `import yaml` below resolves to the third-party PyYAML package, not this module — imports are absolute.
"""

from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path


# What `read` answers with for a file whose YAML does not parse. A document is a dict or list, a scalar, or None for
# an empty file, so saying "did not parse" needs a value of its own.
UNPARSABLE = object()


def read(path: Path) -> object:
    """Return the parsed YAML document: a dict or list, a scalar, None for an empty file, or UNPARSABLE."""
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return UNPARSABLE

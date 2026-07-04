"""Read YAML files (CI configs and manifests).

Note: `import yaml` below resolves to the third-party PyYAML package, not this module — imports are absolute.
"""

from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path


def read(path: Path) -> object:
    """Return the parsed YAML document: a dict or list, a scalar, or None for an empty file."""
    return yaml.safe_load(path.read_text())

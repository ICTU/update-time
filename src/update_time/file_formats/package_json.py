"""Read package.json files.

This module owns parsing the package.json *format*. What the parsed contents mean (which package manager, which
engines, which dependencies to update) is the caller's concern.
"""

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def read(path: Path) -> dict:
    """Return the parsed package.json."""
    return json.loads(path.read_text())

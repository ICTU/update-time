"""Bump exact-pinned dependencies in the PEP 723 inline script metadata of standalone .py files, using uv.

A `# /// script … # ///` block (see https://peps.python.org/pep-0723/) declares a script's dependencies as quoted
specs in a commented TOML table, the same `"name==version"` form a pyproject.toml uses, so uv understands it
natively and the same rewrite applies. Only exact `==` pins are bumped; looser specifiers are left untouched, so a
`package<=max` cap remains the way to opt a dependency out. Only .py files that actually contain a `# /// script`
block are processed; every other .py file is left untouched and never invokes uv.
"""

import re
import sys
from typing import TYPE_CHECKING

from update_time.domain.staleness import warn_about_stale_dependencies
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.package_managers import uv

if TYPE_CHECKING:
    from pathlib import Path

LOG = get_logger("python inline script metadata")
# PEP 723 inline metadata opens with a line that is exactly `# /// script` (a `# /// <type>` block of type `script`).
_SCRIPT_BLOCK = re.compile(r"^# /// script\s*$", re.MULTILINE)


def _has_script_block(script: Path) -> bool:
    """Return whether the .py file contains a PEP 723 `# /// script` inline-metadata block."""
    return _SCRIPT_BLOCK.search(script.read_text()) is not None


def update_python_inline_script_metadatas() -> int:
    """Find all .py files with inline script metadata and update the exact pins in their `# /// script` blocks."""
    scripts = [script for script in glob("*.py") if _has_script_block(script)]
    for script in scripts:
        uv.update_python_inline_script_metadata(script, LOG)
    # Check staleness after the update, so it reads the `==` pins uv settled on, reusing the PyPI source (the same
    # one the pyproject.toml and requirements.txt updaters use).
    warn_about_stale_dependencies(scripts, uv.newest_pypi_releases, LOG.warn_if_stale)
    return 0


def main() -> int:  # pragma: no cover
    """Update the dependencies in the repository's inline script metadata."""
    return update_python_inline_script_metadatas()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

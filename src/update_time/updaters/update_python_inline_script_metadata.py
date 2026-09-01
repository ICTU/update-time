"""Bump exact-pinned dependencies in the PEP 723 inline script metadata of standalone .py files, using uv.

A `# /// script … # ///` block (see https://peps.python.org/pep-0723/) declares a script's dependencies as quoted
specs in a commented TOML table, the same quoted PEP 508 specs a pyproject.toml uses, so uv understands it
natively and the same rewrite applies.
"""

from update_time.domain.file_type import INLINE_SCRIPT_METADATA
from update_time.file_formats import inline_script_metadata
from update_time.file_formats.dependency_file import InlineScript
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.package_managers import uv
from update_time.updaters.uv_pins import warn_about_pins

_LOG = get_logger("python inline script metadata")


def update_python_inline_script_metadatas() -> None:
    """Find all .py files with inline script metadata and update the exact pins in their `# /// script` blocks."""
    scripts = [
        InlineScript(path)
        for path in glob_for(INLINE_SCRIPT_METADATA)
        if inline_script_metadata.has_block(path.read_text())
    ]
    for script in scripts:
        uv.update_python_inline_script_metadata(script, _LOG)
    warn_about_pins(scripts, _LOG)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's inline script metadata."""
    update_python_inline_script_metadatas()


if __name__ == "__main__":  # pragma: no cover
    main()

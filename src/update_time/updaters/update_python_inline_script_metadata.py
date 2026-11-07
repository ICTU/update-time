"""Bump exact-pinned dependencies in the PEP 723 inline script metadata of standalone .py files, using uv."""

from update_time.domain.file_type import INLINE_SCRIPT_METADATA
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.inline_script import InlineScript
from update_time.package_managers.uv.lock import update_scripts
from update_time.updaters.uv_pins import warn_about_pins

_LOG = get_logger("python inline script metadata")


def update_python_inline_script_metadatas() -> None:
    """Find all .py files with inline script metadata and update the exact pins in their `# /// script` blocks."""
    scripts = _scripts()
    warn_about_pins(scripts, _LOG, update_scripts(scripts, _LOG))


def _scripts() -> list[InlineScript]:
    """Return the .py files carrying a `# /// script` block, each logged as checked, and warn about unreadable ones."""
    scripts = []
    for path in glob_for(INLINE_SCRIPT_METADATA):
        script = InlineScript(path)
        try:
            carries_a_block = script.has_metadata_block()
        except (OSError, UnicodeDecodeError) as error:
            _LOG.unreadable_script(path, error)
            continue
        if carries_a_block:
            _LOG.path(path)
            scripts.append(script)
    return scripts


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's inline script metadata."""
    update_python_inline_script_metadatas()


if __name__ == "__main__":  # pragma: no cover
    main()

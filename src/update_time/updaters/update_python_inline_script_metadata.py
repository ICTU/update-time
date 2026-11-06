"""Bump exact-pinned dependencies in the PEP 723 inline script metadata of standalone .py files, using uv."""

from typing import TYPE_CHECKING

from update_time.domain.file_type import INLINE_SCRIPT_METADATA
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.inline_script import InlineScript
from update_time.package_managers.uv.lock import read_script
from update_time.updaters.uv_pins import warn_about_pins

if TYPE_CHECKING:
    from update_time.manifests.python_dependency_toml import Declaration, PythonDependencyToml
    from update_time.package_managers.policy import ManagerPolicy

_LOG = get_logger("python inline script metadata")


def update_python_inline_script_metadatas() -> None:
    """Find all .py files with inline script metadata and update the exact pins in their `# /// script` blocks."""
    scripts = [
        script for path in glob_for(INLINE_SCRIPT_METADATA) if (script := InlineScript(path)).has_metadata_block()
    ]
    policies: dict[PythonDependencyToml, ManagerPolicy[Declaration]] = {}
    for script in scripts:
        policies.update(read_script(script, _LOG).update())
    warn_about_pins(scripts, _LOG, policies)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's inline script metadata."""
    update_python_inline_script_metadatas()


if __name__ == "__main__":  # pragma: no cover
    main()

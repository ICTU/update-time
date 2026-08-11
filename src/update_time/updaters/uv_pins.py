"""The checks the updaters that delegate to uv run over the pins it settled on."""

from typing import TYPE_CHECKING

from update_time.domain.staleness import warn_about_stale_dependencies
from update_time.file_formats import pyproject_toml as pyproject_toml_format
from update_time.package_managers import uv
from update_time.references.vulnerability import warn_about_vulnerable_dependencies
from update_time.sources.osv import Ecosystem

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from update_time.io.log import Logger


def warn_about_pins(files: Sequence[Path], log: Logger) -> None:
    """Warn about the pins uv settled on, for the stale ones and for the vulnerable ones.

    Run after the update, so both checks read the `==` pins uv rewrote rather than the ones the file held before.
    Stating the pair here is what keeps a file kind uv updates from being given one check and not the other. The
    files are a sequence rather than an iterable, since each check walks them again.
    """
    warn_about_stale_dependencies(files, uv.newest_pypi_releases, log.warn_if_stale)
    warn_about_vulnerable_dependencies(files, pyproject_toml_format.pinned_versions, Ecosystem.PYPI, log)

"""The checks the updaters that delegate to uv run over the dependencies their files declare."""

from typing import TYPE_CHECKING

from update_time.file_formats import pyproject_toml as pyproject_toml_format
from update_time.package_managers import uv
from update_time.references.delegated import warn_about_stale_dependencies, warn_about_yanked_dependencies
from update_time.references.vulnerability import warn_about_vulnerable_dependencies
from update_time.sources.osv import Ecosystem

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from update_time.io.log import Logger


def warn_about_pins(files: Sequence[Path], log: Logger) -> None:
    """Warn about the dependencies the files declare: the stale ones, the yanked ones, and the vulnerable ones.

    An updater that delegates the update never calls a source per dependency, so it makes these passes itself, over
    the references a resolver reads back from the file — the only party that knows where in each file a reference
    sits. Each pass takes that resolver as a callback, which lets the pins be read by whichever file format
    declares them.

    Run after the update, so each check reads the `==` pins as the run rewrote them rather than as the file held
    them before. Stating the set here is what keeps a file kind uv updates from being given one check and not the
    others. The files are a sequence rather than an iterable, since each check walks them again. The staleness
    check reads the newest release of every dependency the file declares, while the yank check reads the release
    each pin itself names, since that is the version the run leaves the pin on. Both read the package's PyPI index,
    which is cached for the run, so the second to run costs no request of its own.
    """
    warn_about_stale_dependencies(files, uv.newest_pypi_releases, log)
    warn_about_yanked_dependencies(files, uv.pinned_pypi_releases, log)
    warn_about_vulnerable_dependencies(files, pyproject_toml_format.pinned_versions, Ecosystem.PYPI, log)

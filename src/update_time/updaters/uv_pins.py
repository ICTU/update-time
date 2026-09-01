"""The checks the updaters that delegate to uv run over the dependencies their files declare."""

from typing import TYPE_CHECKING

from update_time.package_managers import uv
from update_time.references.delegated import warn_about_projects, warn_about_yanked_dependencies
from update_time.references.vulnerability import warn_about_vulnerable_dependencies
from update_time.sources.osv import Ecosystem

if TYPE_CHECKING:
    from collections.abc import Sequence

    from update_time.file_formats.dependency_file import DependencyTomlFile
    from update_time.io.log import Logger


def warn_about_pins(files: Sequence[DependencyTomlFile], log: Logger) -> None:
    """Run every check over the dependencies PyPI serves a release for.

    The files are a sequence rather than an iterable, since each check walks them again.
    """
    warn_about_projects(files, uv.pypi_projects, log)
    warn_about_yanked_dependencies(files, uv.pinned_pypi_releases, log)
    warn_about_vulnerable_dependencies(files, uv.pinned_versions, Ecosystem.PYPI, log)

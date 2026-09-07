"""The checks the updaters that delegate to uv run over the dependencies their files declare."""

from typing import TYPE_CHECKING

from update_time.file_formats import pyproject_toml as pyproject_toml_format
from update_time.package_managers import uv
from update_time.references.delegated import (
    warn_about_projects,
    warn_about_redundant_directives,
    warn_about_yanked_dependencies,
)
from update_time.references.vulnerability import warn_about_vulnerable_dependencies
from update_time.sources.osv import Ecosystem

if TYPE_CHECKING:
    from collections.abc import Iterable

    from update_time.file_formats.dependency_file import DependencyTomlFile
    from update_time.io.log import Logger


def warn_about_pins(files: Iterable[DependencyTomlFile], log: Logger) -> None:
    """Run every check over the dependencies PyPI serves a release for, and report every marker over them all.

    Each file's declarations are read once and walked by every pass, so they are a list rather than a generator,
    which the passes after the first would find empty. A dependency PyPI serves no release for takes no check, and
    its marker is reported all the same, since a redundant directive is what the reader has to hear about.
    """
    declared = [pyproject_toml_format.declared_dependencies(file) for file in files]
    served = [uv.pypi_served(declarations) for declarations in declared]
    warn_about_redundant_directives(declared, log, uv.no_pypi_release)
    warn_about_projects(served, uv.pypi_projects, log)
    warn_about_yanked_dependencies(served, uv.pinned_pypi_releases, log)
    warn_about_vulnerable_dependencies(served, uv.pinned_versions, Ecosystem.PYPI, log)

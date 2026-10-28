"""The checks the updaters that delegate to uv run over the dependencies their files declare."""

from typing import TYPE_CHECKING

from update_time.package_managers import uv
from update_time.references.delegated import warn_about_delegated_checks, warn_about_redundant_directives
from update_time.sources.osv import Ecosystem
from update_time.sources.pypi import yank_state

if TYPE_CHECKING:
    from collections.abc import Iterable

    from update_time.io.log import Logger
    from update_time.manifests.python_dependency_toml import PythonDependencyToml


def warn_about_pins(files: Iterable[PythonDependencyToml], log: Logger) -> None:
    """Run every check over the dependencies PyPI serves a release for, and report every marker over them all.

    Each file's declarations are read once and walked by every pass, so they are a list rather than a generator,
    which the passes after the first would find empty. A dependency PyPI serves no release for takes no check, and
    its marker is reported all the same, since a redundant directive is what the reader has to hear about.
    """
    declared = [file.declarations() for file in files]
    warn_about_redundant_directives(declared, log, uv.POLICY)
    served = [uv.pypi_served(declarations) for declarations in declared]
    warn_about_delegated_checks(served, log, uv.pypi_projects, yank_state, Ecosystem.PYPI)

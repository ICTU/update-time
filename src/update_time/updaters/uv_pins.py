"""The checks the updaters that delegate to uv run over the dependencies their files declare."""

from typing import TYPE_CHECKING

from update_time.package_managers.uv.marker_policy import MARKER_POLICY
from update_time.package_managers.uv.uv import declarations_of, pypi_served
from update_time.references.delegated import warn_about_delegated_checks, warn_about_redundant_directives
from update_time.sources.pypi import SOURCE

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from update_time.io.log import Logger
    from update_time.manifests.python_dependency_toml import Declaration, PythonDependencyToml
    from update_time.package_managers.policy import ManagerPolicy


def warn_about_pins(
    files: Iterable[PythonDependencyToml],
    log: Logger,
    policies: Mapping[PythonDependencyToml, ManagerPolicy[Declaration]],
) -> None:
    """Run every check over the dependencies PyPI serves a release for, and report every marker over them all.

    Each file's declarations are read once and walked by every pass, so they are a list rather than a generator,
    which the passes after the first would find empty. A dependency PyPI serves no release for takes no check, and
    its marker is reported all the same, since a redundant directive is what the reader has to hear about. A file's
    markers are judged by the policy `policies` holds for it, or else by uv's marker policy.
    """
    declared: list[list[Declaration]] = []
    for file in files:
        declarations = declarations_of(file)
        warn_about_redundant_directives([declarations], log, policies.get(file, MARKER_POLICY))
        declared.append(declarations)
    warn_about_delegated_checks([pypi_served(declarations) for declarations in declared], log, SOURCE)

"""Find pyproject.toml files, check their dependencies, and update those of the files uv runs for."""

from typing import TYPE_CHECKING

from update_time.domain.file_type import PYPROJECT_TOML
from update_time.formats import toml
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests import pyproject_config
from update_time.manifests import pyproject_toml as pyproject_toml_manifest
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.package_managers.uv import marker_policy, uv
from update_time.package_managers.uv.lock import update_workspaces
from update_time.updaters.uv_pins import warn_about_pins

if TYPE_CHECKING:
    from update_time.manifests.python_dependency_toml import Declaration, PythonDependencyToml
    from update_time.package_managers.policy import ManagerPolicy

_LOG = get_logger("pyproject.toml")
# The policy for the markers of a pyproject.toml, by why uv does not run for it, or None where uv runs for it.
_POLICIES: dict[uv.Decision, ManagerPolicy[Declaration] | None] = {
    uv.Decision.RUN: None,
    uv.Decision.UNMANAGED: marker_policy.UNMANAGED_FILE_POLICY,
    uv.Decision.SEVERAL_MANAGERS: marker_policy.MORE_THAN_ONE_MANAGER_POLICY,
    uv.Decision.NOTHING_TO_RESOLVE: marker_policy.MARKER_POLICY,
    uv.Decision.ROOT_OUTSIDE_THE_SCAN: marker_policy.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_POLICY,
}


def update_pyproject_tomls() -> None:
    """Find the pyproject.toml files, check their pins, and update the pins, and any uv.lock, of each that uv manages.

    Where uv cannot lock a workspace's new pins, each pyproject.toml the run rewrote gets back the pins it held before.
    """
    pyproject_tomls: list[PyprojectToml] = []
    projects_for_uv: list[PyprojectToml] = []
    projects_left_as_they_are: dict[PythonDependencyToml, ManagerPolicy[Declaration]] = {}
    for path in glob_for(PYPROJECT_TOML):
        _LOG.path(path)
        table = toml.read(path)
        if table is None:
            _LOG.invalid_file(path, toml.FORMAT)  # Exists but isn't valid TOML: skip rather than crash on it.
        elif (config := pyproject_config.from_table(table)) is None:
            _LOG.invalid_file(path, pyproject_toml_manifest.FORMAT)
        else:
            pyproject_toml = PyprojectToml(path)
            pyproject_tomls.append(pyproject_toml)
            if (policy := _POLICIES[uv.decision(path, config)]) is None:
                projects_for_uv.append(pyproject_toml)
            else:
                projects_left_as_they_are[pyproject_toml] = policy
                marker_policy.report_file_markers(pyproject_toml, _LOG, policy)
    policies = update_workspaces(projects_for_uv, _LOG)
    policies.update(projects_left_as_they_are)
    warn_about_pins(pyproject_tomls, _LOG, policies)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's pyproject.toml files."""
    update_pyproject_tomls()


if __name__ == "__main__":  # pragma: no cover
    main()

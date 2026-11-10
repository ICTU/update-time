"""Find pyproject.toml files, check their dependencies, and update those of the files uv runs for."""

from typing import TYPE_CHECKING

from update_time.domain.file_type import PYPROJECT_TOML
from update_time.formats import toml
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests import pyproject_toml as pyproject_toml_manifest
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.manifests.python_dependency_toml import is_well_formed
from update_time.package_managers.uv import marker_policy, uv
from update_time.package_managers.uv.lock import update_workspaces
from update_time.updaters.uv_pins import warn_about_pins

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.manifests.python_dependency_toml import Declaration, PythonDependencyToml
    from update_time.package_managers.policy import ManagerPolicy

_LOG = get_logger("pyproject.toml")


def update_pyproject_tomls() -> None:
    """Find the pyproject.toml files, check their pins, and update the pins, and any uv.lock, of each that uv manages.

    uv runs only for a pyproject.toml that uv manages and that declares a dependency for it to resolve, in a workspace
    whose root the scan reaches. Where uv cannot lock a workspace's new pins, each pyproject.toml the run rewrote gets
    back the pins it held before.
    """
    pyproject_tomls: list[PyprojectToml] = []
    projects_for_uv: list[PyprojectToml] = []
    projects_left_as_they_are: dict[PythonDependencyToml, ManagerPolicy[Declaration]] = {}
    for path in glob_for(PYPROJECT_TOML):
        _LOG.path(path)
        config = toml.read(path)
        if config is None:
            _LOG.invalid_file(path, toml.FORMAT)  # Exists but isn't valid TOML: skip rather than crash on it.
        elif not is_well_formed(config):
            _LOG.invalid_file(path, pyproject_toml_manifest.FORMAT)
        elif (name := uv.other_manager(path, config)) is not None:
            _LOG.unsupported_package_manager(path, name, "uv")
        else:
            pyproject_toml = PyprojectToml(path)
            pyproject_tomls.append(pyproject_toml)
            if not pyproject_toml.declares_a_dependency(config):
                _LOG.no_dependencies(path, "uv", "resolve")
                marker_policy.report_file_markers(pyproject_toml, _LOG, marker_policy.MARKER_POLICY)
            elif (policy := _policy_for_a_file_left_as_it_is(path, config)) is None:
                projects_for_uv.append(pyproject_toml)
            else:
                projects_left_as_they_are[pyproject_toml] = policy
                marker_policy.report_file_markers(pyproject_toml, _LOG, policy)
    policies = update_workspaces(projects_for_uv, _LOG)
    policies.update(projects_left_as_they_are)
    warn_about_pins(pyproject_tomls, _LOG, policies)


def _policy_for_a_file_left_as_it_is(path: Path, config: dict) -> ManagerPolicy[Declaration] | None:
    """Return the policy for the markers of a pyproject.toml that uv does not run for, or None where uv runs for it."""
    if not uv.workspace_root_is_scanned(path):
        return marker_policy.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_POLICY
    return None if uv.manages(path, config) else marker_policy.UNMANAGED_FILE_POLICY


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's pyproject.toml files."""
    update_pyproject_tomls()


if __name__ == "__main__":  # pragma: no cover
    main()

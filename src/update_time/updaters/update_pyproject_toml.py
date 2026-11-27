"""Find pyproject.toml files, check their dependencies, and update those of the files uv runs for."""

from typing import TYPE_CHECKING, Final

from update_time.domain.file_type import PYPROJECT_TOML
from update_time.formats.toml import FORMAT, read
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests import pyproject_toml as pyproject_toml_manifest
from update_time.manifests.pyproject_config import from_table
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.package_managers.uv.lock import update_workspaces
from update_time.package_managers.uv.marker_policy import MARKER_POLICY, UNMANAGED_FILE_POLICY, report_file_markers
from update_time.package_managers.uv.uv import Decision, decision
from update_time.updaters.uv_pins import warn_about_pins

if TYPE_CHECKING:
    from update_time.io.log import Logger
    from update_time.manifests.python_dependency_toml import Declaration, PythonDependencyToml
    from update_time.package_managers.policy import ManagerPolicy

_LOG: Final[Logger] = get_logger("pyproject.toml")


def _update_pyproject_tomls() -> None:
    """Find the pyproject.toml files, check their pins, and update the pins, and any uv.lock, of each that uv manages.

    Where uv cannot lock a workspace's new pins, each pyproject.toml the run rewrote gets back the pins it held before.
    """
    pyproject_tomls: list[PyprojectToml] = []
    projects_for_uv: list[PyprojectToml] = []
    projects_left_as_they_are: dict[PythonDependencyToml, ManagerPolicy[Declaration]] = {}
    for path in glob_for(PYPROJECT_TOML):
        _LOG.path(path)
        if (table := read(path)) is None:
            _LOG.invalid_file(path, FORMAT)  # Exists but isn't valid TOML: skip rather than crash on it.
        elif (config := from_table(table)) is None:
            _LOG.invalid_file(path, pyproject_toml_manifest.FORMAT)
        else:
            pyproject_toml = PyprojectToml(path)
            pyproject_tomls.append(pyproject_toml)
            if (policy := _policy(decision(path, config))) is None:
                projects_for_uv.append(pyproject_toml)
            else:
                projects_left_as_they_are[pyproject_toml] = policy
                report_file_markers(pyproject_toml, _LOG, policy)
    policies = update_workspaces(projects_for_uv, _LOG)
    policies.update(projects_left_as_they_are)
    warn_about_pins(pyproject_tomls, _LOG, policies)


if __name__ == "__main__":
    _update_pyproject_tomls()


def _policy(why: Decision) -> ManagerPolicy[Declaration] | None:
    """Return the policy for the markers of a pyproject.toml, by why uv does not run for it, or None where uv runs."""
    return {
        Decision.RUN: None,
        Decision.UNMANAGED: UNMANAGED_FILE_POLICY,
        Decision.SEVERAL_MANAGERS: MARKER_POLICY.policy_for_more_than_one_manager,
        Decision.NOTHING_TO_RESOLVE: MARKER_POLICY,
        Decision.ROOT_OUTSIDE_THE_SCAN: MARKER_POLICY.policy_for_a_workspace_root_outside_the_scan,
    }[why]

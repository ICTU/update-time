"""Find pyproject.toml files, check their dependencies, and update those of the files uv manages."""

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

_LOG = get_logger("pyproject.toml")


def update_pyproject_tomls() -> None:
    """Find the pyproject.toml files, check their pins, and update the pins and the uv.lock of each that uv manages.

    uv runs only for a pyproject.toml that uv manages and that declares a dependency for it to resolve, since `uv lock`
    writes a uv.lock beside any pyproject.toml it runs for. Where uv cannot lock a workspace's new pins, each
    pyproject.toml the run rewrote gets back the pins it held before.
    """
    pyproject_tomls: list[PyprojectToml] = []
    projects_for_uv: list[PyprojectToml] = []
    unmanaged_projects: list[PyprojectToml] = []
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
            elif uv.manages(path, config):
                projects_for_uv.append(pyproject_toml)
            else:
                unmanaged_projects.append(pyproject_toml)
                marker_policy.report_file_markers(pyproject_toml, _LOG, marker_policy.UNMANAGED_FILE_POLICY)
    # Persist the cooldown into config before running uv, so `uv lock` reads it and the lockfile it produces stays
    # reproducible with a plain `uv sync --locked` (see `uv.configure_cooldown`). Then update each workspace, so the
    # files get the text they hold now back where uv cannot lock.
    uv.configure_cooldown(projects_for_uv)
    policies = update_workspaces(projects_for_uv, _LOG)
    policies.update(dict.fromkeys(unmanaged_projects, marker_policy.UNMANAGED_FILE_POLICY))
    warn_about_pins(pyproject_tomls, _LOG, policies)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's pyproject.toml files."""
    update_pyproject_tomls()


if __name__ == "__main__":  # pragma: no cover
    main()

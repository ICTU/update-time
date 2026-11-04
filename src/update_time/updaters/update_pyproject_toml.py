"""Find uv-managed pyproject.toml files and update their dependencies and lockfiles."""

from update_time.domain.file_type import PYPROJECT_TOML
from update_time.formats import toml
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests import pyproject_toml as pyproject_toml_manifest
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.manifests.python_dependency_toml import is_well_formed
from update_time.package_managers.uv import uv
from update_time.package_managers.uv.workspace import read_workspaces
from update_time.updaters.uv_pins import warn_about_pins

_LOG = get_logger("pyproject.toml")


def update_pyproject_tomls() -> None:
    """Find all uv-managed pyproject.toml files, and update their exact pins and their uv.lock files.

    uv runs only for a pyproject.toml that declares a dependency for it to resolve, since `uv lock` writes a uv.lock
    beside any pyproject.toml it runs for. Update-time checks the pins of every uv-managed pyproject.toml.
    Where uv cannot lock a workspace's new pins, each pyproject.toml the run rewrote gets back the pins it held before.
    """
    uv_projects: list[PyprojectToml] = []
    projects_with_dependencies: list[PyprojectToml] = []
    for path in glob_for(PYPROJECT_TOML):
        config = toml.read(path)
        if config is None:
            _LOG.invalid_file(path, toml.FORMAT)  # Exists but isn't valid TOML: skip rather than crash on it.
        elif not is_well_formed(config):
            _LOG.invalid_file(path, pyproject_toml_manifest.FORMAT)
        elif (name := uv.manager_name(path, config)) != "uv":
            _LOG.unsupported_package_manager(path, name, "uv")
        else:
            pyproject_toml = PyprojectToml(path)
            uv_projects.append(pyproject_toml)
            if pyproject_toml.declares_a_dependency(config):
                projects_with_dependencies.append(pyproject_toml)
            else:
                _LOG.no_dependencies(path, "uv", "resolve")
                uv.report_file_markers(pyproject_toml, _LOG)
    # Persist the cooldown into config before running uv, so `uv lock` reads it and the lockfile it produces stays
    # reproducible with a plain `uv sync --locked` (see `uv.configure_cooldown`). Then read each workspace, so the files
    # get the text they hold now back where uv cannot lock.
    uv.configure_cooldown(projects_with_dependencies)
    workspaces = read_workspaces(projects_with_dependencies)
    for workspace in workspaces:
        workspace.update()
    warn_about_pins(uv_projects, _LOG)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's pyproject.toml files."""
    update_pyproject_tomls()


if __name__ == "__main__":  # pragma: no cover
    main()

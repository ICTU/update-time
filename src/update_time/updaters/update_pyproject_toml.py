"""Find uv-managed pyproject.toml files and update their dependencies and lockfiles."""

from update_time.domain.file_type import PYPROJECT_TOML
from update_time.formats import toml
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.package_managers import uv
from update_time.updaters.uv_pins import warn_about_pins

_LOG = get_logger("pyproject.toml")


def update_pyproject_tomls() -> None:
    """Find all uv-managed pyproject.toml files, update them, and then update the uv.lock files.

    uv runs only for a pyproject.toml that declares a dependency for it to resolve, since `uv lock` writes a uv.lock
    beside any pyproject.toml it runs for. Update-time checks the pins of every uv-managed pyproject.toml.
    """
    uv_projects: list[PyprojectToml] = []
    projects_with_dependencies: list[PyprojectToml] = []
    for path in glob_for(PYPROJECT_TOML):
        config = toml.read(path)
        if config is None:
            _LOG.invalid_file(path, toml.FORMAT)  # Exists but isn't valid TOML: skip rather than crash on it.
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
    # Persist the cooldown into config before running uv, so `uv tree`/`uv lock` read it and the lockfile they
    # produce stays reproducible with a plain `uv sync --locked` (see `uv.configure_cooldown`).
    uv.configure_cooldown([pyproject_toml.path for pyproject_toml in projects_with_dependencies])
    # Update every pyproject.toml first, then lock (a uv workspace shares one lockfile, so all member manifests
    # must be bumped before locking). Skip the lockfile update for files whose `uv tree` failed: it would fail too.
    updated = [
        pyproject_toml
        for pyproject_toml in projects_with_dependencies
        if uv.update_pyproject_toml(pyproject_toml, _LOG)
    ]
    for pyproject_toml in updated:
        uv.update_uv_lock(pyproject_toml.path)
    warn_about_pins(uv_projects, _LOG)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's pyproject.toml files."""
    update_pyproject_tomls()


if __name__ == "__main__":  # pragma: no cover
    main()

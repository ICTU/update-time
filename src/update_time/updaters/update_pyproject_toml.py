"""Find uv-managed pyproject.toml files and update their dependencies and lockfiles."""

from update_time.domain.staleness import warn_about_stale_dependencies
from update_time.file_formats import pyproject_toml as pyproject_toml_format
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.package_managers import uv

LOG = get_logger("pyproject.toml")


def update_pyproject_tomls() -> None:
    """Find all uv-managed pyproject.toml files, update them, and then update the uv.lock files."""
    files = []
    for pyproject_toml in glob("pyproject.toml"):
        config = pyproject_toml_format.read(pyproject_toml)
        if config is None:
            LOG.invalid_pyproject_toml(pyproject_toml)  # Exists but isn't valid TOML: skip rather than crash on it.
        elif (manager := uv.python_manager(pyproject_toml, config)) == "uv":
            files.append(pyproject_toml)
        else:
            LOG.unsupported_package_manager(pyproject_toml, manager, "uv")
    # Persist the cooldown into config before running uv, so `uv tree`/`uv lock` read it and the lockfile they
    # produce stays reproducible with a plain `uv sync --locked` (see `uv.configure_cooldown`).
    uv.configure_cooldown(files)
    # Update every pyproject.toml first, then lock (a uv workspace shares one lockfile, so all member manifests
    # must be bumped before locking). Skip the lockfile update for files whose `uv tree` failed: it would fail too.
    updated = [pyproject_toml for pyproject_toml in files if uv.update_pyproject_toml(pyproject_toml, LOG)]
    for pyproject_toml in updated:
        uv.update_uv_lock(pyproject_toml)
    # Check staleness after the update, so it reads the `==` pins uv settled on, reusing the PyPI source (the same
    # one the requirements.txt updater uses).
    warn_about_stale_dependencies(files, uv.newest_pypi_releases, LOG.warn_if_stale)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's pyproject.toml files."""
    update_pyproject_tomls()


if __name__ == "__main__":  # pragma: no cover
    main()

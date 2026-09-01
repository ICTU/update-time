"""Find uv-managed pyproject.toml files and update their dependencies and lockfiles."""

from update_time.domain.file_type import PYPROJECT_TOML
from update_time.file_formats import toml
from update_time.file_formats.dependency_file import PyprojectToml
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.package_managers import uv
from update_time.updaters.uv_pins import warn_about_pins

_LOG = get_logger("pyproject.toml")


def update_pyproject_tomls() -> None:
    """Find all uv-managed pyproject.toml files, update them, and then update the uv.lock files."""
    paths = []
    for path in glob_for(PYPROJECT_TOML):
        config = toml.read(path)
        if config is None:
            _LOG.invalid_pyproject_toml(path)  # Exists but isn't valid TOML: skip rather than crash on it.
        elif (manager := uv.python_manager(path, config)) == "uv":
            paths.append(path)
        else:
            _LOG.unsupported_package_manager(path, manager, "uv")
    # Persist the cooldown into config before running uv, so `uv tree`/`uv lock` read it and the lockfile they
    # produce stays reproducible with a plain `uv sync --locked` (see `uv.configure_cooldown`).
    uv.configure_cooldown(paths)
    # Update every pyproject.toml first, then lock (a uv workspace shares one lockfile, so all member manifests
    # must be bumped before locking). Skip the lockfile update for files whose `uv tree` failed: it would fail too.
    pyproject_tomls = [PyprojectToml(path) for path in paths]
    updated = [pyproject_toml for pyproject_toml in pyproject_tomls if uv.update_pyproject_toml(pyproject_toml, _LOG)]
    for pyproject_toml in updated:
        uv.update_uv_lock(pyproject_toml.path)
    warn_about_pins(pyproject_tomls, _LOG)


def main() -> None:  # pragma: no cover
    """Update the dependencies in the repository's pyproject.toml files."""
    update_pyproject_tomls()


if __name__ == "__main__":  # pragma: no cover
    main()

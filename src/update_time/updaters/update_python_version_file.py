"""Python version file updater bumps the CPython version pinned in `.python-version` files within `requires-python`."""

from functools import cache, wraps
from typing import TYPE_CHECKING

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from update_time.domain.bound import Verb, VersionBound, joint_bound
from update_time.domain.file_type import PYTHON_VERSION_FILE
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.pyproject_toml import PyprojectToml
from update_time.manifests.python_version import PYTHON, PYTHON_VERSION_REFERENCE
from update_time.markers.marker import Scope
from update_time.package_managers import uv
from update_time.references.file import update_file
from update_time.references.match import matched_reference, reference_matches
from update_time.sources.oci import get_latest_tag

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.dependency import DependencyVersion, PinnedDependency
    from update_time.domain.line import Line
    from update_time.domain.reference import Reference

_LOG = get_logger("python version file")


def update_python_version_files() -> None:
    """Update the CPython version in all `.python-version` files found recursively from the start directory."""
    for version_file in glob_for(PYTHON_VERSION_FILE):
        requires_pythons = _requires_pythons(version_file.parent)
        bound = joint_bound(*(_python_bound(requires_python) for requires_python in requires_pythons))
        lines = update_file(
            version_file, PYTHON_VERSION_REFERENCE, get_new_version=_tag_getter(bound), logger=_LOG, dependency=PYTHON
        )
        for requires_python in requires_pythons:
            _report_requires_python(lines, requires_python)


def _requires_pythons(directory: Path) -> list[Reference]:
    """Return the `requires-python` of each project in the workspace of the nearest pyproject.toml: what uv checks."""
    if (pyproject_toml := _nearest_pyproject_toml(directory)) is None:
        return []
    projects = uv.workspace_pyproject_tomls(pyproject_toml)
    return [requires_python for project in projects if (requires_python := _requires_python(project)) is not None]


@cache
def _requires_python(pyproject_toml: Path) -> Reference | None:
    """Return the `requires-python` of the project, warning where it cannot be read or is not a version specifier."""
    try:
        requires_python = PyprojectToml(pyproject_toml).requires_python()
    except (OSError, UnicodeDecodeError) as error:
        _LOG.unreadable_requires_python(pyproject_toml, error)
        return None
    if requires_python is None:
        return None
    try:
        _python_bound(requires_python)
    except InvalidSpecifier:
        _LOG.invalid_requires_python(requires_python)
        return None
    return requires_python


def _nearest_pyproject_toml(directory: Path) -> Path | None:
    """Return the nearest pyproject.toml at or above the directory: the project uv runs the entry for."""
    for ancestor in (directory, *directory.parents):
        if (pyproject_toml := ancestor / "pyproject.toml").exists():
            return pyproject_toml
    return None


def _report_requires_python(lines: list[Line], requires_python: Reference) -> None:
    """Warn about each entry the run leaves outside the `requires-python`, and log each one it holds back."""
    bound = _python_bound(requires_python)
    for line, match, marker in reference_matches(lines, PYTHON_VERSION_REFERENCE):
        reference = matched_reference(match, line.location, PYTHON)
        version = reference.current_version
        if not bound.keeps(Version(version), version):
            _LOG.python_version_outside_requires_python(reference, requires_python)
        elif bound.redundancy(version) is None and not marker.ignores(Scope.UPDATE):
            _LOG.held_back_by_requires_python(reference, requires_python)


def _python_bound(requires_python: Reference) -> VersionBound:
    """Return the bound the `requires-python` puts on the Python version."""
    return VersionBound(Verb.ALLOW, SpecifierSet(requires_python.current_version))


def _tag_getter(requires_python_bound: VersionBound) -> NewVersionGetter:
    """Return a getter of the latest `python` tag that both the `requires-python` and the marker's bound keep.

    The getter carries the capabilities of `get_latest_tag`, such as dating the tags on Docker Hub.
    """

    @wraps(get_latest_tag, assigned=())
    def get_new_version(
        pinned: PinnedDependency, version_bound: VersionBound, cooldown_days: int, /
    ) -> DependencyVersion:
        return get_latest_tag(pinned, joint_bound(version_bound, requires_python_bound), cooldown_days)

    return get_new_version


def main() -> None:  # pragma: no cover
    """Update the CPython version in the repository's `.python-version` files."""
    update_python_version_files()


if __name__ == "__main__":  # pragma: no cover
    main()

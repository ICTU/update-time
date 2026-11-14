"""The lock bun writes for a project or a workspace, and the versions it records for their dependencies."""

from pathlib import Path
from typing import TYPE_CHECKING

from update_time.formats import yaml
from update_time.manifests.package_json import DEPENDENCY_SECTIONS

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyName, VersionString

# Why a lock that does not parse, or does not name the version of its format, cannot be read.
_NOT_A_BUN_LOCK = "it does not hold the JSON bun writes"


def locked_versions(lock: Path) -> dict[Path, dict[DependencyName, VersionString]]:
    """Return the version the lock records for each dependency of each workspace, keyed by the workspace's path.

    bun writes the lock as JSON with trailing commas, which the YAML parser reads as a flow mapping. Raise an `OSError`
    where the lock cannot be read, and a `ValueError` where it does not hold the JSON bun writes.
    """
    document = yaml.read(lock)
    if document is None or "lockfileVersion" not in document:
        raise ValueError(_NOT_A_BUN_LOCK)
    workspaces, packages = document["workspaces"], document["packages"]
    return {Path(path): _workspace_versions(workspaces[path], packages) for path in workspaces}


def _workspace_versions(workspace: yaml.Document, packages: yaml.Document) -> dict[DependencyName, VersionString]:
    """Return the version the lock records for each dependency the workspace declares, where its entry names one."""
    name = workspace["name"].string()
    declared = [dependency for section in DEPENDENCY_SECTIONS for dependency in workspace[section]]
    return {
        dependency: version
        for dependency in declared
        if (version := _version(_entry(packages, name, dependency))) is not None
    }


def _entry(packages: yaml.Document, workspace: str | None, dependency: DependencyName) -> yaml.Document:
    """Return the lock's entry for the dependency of the workspace.

    The lock keys a version that only the workspace resolves to under the workspace's name, such as `a/is-number`, and
    a version the workspaces share under the dependency's name alone.
    """
    own = f"{workspace}/{dependency}"
    return packages[own] if own in packages else packages[dependency]


def _version(package: yaml.Document) -> VersionString | None:
    """Return the version from the package's entry, whose first element names the package and its version.

    The entry of a workspace member, a git repository, or a path does not name a version, as `b@workspace:packages/b`
    shows.
    """
    resolution = next(iter(package.elements()), yaml.Document()).string()
    version = None if resolution is None else resolution.rsplit("@", maxsplit=1)[-1]
    return None if version is None or ":" in version else version

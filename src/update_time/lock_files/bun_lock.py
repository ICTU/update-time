"""The lock bun writes for a project or a workspace, and the versions it records for their dependencies."""

from pathlib import Path
from typing import TYPE_CHECKING

from update_time.formats.yaml import Document, read
from update_time.manifests.package_json import DEPENDENCY_SECTIONS

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyName, VersionString

    from .locked_versions import LockedVersions


def locked_versions(lock: Path) -> LockedVersions:
    """Return the version the lock records for each dependency of each workspace, keyed by the workspace's path.

    bun writes the lock as JSON with trailing commas, which the YAML parser reads as a flow mapping. Raise an `OSError`
    where the lock cannot be read, and a `ValueError` where it does not hold the JSON bun writes.
    """
    document = read(lock)
    # Why a lock that does not parse, or does not name the version of its format, cannot be read.
    if document is None or "lockfileVersion" not in document:
        msg = "it does not hold the JSON bun writes"
        raise ValueError(msg)
    workspaces, packages = document["workspaces"], document["packages"]
    return {Path(path): _workspace_versions(path, workspaces[path], packages) for path in workspaces}


def _workspace_versions(path: str, workspace: Document, packages: Document) -> dict[DependencyName, VersionString]:
    """Return the version the lock records for each dependency the workspace declares, where its entry names one."""
    # The root's dependencies sit at the top of the lock. A key under the root's name, such as `elysia/cookie`, belongs
    # to a package of the same name that the root depends on.
    member = workspace["name"].string() if path else None
    declared = [dependency for section in DEPENDENCY_SECTIONS for dependency in workspace[section]]
    return {
        dependency: version
        for dependency in declared
        if (version := _version(_entry(packages, member, dependency))) is not None
    }


def _version(package: Document) -> VersionString | None:
    """Return the version from the package's entry, whose first element names the package and its version.

    The entry of a workspace member, a git repository, or a path does not name a version, as `b@workspace:packages/b`
    shows.
    """
    resolution = next(iter(package.elements()), Document()).string()
    version = None if resolution is None else resolution.rsplit("@", maxsplit=1)[-1]
    return None if version is None or ":" in version else version


def _entry(packages: Document, member: str | None, dependency: DependencyName) -> Document:
    """Return the lock's entry for the dependency of the member, or of the root where the member is None.

    The lock keys a version that only a member resolves to under the member's name, such as `a/is-number`, and a
    version the workspaces share under the dependency's name alone.
    """
    own = f"{member}/{dependency}"
    return packages[own] if member is not None and own in packages else packages[dependency]

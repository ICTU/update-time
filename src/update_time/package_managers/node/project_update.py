"""Compare the versions a Node package manager locks per project before and after its update, and log the changes."""

from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from update_time.domain.reference import Reference
from update_time.domain.version_scheme import SEMVER
from update_time.io.log import FileContent, get_logger
from update_time.manifests.package_json import PackageJson
from update_time.primitives.location import Location
from update_time.sources import npmjs

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.lock_files.locked_versions import LockedVersions
    from update_time.manifests.npm_declaration import CatalogEntry, Catalogs


_LOG = get_logger("package.json")


@dataclass(frozen=True)
class ProjectSnapshot:
    """The package.json and the locked versions of a project, as they were before the update."""

    listed: Path
    path: Path
    package_json: PackageJson | None
    locked: dict[DependencyName, VersionString]


@dataclass(frozen=True)
class ProjectUpdate:
    """The snapshot of a project from before the update, with its package.json and locked versions after it."""

    before: ProjectSnapshot
    after: PackageJson | None
    locked_after: dict[DependencyName, VersionString]

    @classmethod
    def of(cls, before: ProjectSnapshot, listed_after: LockedVersions) -> ProjectUpdate:
        """Return the update of the project, reading its package.json again where the update needs it.

        A project that was unreadable before the update, and whose versions did not move, is not read again.
        """
        locked_after = listed_after.get(before.listed, {})
        if before.package_json is None and not _moved_versions(before.locked, locked_after):
            return cls(before, None, locked_after)
        return cls(
            before,
            PackageJson.readable(before.path, partial(_LOG.unreadable_file, content=FileContent.DEPENDENCY_LINES)),
            locked_after,
        )

    @property
    def moved_versions(self) -> dict[DependencyName, VersionString]:
        """Return each version that differs from the locked one."""
        return _moved_versions(self.before.locked, self.locked_after)

    def log_moved_versions(self, manager: str, catalogs: Catalogs, reported: set[Location]) -> None:
        """Log each moved version, up or back, at the lines declaring its dependency, or else at the package.json.

        Skip the locations already in `reported`, and add the ones logged, so that a catalog entry several projects
        take their version from is logged once.
        """
        declarations = {} if self.after is None else self.after.dependency_references(catalogs)
        for package, new_version in self.moved_versions.items():
            current = self.before.locked[package]
            declared = declarations.get(package, [Reference(package, "", Location(self.before.path))])
            moved = [replace(each, current_version=current) for each in declared if each.location not in reported]
            reported.update(each.location for each in moved)
            if SEMVER.is_downgrade(current, new_version):
                for reference in moved:
                    _LOG.version_moved_back(manager, reference, new_version)
                continue
            releases = {name: npmjs.SOURCE.release(name, new_version) for name in {each.dependency for each in moved}}
            for reference in moved:
                _LOG.new_version(reference, releases[reference.dependency])

    def log_range_rewrites(self, manager: str) -> None:
        """Log each rewrite of a range in the package.json, for a dependency with an unmoved locked version."""
        if self.before.package_json is None or self.after is None:
            return
        ranges_after = self.after.declared_ranges()
        dependencies = {key: reference.dependency for key, reference in ranges_after.items()}
        versions_before = _versions_of(dependencies, self.before.locked)
        versions_after = _versions_of(dependencies, self.locked_after)
        ranges_before = self.before.package_json.declared_ranges()
        _log_range_rewrites(manager, ranges_before, ranges_after, versions_before, versions_after)


def log_updates(
    manager: str, updates: list[ProjectUpdate], catalogs_before: Catalogs, catalogs_after: Catalogs
) -> None:
    """Log each moved version and each rewritten range, in the projects and in the catalogs."""
    reported: set[Location] = set()
    for update in updates:
        update.log_moved_versions(manager, catalogs_after, reported)
        update.log_range_rewrites(manager)
    _log_catalog_range_rewrites(manager, catalogs_before, catalogs_after, updates)


def _versions_of[KeyT](
    dependencies: dict[KeyT, DependencyName], locked: dict[DependencyName, VersionString]
) -> dict[KeyT, VersionString]:
    """Return the locked version of each dependency the lock records, keyed as the dependency is."""
    return {key: locked[dependency] for key, dependency in dependencies.items() if dependency in locked}


def _log_catalog_range_rewrites(
    manager: str, catalogs_before: Catalogs, catalogs_after: Catalogs, updates: list[ProjectUpdate]
) -> None:
    """Log each rewrite of a range in the catalogs, for an entry with an unmoved locked version.

    An entry's locked version is the version that each project taking its version from the entry locks.
    """
    versions_before: dict[CatalogEntry, VersionString] = {}
    versions_after: dict[CatalogEntry, VersionString] = {}
    for update in updates:
        entries = [] if update.after is None else update.after.catalog_dependencies()
        dependencies = {entry: entry.dependency for entry in entries}
        versions_before |= _versions_of(dependencies, update.before.locked)
        versions_after |= _versions_of(dependencies, update.locked_after)
    ranges_before, ranges_after = catalogs_before.ranges(), catalogs_after.ranges()
    _log_range_rewrites(manager, ranges_before, ranges_after, versions_before, versions_after)


def _log_range_rewrites[KeyT](
    manager: str,
    ranges_before: dict[KeyT, Reference],
    ranges_after: dict[KeyT, Reference],
    versions_before: dict[KeyT, VersionString],
    versions_after: dict[KeyT, VersionString],
) -> None:
    """Log each rewritten range of a dependency that the lock records at an unmoved version."""
    moved_versions = _moved_versions(versions_before, versions_after)
    for key, range_after in ranges_after.items():
        range_before = ranges_before.get(key)
        if range_before is None or range_before.current_version == range_after.current_version:
            continue
        if key not in versions_after or key in moved_versions:
            continue
        reference = Reference(range_after.dependency, range_before.current_version, range_after.location)
        _LOG.range_rewrite(manager, reference, range_after.current_version, versions_before.get(key, ""))


def _moved_versions[KeyT](
    locked: dict[KeyT, VersionString], versions: dict[KeyT, VersionString]
) -> dict[KeyT, VersionString]:
    """Return each version that differs from the locked one.

    A package that the lock file did not record before the update counts as unmoved.
    """
    return {package: version for package, version in versions.items() if locked.get(package, version) != version}

"""Update package.json dependencies with the project's Node package manager (npm or pnpm), and its marker policy."""

import os
import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency import DependencyVersion
from update_time.domain.reference import Reference
from update_time.io.log import get_logger
from update_time.io.process import run
from update_time.manifests.package_json import DEPENDENCY_SECTIONS, PackageJson
from update_time.markers.directive import Reason
from update_time.markers.marker import Scope
from update_time.package_managers.policy import ManagerPolicy
from update_time.primitives.command import Command
from update_time.primitives.location import Location
from update_time.primitives.lookup import LookedUp
from update_time.sources.npmjs import get_changes, get_publication_datetime

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.manifests.package_json import Declaration

_LOG = get_logger("package.json")
# Files that signal which package manager a project uses, checked when there is no corepack `packageManager` field.
# pnpm is handled like npm; yarn and bun are detected only to be skipped (see `SUPPORTED_MANAGERS`). npm's
# `package-lock.json` is deliberately absent: npm is the default when nothing else matches, so listing it would be a
# no-op, and leaving it out lets a real non-npm lockfile win over a stale `package-lock.json` left by a migration.
_MANAGER_FILES = {"pnpm-lock.yaml": "pnpm", "pnpm-workspace.yaml": "pnpm", "yarn.lock": "yarn", "bun.lockb": "bun"}
_COMMON_NPM_OPTIONS = ["--include=dev", "--silent"]

# pnpm measures its cooldown (`minimumReleaseAge`) in minutes, while Update-time's --cooldown option is in days.
_MINUTES_PER_DAY = 24 * 60

# The path of the workspace root relative to itself. A list command reports the dependencies of each project at the
# project's path relative to the workspace root.
_WORKSPACE_ROOT = PurePosixPath()


def _versions(dependencies: dict) -> dict[DependencyName, VersionString]:
    """Return the version of each dependency that the lock file records."""
    return {package: info["version"] for package, info in dependencies.items() if "version" in info}


def _npm_locked_versions(listed: dict | list) -> dict[PurePosixPath, dict[DependencyName, VersionString]]:
    """Return the locked top-level versions from `npm list --json` output, per project.

    In a workspace, npm lists each member as a dependency of the root, with the member's dependencies nested in its
    entry. The entry resolves the member to its directory, relative to the folder holding the member's link: the
    root's `node_modules`, or `node_modules/@scope` for a member named `@scope/b`.
    """
    dependencies = listed.get("dependencies", {}) if isinstance(listed, dict) else {}
    projects = {_WORKSPACE_ROOT: _versions(dependencies)}
    for name, info in dependencies.items():
        if (resolved := info.get("resolved", "")).startswith("file:"):
            link_folder = posixpath.dirname(f"node_modules/{name}")
            member = posixpath.normpath(posixpath.join(link_folder, resolved.removeprefix("file:")))
            projects[PurePosixPath(member)] = _versions(info.get("dependencies", {}))
    return projects


def _pnpm_locked_versions(listed: dict | list) -> dict[PurePosixPath, dict[DependencyName, VersionString]]:
    """Return the locked top-level versions from `pnpm list --json` output.

    pnpm reports a list of projects (one per workspace package, so just this project here) and splits its dependencies
    over `dependencies`, `devDependencies`, and `optionalDependencies`, unlike npm's single `dependencies` object.
    """
    versions: dict[DependencyName, VersionString] = {}
    for project in listed if isinstance(listed, list) else []:
        for section in DEPENDENCY_SECTIONS:
            versions |= _versions(project.get(section, {}))
    return {_WORKSPACE_ROOT: versions}


@dataclass(frozen=True)
class _PackageManager:
    """A supported Node package manager and the commands Update-time runs for it.

    The executables, flags, and lockfiles differ across managers, as does the cooldown option (npm measures its
    `min-release-age` in days, pnpm its `minimumReleaseAge` in minutes).
    """

    name: str
    update: Command
    list_locked: Command
    config_get: Command
    prefix: Command | None
    cooldown_config_keys: tuple[str, ...]
    cooldown_unset: str
    cooldown_option: Callable[[int], str]
    locked_versions: Callable[[dict | list], dict[PurePosixPath, dict[DependencyName, VersionString]]]

    def _workspace_root(self, directory: Path) -> Path:
        """Return the root of the workspace the project in the directory belongs to, or else the directory itself.

        The manager reads its config in the workspace root, and its list command reports projects relative to it.
        """
        if self.prefix is not None and (reported := run(self.prefix, cwd=directory).stdout.strip()):
            return Path(reported)
        return directory

    def _cooldown_options(self, directory: Path) -> list[str]:
        """Return the option that applies Update-time's cooldown, or nothing if the project configures its own.

        `<manager> config get` reports the effective value from the whole config cascade (an unset key reads back as
        the manager's own sentinel), so a project that sets its own cutoff wins and we add nothing. When the manager
        fails to report a key, the key counts as unset.
        """
        results = (run(Command(*self.config_get, key), cwd=directory) for key in self.cooldown_config_keys)
        if any(result.succeeded and result.stdout.strip() != self.cooldown_unset for result in results):
            return []
        return [self.cooldown_option(COOLDOWN.get())]

    def _locked_in(self, directory: Path) -> dict[PurePosixPath, dict[DependencyName, VersionString]]:
        """Return the top-level versions the lock file in the directory records, per project."""
        return self.locked_versions(run(self.list_locked, directory).json)

    def update_package_json(self, package_json: PackageJson) -> PackageJson | None:
        """Update the package.json and its lockfile using this package manager, and log what it moved and rewrote.

        The update moves the dependencies of the package.json's project, and those of the workspace members below.
        """
        path = package_json.path
        _LOG.path(path)
        directory = path.parent
        workspace_root = self._workspace_root(directory)
        project = PurePosixPath(os.path.relpath(directory.resolve(), workspace_root.resolve()))
        cooldown = self._cooldown_options(workspace_root)
        listed_before = self._locked_in(directory)
        snapshots = [_ProjectSnapshot(project, path, package_json, listed_before.get(project, {}))]
        for member in listed_before:
            if _is_below(member, project):
                member_path = directory / member.relative_to(project) / "package.json"
                member_json = PackageJson.readable(member_path)
                snapshots.append(_ProjectSnapshot(member, member_path, member_json, listed_before[member]))
        run(Command(*self.update, *cooldown), cwd=directory)
        listed_after = self._locked_in(directory)
        updates = [_ProjectUpdate.of(snapshot, listed_after) for snapshot in snapshots]
        for update in updates:
            update.log_new_versions()
            update.log_range_rewrites(self.name)
        return updates[0].after  # The package.json of the project in the directory, as the update left it


def _is_below(listed: PurePosixPath, project: PurePosixPath) -> bool:
    """Return whether the listed project is a workspace member below the project."""
    return listed != project and listed.is_relative_to(project)


@dataclass(frozen=True)
class _ProjectSnapshot:
    """The package.json and the locked versions of a project, as they were before the update."""

    listed: PurePosixPath
    path: Path
    package_json: PackageJson | None
    locked: dict[DependencyName, VersionString]


@dataclass(frozen=True)
class _ProjectUpdate:
    """The snapshot of a project from before the update, with its package.json and locked versions after it."""

    before: _ProjectSnapshot
    after: PackageJson | None
    locked_after: dict[DependencyName, VersionString]

    @classmethod
    def of(
        cls, before: _ProjectSnapshot, listed_after: dict[PurePosixPath, dict[DependencyName, VersionString]]
    ) -> _ProjectUpdate:
        """Return the update of the project, reading its package.json again where the update needs it.

        A project that was unreadable before the update, and whose versions did not move, is not read again.
        """
        locked_after = listed_after.get(before.listed, {})
        if before.package_json is None and not _moved_versions(before.locked, locked_after):
            return cls(before, None, locked_after)
        return cls(before, PackageJson.readable(before.path, _LOG.unreadable_package_json), locked_after)

    @property
    def moved_versions(self) -> dict[DependencyName, VersionString]:
        """Return each version that differs from the locked one."""
        return _moved_versions(self.before.locked, self.locked_after)

    def log_new_versions(self) -> None:
        """Log each moved version at the lines declaring its dependency, or at the package.json where none does."""
        declarations = {} if self.after is None else self.after.dependency_locations()
        for package, new_version in self.moved_versions.items():
            current = self.before.locked[package]
            changes = get_changes(package, new_version)
            published = get_publication_datetime(package, new_version)
            package_version = DependencyVersion(new_version, changes, publication=LookedUp(published))
            for location in declarations.get(package, [Location(self.before.path)]):
                _LOG.new_version(Reference(package, current, location), package_version)

    def log_range_rewrites(self, manager: str) -> None:
        """Log each rewrite of a range in the package.json, for a dependency with an unmoved locked version."""
        if self.before.package_json is None or self.after is None:
            return
        ranges_before = self.before.package_json.declared_ranges()
        moved_versions = self.moved_versions
        for key, range_after in self.after.declared_ranges().items():
            range_before = ranges_before.get(key)
            if range_before is None or range_before.current_version == range_after.current_version:
                continue
            if range_after.dependency not in self.locked_after or range_after.dependency in moved_versions:
                continue
            reference = Reference(range_after.dependency, range_before.current_version, range_after.location)
            version_before = self.before.locked.get(range_after.dependency, "")
            _LOG.range_rewrite(manager, reference, range_after.current_version, version_before)


def _moved_versions(
    locked: dict[DependencyName, VersionString], versions: dict[DependencyName, VersionString]
) -> dict[DependencyName, VersionString]:
    """Return each version that differs from the locked one.

    A package that the lock file did not record before the update counts as unmoved.
    """
    return {package: version for package, version in versions.items() if locked.get(package, version) != version}


_NPM = _PackageManager(
    name="npm",
    update=Command("npm", "update", "--save", *_COMMON_NPM_OPTIONS),
    list_locked=Command("npm", "list", "--json", "--depth=0", *_COMMON_NPM_OPTIONS, "--package-lock-only"),
    config_get=Command("npm", "config", "get"),
    # Inside a workspace member, `npm prefix` reports the workspace root. `npm config get` refuses to run inside the
    # member, and `npm update` reads the config of the workspace root.
    prefix=Command("npm", "prefix"),
    # npm config keys that hold back fresh releases. If the project sets either, we leave its cooldown alone (and
    # `min-release-age` can't be combined with `before`, so a project-level `before` also means we add nothing).
    cooldown_config_keys=("min-release-age", "before"),
    cooldown_unset="null",  # `npm config get` reports an unset key as `null`.
    cooldown_option=lambda days: f"--min-release-age={days}",
    locked_versions=_npm_locked_versions,
)
_PNPM = _PackageManager(
    name="pnpm",
    # Deliberately without `--latest`, which would cross the version ranges declared in package.json; without it,
    # pnpm stays within the declared ranges, like `npm update` does.
    update=Command("pnpm", "update"),
    list_locked=Command("pnpm", "list", "--json", "--depth=0", "--lockfile-only"),
    config_get=Command("pnpm", "config", "get"),
    prefix=None,  # `pnpm config get` reads the workspace root's config inside a workspace member as well.
    cooldown_config_keys=("minimumReleaseAge",),
    cooldown_unset="undefined",  # `pnpm config get` reports an unset key as `undefined` (not its built-in default).
    cooldown_option=lambda days: f"--config.minimumReleaseAge={days * _MINUTES_PER_DAY}",
    locked_versions=_pnpm_locked_versions,
)
# The managers Update-time can update, keyed by name. The updater resolves a detected manager name against this
# catalog and skips a project whose manager isn't here (yarn, bun) rather than mishandling it by running npm (which
# would write a stray package-lock.json and ignore the real lock).
SUPPORTED_MANAGERS = {manager.name: manager for manager in (_NPM, _PNPM)}


def _unchecked_scopes(declaration: Declaration) -> dict[Scope, Reason]:
    """Return each warning scope that Update-time leaves unchecked for the declaration, with the reason.

    The npm registry does not signal archival, so that scope is always among them. Where the declaration does not
    name a release, Update-time leaves every warning scope unchecked.
    """
    if declaration.names_a_release:
        return {Scope.ARCHIVED: Reason.NO_ARCHIVAL_SIGNAL}
    return ManagerPolicy.every_warning_scope(Reason.NO_NPM_RELEASE)


# npm and pnpm move a dependency within the range its package.json declares, whatever a marker says about it.
POLICY = ManagerPolicy(honours_a_freeze=False, unchecked_scopes=_unchecked_scopes)


def manager_name(package_json: PackageJson) -> str:
    """Return the name of the project's package manager (npm, pnpm, yarn, bun), defaulting to npm.

    The nearest directory that names a manager decides, from the package.json's own upwards, so a workspace member
    takes the manager of its workspace root. In each directory, the corepack `packageManager` field of its
    package.json (e.g. `"pnpm@9.15.0"`) wins over a lockfile or a workspace file.
    """
    directory = package_json.path.parent
    if manager := package_json.declared_manager() or _signalled_manager(directory):
        return manager
    for ancestor in directory.parents:
        if manager := _declared_manager_in(ancestor) or _signalled_manager(ancestor):
            return manager
    return "npm"


def _declared_manager_in(directory: Path) -> str:
    """Return the name of the manager that the package.json in the directory names, if any."""
    package_json = directory / "package.json"
    if not package_json.exists():
        return ""
    readable = PackageJson.readable(package_json, _LOG.unreadable_manager_declaration)
    return "" if readable is None else readable.declared_manager()


def _signalled_manager(directory: Path) -> str:
    """Return the name of the manager that a lockfile or a workspace file in the directory signals, if any."""
    return next((manager for name, manager in _MANAGER_FILES.items() if (directory / name).exists()), "")

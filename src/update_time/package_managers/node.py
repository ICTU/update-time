"""Update package.json dependencies with the project's package manager (npm, pnpm, or bun), and its marker policy."""

import os
import posixpath
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.directive import Reason
from update_time.domain.marker import Scope
from update_time.domain.reference import Reference
from update_time.domain.version_scheme import SEMVER
from update_time.formats import toml
from update_time.io.filesystem import is_scanned
from update_time.io.log import get_logger
from update_time.io.process import is_installed, run
from update_time.lock_files import bun_lock
from update_time.manifests.npm_declaration import Catalogs
from update_time.manifests.package_json import DEPENDENCY_SECTIONS, PackageJson
from update_time.manifests.pnpm_workspace_yaml import PnpmWorkspaceYaml
from update_time.package_managers.policy import ManagerPolicy
from update_time.primitives.command import Command
from update_time.primitives.location import Location
from update_time.primitives.timestamp import MINUTES_PER_DAY, SECONDS_PER_DAY
from update_time.sources import npmjs

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.manifests.npm_declaration import CatalogEntry, Declaration


_LOG = get_logger("package.json")
_BUN_LOCK = "bun.lock"  # The lock bun writes as text, which Update-time reads
_BUN_BINARY_LOCK = "bun.lockb"  # The lock bun wrote before version 1.2, and still writes where a project asks for it
# The setting under `[install]` in a bunfig.toml that holds bun's cooldown.
_BUNFIG_COOLDOWN = "minimumReleaseAge"
# Why a bunfig.toml whose `install` is not a table cannot be read for the cooldown it sets.
_INSTALL_NOT_A_TABLE = "its install is not a table"
_COMMON_NPM_OPTIONS = ["--include=dev", "--silent"]
_NPM_LIST = Command("npm", "list", "--json", "--depth=0", *_COMMON_NPM_OPTIONS, "--package-lock-only")
_PNPM_LIST = Command("pnpm", "list", "--json", "--depth=0", "--lockfile-only")

# The path of the root project relative to the workspace root. The locked versions of each project are keyed by the
# project's path relative to the workspace root, as the list commands of npm and pnpm report them and bun's lock
# records them.
_ROOT_PROJECT = Path()

# The file that marks the root of a pnpm workspace.
_PNPM_WORKSPACE_FILE = "pnpm-workspace.yaml"

# The top-level versions of each project that a lock file records, keyed by the path of the project.
type _LockedVersions = dict[Path, dict[DependencyName, VersionString]]


def _versions(dependencies: dict) -> dict[DependencyName, VersionString]:
    """Return the version of each dependency that the lock file records."""
    return {package: info["version"] for package, info in dependencies.items() if "version" in info}


def _npm_locked_versions(directory: Path) -> _LockedVersions:
    """Return the top-level versions that `npm list` reports in the directory, per project.

    In a workspace, npm lists each member as a dependency of the root, with the member's dependencies nested in its
    entry. The entry resolves the member to its directory, relative to the folder holding the member's link: the
    root's `node_modules`, or `node_modules/@scope` for a member named `@scope/b`.
    """
    listed = run(_NPM_LIST, directory).json
    dependencies = listed.get("dependencies", {}) if isinstance(listed, dict) else {}
    projects = {_ROOT_PROJECT: _versions(dependencies)}
    for name, info in dependencies.items():
        if (resolved := info.get("resolved", "")).startswith("file:"):
            link_folder = posixpath.dirname(f"node_modules/{name}")
            member = posixpath.normpath(posixpath.join(link_folder, resolved.removeprefix("file:")))
            projects[Path(member)] = _versions(info.get("dependencies", {}))
    return projects


def _pnpm_locked_versions(directory: Path) -> _LockedVersions:
    """Return the top-level versions that `pnpm list` reports in the directory, per project.

    At the root of a workspace, pnpm lists each project of the workspace, naming the project's directory as its `path`.
    A project listed without one is the project in the directory. pnpm splits the dependencies of a project over
    `dependencies`, `devDependencies`, and `optionalDependencies`, unlike npm's single `dependencies` object.
    """
    listed = run(_PNPM_LIST, directory).json
    projects: _LockedVersions = {}
    for project in listed if isinstance(listed, list) else []:
        path = project.get("path")
        listed_path = Path(os.path.relpath(path, directory.resolve())) if isinstance(path, str) else _ROOT_PROJECT
        versions: dict[DependencyName, VersionString] = {}
        for section in DEPENDENCY_SECTIONS:
            versions |= _versions(project.get(section, {}))
        projects[listed_path] = versions
    return projects


def _npm_workspace_root(directory: Path) -> Path:
    """Return the workspace root that `npm prefix` reports for the project in the directory, or else the directory."""
    reported = run(Command("npm", "prefix"), cwd=directory).stdout.strip()
    return Path(reported) if reported else directory


def _bun_workspace_root(directory: Path) -> Path:
    """Return the nearest ancestor whose package.json declares the directory a workspace member, else the directory.

    bun keeps one lock at the workspace root, and locks a project outside a workspace in the project's own directory.
    """
    return next((ancestor for ancestor in directory.parents if _declares_member(ancestor, directory)), directory)


def _pnpm_workspace_root(directory: Path) -> Path:
    """Return the nearest directory, from the directory upwards, that holds a pnpm-workspace.yaml, else the directory.

    pnpm locks a project outside a workspace in the project's own directory.
    """
    roots = (ancestor for ancestor in (directory, *directory.parents) if (ancestor / _PNPM_WORKSPACE_FILE).exists())
    return next(roots, directory)


def _declares_member(root: Path, directory: Path) -> bool:
    """Return whether the package.json in the root declares the directory a member of its workspace.

    A pattern that starts with `!` takes out the directories it matches, wherever it sits among the patterns.
    """
    if (readable := PackageJson.readable(root / "package.json")) is None:
        return False
    member = directory.relative_to(root)
    patterns = readable.workspace_patterns()
    excluded = any(member.full_match(pattern.removeprefix("!")) for pattern in patterns if pattern.startswith("!"))
    return not excluded and any(member.full_match(pattern) for pattern in patterns if not pattern.startswith("!"))


def _bun_locked_versions(directory: Path) -> _LockedVersions:
    """Return the top-level versions that the bun.lock in the directory records, or warn why it can't."""
    lock = directory / _BUN_LOCK
    try:
        return bun_lock.locked_versions(lock)
    except (OSError, ValueError) as error:
        _LOG.unreadable_bun_lock(lock, error)
        return {}


def _bun_cooldown_setting(directory: Path) -> str | None:
    """Return the setting that holds bun's cooldown where the bunfig.toml in the directory, or the global one, sets it.

    bun does not have a command that reports its config, so Update-time reads the files bun reads.
    """
    if _bunfig_sets_a_cooldown(directory / "bunfig.toml") or _global_bunfig_sets_a_cooldown():
        return _BUNFIG_COOLDOWN
    return None


@cache
def _global_bunfig_sets_a_cooldown() -> bool:
    """Return whether the global bunfig.toml sets a cooldown, reading it once per run.

    bun reads it in XDG_CONFIG_HOME where that holds an absolute path, and in the home directory otherwise.
    """
    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    global_directory = xdg_config_home if xdg_config_home.is_absolute() else Path.home()
    return _bunfig_sets_a_cooldown(global_directory / ".bunfig.toml")


def _bunfig_sets_a_cooldown(bunfig: Path) -> bool:
    """Return whether the bunfig.toml sets a cooldown, warning where it exists and cannot be read."""
    try:
        install = toml.load(bunfig).get("install", {})
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as error:
        _LOG.unreadable_bunfig(bunfig, error)
        return False
    if not isinstance(install, dict):
        _LOG.unreadable_bunfig(bunfig, ValueError(_INSTALL_NOT_A_TABLE))
        return False
    return _BUNFIG_COOLDOWN in install


@dataclass(frozen=True)
class _ConfiguredCooldown:
    """The config keys that hold a manager's cooldown, and the command that reads them."""

    config_get: Command
    keys: tuple[str, ...]
    unset: str  # What the command reports for a key that is not set

    def setting(self, directory: Path) -> str | None:
        """Return the first of the keys that the config in the directory sets, if any.

        `<manager> config get` reports the effective value from the whole config cascade, so a key that the project or
        the user sets counts. When the manager fails to report a key, the key counts as unset.
        """
        for key in self.keys:
            result = run(Command(*self.config_get, key), cwd=directory)
            if result.succeeded and result.stdout.strip() != self.unset:
                return key
        return None


@dataclass(frozen=True)
class _Workspace:
    """A workspace of a Node package manager, and its root, where the workspace's lock and catalogs sit.

    A project outside a workspace is a workspace of its own, rooted in the project's directory.
    """

    manager: _PackageManager
    root: Path

    def has_a_lock(self) -> bool:
        """Return whether a lock file that Update-time reads sits at the root."""
        return any((self.root / name).exists() for name in self.manager.lock_files)

    def catalogs(self) -> Catalogs:
        """Return the catalogs the file at the root holds, or none where the manager has no catalogs."""
        read = self.manager.catalogs
        return Catalogs() if read is None else read(self.root)

    def root_file(self) -> Path:
        """Return the file at the root that names the members: the manager's workspace file, else the package.json."""
        return self.root / (self.manager.workspace_file or "package.json")

    def policy_for_a_project_left_as_it_is(self, path: Path) -> ManagerPolicy[Declaration] | None:
        """Return the policy for the markers of a package.json the manager does not run for, or None where it runs.

        The manager runs only where the scan reaches the root and a lock file that Update-time reads sits there,
        since the update would otherwise write one, or leave its versions unread.
        """
        manager = self.manager
        if not is_scanned(self.root):
            _LOG.workspace_root_outside_the_scan(path, manager.name, self.root_file())
            return _WORKSPACE_ROOT_OUTSIDE_THE_SCAN_POLICY
        if self.has_a_lock():
            return None
        lock = self.root / manager.lock_files[0]
        if manager.binary_lock is not None and (binary_lock := self.root / manager.binary_lock).exists():
            _LOG.binary_lock(path, binary_lock, lock)
            return _BINARY_LOCK_POLICY
        _LOG.no_lock_file(path, manager.name, lock)
        return _NO_LOCK_FILE_POLICY

    def update(self, package_json: PackageJson | None) -> PackageJson | None:
        """Update the workspace's projects at its root, log what moved, and return the root's package.json as left.

        A pnpm workspace root can hold a pnpm-workspace.yaml without a package.json.
        """
        manager = self.manager
        path = self.root_file() if package_json is None else package_json.path
        directory = path.parent
        cooldown = manager.cooldown_options(path)
        listed_before = manager.locked_versions(directory)
        catalogs_before = self.catalogs()
        root_path = directory / "package.json"
        snapshots = [_ProjectSnapshot(_ROOT_PROJECT, root_path, package_json, listed_before.get(_ROOT_PROJECT, {}))]
        for member, locked in listed_before.items():
            if member != _ROOT_PROJECT:
                member_path = directory / member / "package.json"
                snapshots.append(_ProjectSnapshot(member, member_path, PackageJson.readable(member_path), locked))
        run(Command(*manager.update, *cooldown), cwd=directory)
        listed_after = manager.locked_versions(directory)
        updates = [_ProjectUpdate.of(snapshot, listed_after) for snapshot in snapshots]
        _log_updates(manager.name, updates, catalogs_before, self.catalogs())
        return updates[0].after


@dataclass(frozen=True)
class _PackageManager:
    """A supported Node package manager, the commands Update-time runs for it, and how Update-time reads its lock.

    The executables, flags, lock files, cooldown options, and the file holding the catalogs differ across managers.
    """

    name: str
    update: Command
    find_workspace_root: Callable[[Path], Path]  # Find the root of the workspace the project in a directory belongs to
    lock_files: tuple[str, ...]
    binary_lock: str | None  # Update-time cannot read this lock, which the manager may write instead
    workspace_file: str | None
    cooldown_setting: Callable[[Path], str | None]  # The setting that holds the project's own cooldown, if one does
    cooldown_option: Callable[[int], str]
    locked_versions: Callable[[Path], _LockedVersions]
    catalogs: Callable[[Path], Catalogs] | None  # Read the catalogs that the file at the workspace root holds

    def files(self) -> tuple[str, ...]:
        """Return the names of the files that name this manager: its locks, and its workspace file if it has one."""
        return tuple(name for name in (*self.lock_files, self.binary_lock, self.workspace_file) if name is not None)

    def roots_a_workspace(self, package_json: PackageJson) -> bool:
        """Return whether the package.json roots a workspace whose members the manager updates from it.

        A manager with a workspace file of its own takes the members from that file, beside the package.json, and does
        not read the package.json `workspaces` field. pnpm creates its file from that field instead.
        """
        if self.workspace_file is None:
            return package_json.declares_workspaces()
        return (package_json.path.parent / self.workspace_file).exists()

    def checked_catalogs(self, package_json: PackageJson) -> Catalogs:
        """Return the catalogs of the package.json's workspace, or none where the scan does not reach its root."""
        if self.catalogs is None:
            return Catalogs()
        workspace = self.workspace_of(package_json.path.parent)
        return workspace.catalogs() if is_scanned(workspace.root) else Catalogs()

    def workspace_of(self, directory: Path) -> _Workspace:
        """Return the workspace that the project in the directory belongs to."""
        return _Workspace(self, self.find_workspace_root(directory))

    def cooldown_options(self, path: Path) -> list[str]:
        """Return the option that applies Update-time's cooldown, or log the project's own and return nothing."""
        if (setting := self.cooldown_setting(path.parent)) is None:
            return [self.cooldown_option(COOLDOWN.get())]
        _LOG.project_cooldown(path, f"{self.name} {setting}", COOLDOWN.get())
        return []

    def update_package_json(self, package_json: PackageJson) -> CheckedPackageJson | None:
        """Update the package.json and its lockfile using this package manager, and log what it moved and rewrote.

        The update moves the dependencies of the workspace's members as well, so a member is left to the run for its
        workspace root. Without the package manager on the path, the package.json gets an error and stays as it is.
        """
        path = package_json.path
        if not is_installed(executable := self.update.executable):
            _LOG.package_manager_not_on_path(path, executable)
            return CheckedPackageJson(package_json, MARKER_POLICY)
        directory = path.parent
        workspace = self.workspace_of(directory)
        if (policy := workspace.policy_for_a_project_left_as_it_is(path)) is not None:
            return CheckedPackageJson(package_json, policy)
        if workspace.root.resolve() != directory.resolve():
            _LOG.updated_by_the_workspace_root(path, self.name, workspace.root_file())
            return CheckedPackageJson(package_json, MARKER_POLICY)
        after = workspace.update(package_json)
        return None if after is None else CheckedPackageJson(after, MARKER_POLICY)


@dataclass(frozen=True)
class _ProjectSnapshot:
    """The package.json and the locked versions of a project, as they were before the update."""

    listed: Path
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
    def of(cls, before: _ProjectSnapshot, listed_after: _LockedVersions) -> _ProjectUpdate:
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

    def log_moved_versions(self, manager: str, catalogs: Catalogs, reported: set[Location]) -> None:
        """Log each moved version, up or back, at the lines declaring its dependency, or else at the package.json.

        Skip the locations already in `reported`, and add the ones logged, so that a catalog entry several projects
        take their version from is logged once.
        """
        declarations = {} if self.after is None else self.after.dependency_locations(catalogs)
        for package, new_version in self.moved_versions.items():
            current = self.before.locked[package]
            declared = declarations.get(package, [Location(self.before.path)])
            locations = [location for location in declared if location not in reported]
            reported.update(locations)
            if SEMVER.is_downgrade(current, new_version):
                for location in locations:
                    _LOG.version_moved_back(manager, Reference(package, current, location), new_version)
                continue
            package_version = npmjs.SOURCE.release(package, new_version)
            for location in locations:
                _LOG.new_version(Reference(package, current, location), package_version)

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


def _log_updates(
    manager: str, updates: list[_ProjectUpdate], catalogs_before: Catalogs, catalogs_after: Catalogs
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
    manager: str, catalogs_before: Catalogs, catalogs_after: Catalogs, updates: list[_ProjectUpdate]
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


def _catalogs_of(catalog_file: PackageJson | PnpmWorkspaceYaml | None) -> Catalogs:
    """Return the catalogs the file holds, or none where the file could not be read."""
    return Catalogs() if catalog_file is None else catalog_file.catalogs()


_NPM = _PackageManager(
    name="npm",
    update=Command("npm", "update", "--save", *_COMMON_NPM_OPTIONS),
    # Inside a workspace member, `npm prefix` reports the workspace root, and the run there updates the member too.
    find_workspace_root=_npm_workspace_root,
    lock_files=("package-lock.json", "npm-shrinkwrap.json"),  # The first is the one npm writes where neither exists.
    binary_lock=None,
    workspace_file=None,
    cooldown_setting=_ConfiguredCooldown(
        Command("npm", "config", "get"),
        # npm config keys that hold back fresh releases. If the project sets either, we leave its cooldown alone (and
        # `min-release-age` can't be combined with `before`, so a project-level `before` also means we add nothing).
        keys=("min-release-age", "before"),
        unset="null",
    ).setting,
    cooldown_option=lambda days: f"--min-release-age={days}",
    locked_versions=_npm_locked_versions,
    catalogs=None,  # npm has no catalogs
)
_PNPM = _PackageManager(
    name="pnpm",
    # `-r` updates every project of the workspace, and rewrites the ranges of its catalogs, from the root. Deliberately
    # without `--latest`, which would cross the version ranges declared in package.json; without it, pnpm stays within
    # the declared ranges, like `npm update` does.
    update=Command("pnpm", "update", "-r"),
    find_workspace_root=_pnpm_workspace_root,
    lock_files=("pnpm-lock.yaml",),
    binary_lock=None,
    workspace_file=_PNPM_WORKSPACE_FILE,
    cooldown_setting=_ConfiguredCooldown(
        Command("pnpm", "config", "get"),
        keys=("minimumReleaseAge",),
        unset="undefined",  # Rather than pnpm's built-in default
    ).setting,
    cooldown_option=lambda days: f"--config.minimumReleaseAge={days * MINUTES_PER_DAY}",
    locked_versions=_pnpm_locked_versions,
    catalogs=lambda root: _catalogs_of(PnpmWorkspaceYaml.readable(root / _PNPM_WORKSPACE_FILE)),
)
_BUN = _PackageManager(
    name="bun",
    # `-r` updates every workspace, from the root and from a member alike, as `npm update` does. Deliberately without
    # `--latest`, which would cross the version ranges declared in package.json.
    update=Command("bun", "update", "-r"),
    find_workspace_root=_bun_workspace_root,
    lock_files=(_BUN_LOCK,),
    binary_lock=_BUN_BINARY_LOCK,
    workspace_file=None,
    cooldown_setting=_bun_cooldown_setting,
    cooldown_option=lambda days: f"--minimum-release-age={days * SECONDS_PER_DAY}",
    locked_versions=_bun_locked_versions,
    catalogs=lambda root: _catalogs_of(PackageJson.readable(root / "package.json")),
)
# The managers Update-time can update, keyed by name. The updater resolves a detected manager name against this
# catalog and skips a project whose manager isn't here (yarn) rather than mishandling it by running npm (which
# would write a stray package-lock.json and ignore the real lock).
SUPPORTED_MANAGERS = {manager.name: manager for manager in (_NPM, _PNPM, _BUN)}
# The files that name a project's package manager. A corepack `packageManager` field wins over them, and npm is the
# default. yarn is named only to be skipped.
_MANAGER_FILES = {name: manager.name for manager in SUPPORTED_MANAGERS.values() for name in manager.files()} | {
    "yarn.lock": "yarn"
}


def _unchecked_scopes(declaration: Declaration) -> dict[Scope, Reason]:
    """Return each warning scope that Update-time leaves unchecked for the declaration, with the reason.

    The npm registry does not signal archival, so that scope is always among them. Where the declaration does not
    name a release, Update-time leaves every warning scope unchecked. A version that a catalog names is checked at
    the catalog's entry instead.
    """
    if declaration.names_a_release:
        return {Scope.ARCHIVED: Reason.NO_ARCHIVAL_SIGNAL}
    if declaration.catalog is not None:
        return ManagerPolicy.every_warning_scope(Reason.CHECKED_AT_THE_CATALOG)
    return ManagerPolicy.every_warning_scope(Reason.NO_NPM_RELEASE)


# npm, pnpm, and bun move a dependency within the range its package.json declares, whatever a marker says about it.
MARKER_POLICY = ManagerPolicy(
    hold_back_redundancy=lambda _declaration: Reason.MANAGER_RESOLVES_THE_VERSION,
    bound_redundancy=lambda _declaration: Reason.BOUND_DECIDES_NOTHING,
    cooldown_redundancy=Reason.COOLDOWN_PER_RUN,
    unchecked_scopes=_unchecked_scopes,
)

# The policy for the markers of a package.json whose project does not have a lock file.
_NO_LOCK_FILE_POLICY = MARKER_POLICY.policy_for_files_left_as_they_are(
    Reason.NO_LOCK_FILE_LEFT, Reason.NO_LOCK_FILE_KEPT
)

# The policy for the markers of a package.json whose project is locked in a binary lock that Update-time cannot read.
_BINARY_LOCK_POLICY = MARKER_POLICY.policy_for_files_left_as_they_are(Reason.BINARY_LOCK_LEFT, Reason.BINARY_LOCK_KEPT)

# The policy for the markers of a package.json whose project's files name more than one package manager.
MORE_THAN_ONE_MANAGER_POLICY = MARKER_POLICY.policy_for_files_left_as_they_are(
    Reason.MORE_THAN_ONE_MANAGER_LEFT, Reason.MORE_THAN_ONE_MANAGER_KEPT
)

# The policy for the markers of a package.json whose workspace root lies outside the scan root.
_WORKSPACE_ROOT_OUTSIDE_THE_SCAN_POLICY = MARKER_POLICY.policy_for_files_left_as_they_are(
    Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_LEFT, Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_KEPT
)


def update_pnpm_workspace(workspace_file: Path) -> ManagerPolicy[Declaration]:
    """Update the pnpm workspace the file roots, and return the policy for the markers of the file's catalogs.

    The run for the package.json at the root updates a workspace whose root holds one. Each member reports a pnpm that
    is not on the path.
    """
    policy = _catalog_policy(workspace_file)
    workspace = _PNPM.workspace_of(workspace_file.parent)
    if (
        policy is MARKER_POLICY
        and not (workspace.root / "package.json").exists()
        and is_installed(_PNPM.update.executable)
    ):
        workspace.update(None)
    return policy


def _catalog_policy(workspace_file: Path) -> ManagerPolicy[Declaration]:
    """Return the policy for the markers of a pnpm-workspace.yaml, by whether pnpm runs for the workspace it roots."""
    directory = workspace_file.parent
    choice = _choice_in(directory, _declared_manager_in(directory))
    if choice is not None and choice.names_more_than_one_manager():
        return MORE_THAN_ONE_MANAGER_POLICY
    if not _PNPM.workspace_of(directory).has_a_lock():
        return _NO_LOCK_FILE_POLICY
    return MARKER_POLICY


@dataclass(frozen=True)
class CheckedPackageJson:
    """A package.json to check, as its package manager left it, with the policy its markers are judged by."""

    package_json: PackageJson
    policy: ManagerPolicy[Declaration]

    def judged_by(self, catalogs: Catalogs) -> CheckedPackageJson:
        """Return the package.json, with a policy checking a `catalog:` dependency where the catalogs hold its entry."""
        policy = self.policy

        def unchecked_scopes(declaration: Declaration) -> Mapping[Scope, Reason]:
            """Return the scopes the policy leaves unchecked, or every one where the catalogs do not hold the entry."""
            if declaration.catalog is not None and catalogs.location_of(declaration) is None:
                return ManagerPolicy.every_warning_scope(Reason.CATALOG_ENTRY_NOT_FOUND)
            return policy.unchecked_scopes(declaration)

        return CheckedPackageJson(self.package_json, replace(policy, unchecked_scopes=unchecked_scopes))


@dataclass(frozen=True)
class _ManagerChoice:
    """The name of a project's package manager, and the files in the directory that named it."""

    name: str
    files: tuple[Path, ...] = ()

    def names_more_than_one_manager(self) -> bool:
        """Return whether the files name more than one package manager."""
        return len({_MANAGER_FILES[file.name] for file in self.files}) > 1


def manager_choice(package_json: PackageJson) -> _ManagerChoice:
    """Return the project's package manager (npm, pnpm, yarn, bun), defaulting to npm, and the files that named it.

    The nearest directory that names a manager decides, from the package.json's own upwards, so a workspace member
    takes the manager of its workspace root. In each directory, the corepack `packageManager` field of its
    package.json (e.g. `"pnpm@9.15.0"`) wins over a lockfile or a workspace file.
    """
    directory = package_json.path.parent
    if choice := _choice_in(directory, package_json.declared_manager()):
        return choice
    for ancestor in directory.parents:
        if choice := _choice_in(ancestor, _declared_manager_in(ancestor)):
            return choice
    return _ManagerChoice("npm")


def _declared_manager_in(directory: Path) -> str:
    """Return the name of the manager that the package.json in the directory names, if any."""
    package_json = directory / "package.json"
    if not package_json.exists():
        return ""
    readable = PackageJson.readable(package_json, _LOG.unreadable_manager_declaration)
    return "" if readable is None else readable.declared_manager()


def _choice_in(directory: Path, declared_manager: str) -> _ManagerChoice | None:
    """Return the manager the package.json in the directory declares, or else the first its files name, if any."""
    if declared_manager:
        return _ManagerChoice(declared_manager)
    files = tuple(directory / name for name in _MANAGER_FILES if (directory / name).exists())
    return _ManagerChoice(_MANAGER_FILES[files[0].name], files) if files else None

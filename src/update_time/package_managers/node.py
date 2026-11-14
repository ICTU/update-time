"""Update package.json dependencies with the project's package manager (npm, pnpm, or bun), and its marker policy."""

import os
import posixpath
from dataclasses import dataclass
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
from update_time.manifests.package_json import DEPENDENCY_SECTIONS, PackageJson
from update_time.package_managers.policy import ManagerPolicy
from update_time.primitives.command import Command
from update_time.primitives.location import Location
from update_time.primitives.timestamp import MINUTES_PER_DAY, SECONDS_PER_DAY
from update_time.sources import npmjs

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.manifests.package_json import Declaration

_LOG = get_logger("package.json")
_BUN_LOCK = "bun.lock"  # The lock bun writes as text, which Update-time reads
_BUN_BINARY_LOCK = "bun.lockb"  # The lock bun wrote before version 1.2, and still writes where a project asks for it
# Why a bunfig.toml whose `install` is not a table cannot be read for the cooldown it sets.
_INSTALL_NOT_A_TABLE = "its install is not a table"
# Files that signal which package manager a project uses, checked when there is no corepack `packageManager` field.
# pnpm and bun are handled like npm; yarn is detected only to be skipped (see `SUPPORTED_MANAGERS`). npm's
# `package-lock.json` is deliberately absent: npm is the default when nothing else matches, so listing it would be a
# no-op, and leaving it out lets a real non-npm lockfile win over a stale `package-lock.json` left by a migration.
_MANAGER_FILES = {
    "pnpm-lock.yaml": "pnpm",
    "pnpm-workspace.yaml": "pnpm",
    "yarn.lock": "yarn",
    _BUN_LOCK: "bun",
    _BUN_BINARY_LOCK: "bun",
}
_COMMON_NPM_OPTIONS = ["--include=dev", "--silent"]
_NPM_LIST = Command("npm", "list", "--json", "--depth=0", *_COMMON_NPM_OPTIONS, "--package-lock-only")
_PNPM_LIST = Command("pnpm", "list", "--json", "--depth=0", "--lockfile-only")

# The path of the workspace root relative to itself. The locked versions of each project are keyed by the project's
# path relative to the workspace root, as npm's list command reports them and bun's lock records them.
_WORKSPACE_ROOT = Path()

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
    projects = {_WORKSPACE_ROOT: _versions(dependencies)}
    for name, info in dependencies.items():
        if (resolved := info.get("resolved", "")).startswith("file:"):
            link_folder = posixpath.dirname(f"node_modules/{name}")
            member = posixpath.normpath(posixpath.join(link_folder, resolved.removeprefix("file:")))
            projects[Path(member)] = _versions(info.get("dependencies", {}))
    return projects


def _pnpm_locked_versions(directory: Path) -> _LockedVersions:
    """Return the top-level versions that `pnpm list` reports in the directory.

    pnpm reports a list of projects (one per workspace package, so just this project here) and splits its dependencies
    over `dependencies`, `devDependencies`, and `optionalDependencies`, unlike npm's single `dependencies` object.
    """
    listed = run(_PNPM_LIST, directory).json
    versions: dict[DependencyName, VersionString] = {}
    for project in listed if isinstance(listed, list) else []:
        for section in DEPENDENCY_SECTIONS:
            versions |= _versions(project.get(section, {}))
    return {_WORKSPACE_ROOT: versions}


def _npm_workspace_root(directory: Path) -> Path:
    """Return the workspace root that `npm prefix` reports for the project in the directory, or else the directory."""
    reported = run(Command("npm", "prefix"), cwd=directory).stdout.strip()
    return Path(reported) if reported else directory


def _bun_workspace_root(directory: Path) -> Path:
    """Return the nearest ancestor whose package.json declares the directory a workspace member, else the directory.

    bun keeps one lock at the workspace root, and locks a project outside a workspace in the project's own directory.
    """
    return next((ancestor for ancestor in directory.parents if _declares_member(ancestor, directory)), directory)


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


def _bun_sets_its_own_cooldown(directory: Path) -> bool:
    """Return whether the bunfig.toml in the directory, or the global one, sets a cooldown.

    bun does not have a command that reports its config, so Update-time reads the files bun reads.
    """
    return _bunfig_sets_a_cooldown(directory / "bunfig.toml") or _global_bunfig_sets_a_cooldown()


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
    return "minimumReleaseAge" in install


@dataclass(frozen=True)
class _ConfiguredCooldown:
    """The config keys that hold a manager's cooldown, and the command that reads them."""

    config_get: Command
    keys: tuple[str, ...]
    unset: str  # What the command reports for a key that is not set

    def is_set(self, directory: Path) -> bool:
        """Return whether the config in the directory sets one of the keys.

        `<manager> config get` reports the effective value from the whole config cascade, so a key that the project or
        the user sets counts. When the manager fails to report a key, the key counts as unset.
        """
        results = (run(Command(*self.config_get, key), cwd=directory) for key in self.keys)
        return any(result.succeeded and result.stdout.strip() != self.unset for result in results)


@dataclass(frozen=True)
class _PackageManager:
    """A supported Node package manager, the commands Update-time runs for it, and how Update-time reads its lock.

    The executables, flags, lock files, and cooldown options differ across managers.
    """

    name: str
    update: Command
    # Find the workspace root of the project in a directory. The manager runs there, for the members below as well.
    workspace_root: Callable[[Path], Path]
    lock_files: tuple[str, ...]
    binary_lock: str | None  # Update-time cannot read this lock, which the manager may write instead
    workspace_file: str | None
    reads_workspaces_field: bool
    sets_its_own_cooldown: Callable[[Path], bool]
    cooldown_option: Callable[[int], str]
    locked_versions: Callable[[Path], _LockedVersions]

    def _lock_root(self, directory: Path, workspace_root: Path) -> Path:
        """Return the root of the workspace whose lock file locks the project in the directory.

        For npm and bun, that root is the workspace root. pnpm marks it with its workspace file instead, and locks a
        project outside a workspace in the project's own directory.
        """
        if self.workspace_file is None:
            return workspace_root
        roots = (ancestor for ancestor in (directory, *directory.parents) if (ancestor / self.workspace_file).exists())
        return next(roots, directory)

    def _cooldown_options(self, directory: Path) -> list[str]:
        """Return the option that applies Update-time's cooldown, or nothing if the project configures its own."""
        return [] if self.sets_its_own_cooldown(directory) else [self.cooldown_option(COOLDOWN.get())]

    def _policy_for_a_project_left_as_it_is(self, path: Path, lock_root: Path) -> ManagerPolicy[Declaration] | None:
        """Return the policy for the markers of a package.json the manager does not run for, or None where it runs.

        The manager runs only where the scan reaches the lock root and a lock file that Update-time reads sits there,
        since the update would otherwise write one, or leave its versions unread.
        """
        if not is_scanned(lock_root):
            root = lock_root / (self.workspace_file or "package.json")
            _LOG.workspace_root_outside_the_scan(path, self.name, root)
            return _WORKSPACE_ROOT_OUTSIDE_THE_SCAN_POLICY
        if any((lock_root / name).exists() for name in self.lock_files):
            return None
        lock = lock_root / self.lock_files[0]
        if self.binary_lock is not None and (binary_lock := lock_root / self.binary_lock).exists():
            _LOG.binary_lock(path, binary_lock, lock)
            return _BINARY_LOCK_POLICY
        _LOG.no_lock_file(path, self.name, lock)
        return _NO_LOCK_FILE_POLICY

    def update_package_json(self, package_json: PackageJson) -> CheckedPackageJson | None:
        """Update the package.json and its lockfile using this package manager, and log what it moved and rewrote.

        The update moves the dependencies of the package.json's project, and those of the workspace members below, so
        a member is left to the run for its workspace root. Without the package manager on the path, the package.json
        gets an error and stays as it is.
        """
        path = package_json.path
        if not is_installed(executable := self.update.executable):
            _LOG.package_manager_not_on_path(path, executable)
            return CheckedPackageJson(package_json, MARKER_POLICY)
        directory = path.parent
        workspace_root = self.workspace_root(directory)
        lock_root = self._lock_root(directory, workspace_root)
        if (policy := self._policy_for_a_project_left_as_it_is(path, lock_root)) is not None:
            return CheckedPackageJson(package_json, policy)
        if workspace_root.resolve() != directory.resolve():
            _LOG.updated_by_the_workspace_root(path, self.name, workspace_root / "package.json")
            return CheckedPackageJson(package_json, MARKER_POLICY)
        cooldown = self._cooldown_options(directory)
        listed_before = self.locked_versions(directory)
        snapshots = [_ProjectSnapshot(_WORKSPACE_ROOT, path, package_json, listed_before.get(_WORKSPACE_ROOT, {}))]
        for member, locked in listed_before.items():
            if member != _WORKSPACE_ROOT:
                member_path = directory / member / "package.json"
                snapshots.append(_ProjectSnapshot(member, member_path, PackageJson.readable(member_path), locked))
        run(Command(*self.update, *cooldown), cwd=directory)
        listed_after = self.locked_versions(directory)
        updates = [_ProjectUpdate.of(snapshot, listed_after) for snapshot in snapshots]
        for update in updates:
            update.log_moved_versions(self.name)
            update.log_range_rewrites(self.name)
        after = updates[0].after  # The package.json of the project in the directory, as the update left it
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

    def log_moved_versions(self, manager: str) -> None:
        """Log each moved version, up or back, at the lines declaring its dependency, or else at the package.json."""
        declarations = {} if self.after is None else self.after.dependency_locations()
        for package, new_version in self.moved_versions.items():
            current = self.before.locked[package]
            locations = declarations.get(package, [Location(self.before.path)])
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
    # Inside a workspace member, `npm prefix` reports the workspace root, and the run there updates the member too.
    workspace_root=_npm_workspace_root,
    lock_files=("package-lock.json", "npm-shrinkwrap.json"),  # The first is the one npm writes where neither exists.
    binary_lock=None,
    workspace_file=None,
    reads_workspaces_field=True,  # npm takes the members of a workspace from the root package.json's `workspaces`.
    sets_its_own_cooldown=_ConfiguredCooldown(
        Command("npm", "config", "get"),
        # npm config keys that hold back fresh releases. If the project sets either, we leave its cooldown alone (and
        # `min-release-age` can't be combined with `before`, so a project-level `before` also means we add nothing).
        keys=("min-release-age", "before"),
        unset="null",
    ).is_set,
    cooldown_option=lambda days: f"--min-release-age={days}",
    locked_versions=_npm_locked_versions,
)
_PNPM = _PackageManager(
    name="pnpm",
    # Deliberately without `--latest`, which would cross the version ranges declared in package.json; without it,
    # pnpm stays within the declared ranges, like `npm update` does.
    update=Command("pnpm", "update"),
    # `pnpm config get` reads the workspace root's config inside a workspace member as well, and `pnpm list` reports
    # the project in the directory alone.
    workspace_root=lambda directory: directory,
    lock_files=("pnpm-lock.yaml",),
    binary_lock=None,
    workspace_file="pnpm-workspace.yaml",
    # pnpm takes the members of a workspace from pnpm-workspace.yaml, and creates that file from a `workspaces` field.
    reads_workspaces_field=False,
    sets_its_own_cooldown=_ConfiguredCooldown(
        Command("pnpm", "config", "get"),
        keys=("minimumReleaseAge",),
        unset="undefined",  # Rather than pnpm's built-in default
    ).is_set,
    cooldown_option=lambda days: f"--config.minimumReleaseAge={days * MINUTES_PER_DAY}",
    locked_versions=_pnpm_locked_versions,
)
_BUN = _PackageManager(
    name="bun",
    # `-r` updates every workspace, from the root and from a member alike, as `npm update` does. Deliberately without
    # `--latest`, which would cross the version ranges declared in package.json.
    update=Command("bun", "update", "-r"),
    workspace_root=_bun_workspace_root,
    lock_files=(_BUN_LOCK,),
    binary_lock=_BUN_BINARY_LOCK,
    workspace_file=None,
    reads_workspaces_field=True,  # bun takes the members of a workspace from the root package.json's `workspaces`.
    sets_its_own_cooldown=_bun_sets_its_own_cooldown,
    cooldown_option=lambda days: f"--minimum-release-age={days * SECONDS_PER_DAY}",
    locked_versions=_bun_locked_versions,
)
# The managers Update-time can update, keyed by name. The updater resolves a detected manager name against this
# catalog and skips a project whose manager isn't here (yarn) rather than mishandling it by running npm (which
# would write a stray package-lock.json and ignore the real lock).
SUPPORTED_MANAGERS = {manager.name: manager for manager in (_NPM, _PNPM, _BUN)}


def _unchecked_scopes(declaration: Declaration) -> dict[Scope, Reason]:
    """Return each warning scope that Update-time leaves unchecked for the declaration, with the reason.

    The npm registry does not signal archival, so that scope is always among them. Where the declaration does not
    name a release, Update-time leaves every warning scope unchecked.
    """
    if declaration.names_a_release:
        return {Scope.ARCHIVED: Reason.NO_ARCHIVAL_SIGNAL}
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

# The policy for the markers of a package.json whose workspace root lies outside the scan root.
_WORKSPACE_ROOT_OUTSIDE_THE_SCAN_POLICY = MARKER_POLICY.policy_for_files_left_as_they_are(
    Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_LEFT, Reason.WORKSPACE_ROOT_OUTSIDE_THE_SCAN_KEPT
)


@dataclass(frozen=True)
class CheckedPackageJson:
    """A package.json to check, as its package manager left it, with the policy its markers are judged by."""

    package_json: PackageJson
    policy: ManagerPolicy[Declaration]


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

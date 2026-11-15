"""Model a Node package manager and its workspace, and look up what the managers share: roots, cooldowns, catalogs."""

from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING

from update_time.domain.cooldown import COOLDOWN
from update_time.io.filesystem import is_scanned
from update_time.io.log import get_logger
from update_time.io.process import is_installed, run
from update_time.lock_files.locked_versions import ROOT_PROJECT
from update_time.manifests.npm_declaration import Catalogs
from update_time.manifests.package_json import PackageJson
from update_time.package_managers.node.marker_policy import (
    BINARY_LOCK_POLICY,
    MARKER_POLICY,
    NO_LOCK_FILE_POLICY,
    WORKSPACE_ROOT_OUTSIDE_THE_SCAN_POLICY,
    CheckedPackageJson,
)
from update_time.package_managers.node.project_update import ProjectSnapshot, ProjectUpdate, log_updates
from update_time.primitives.command import Command

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from update_time.domain.dependency import DependencyName
    from update_time.lock_files.locked_versions import LockedVersions
    from update_time.manifests.catalogs_yaml import CatalogsYaml
    from update_time.manifests.npm_declaration import Declaration
    from update_time.package_managers.policy import ManagerPolicy
    from update_time.primitives.command import Result


_LOG = get_logger("package.json")


@dataclass(frozen=True)
class CooldownArguments:
    """What a manager's update takes to apply Update-time's cooldown: options, or variables in its environment."""

    options: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ConfiguredCooldown:
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

    manager: PackageManager
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
        since the update would otherwise write one, or leave its versions unread. It also runs only where the version
        of the manager that runs at the root can update the project. The warning about a lock Update-time cannot read,
        or about the manager's version, names the root, so a member points at it.
        """
        manager = self.manager
        if not is_scanned(self.root):
            _LOG.workspace_root_outside_the_scan(path, manager.name, self.root_file())
            return WORKSPACE_ROOT_OUTSIDE_THE_SCAN_POLICY
        if self.has_a_lock():
            policy = _version_policy(self)
        elif manager.binary_lock is not None and (binary_lock := self.root / manager.binary_lock).exists():
            policy = _binary_lock_policy(self, binary_lock)
        else:
            _LOG.no_lock_file(path, manager.name, self.root / manager.lock_files[0])
            return NO_LOCK_FILE_POLICY
        if policy is not None and path.parent.resolve() != self.root.resolve():
            _LOG.left_with_the_workspace_root(path, manager.name, self.root_file())
        return policy

    def update(self, package_json: PackageJson | None) -> PackageJson | None:
        """Update the workspace's projects at its root, log what moved, and return the root's package.json as left.

        A pnpm workspace root can hold a pnpm-workspace.yaml without a package.json.
        """
        manager = self.manager
        path = self.root_file() if package_json is None else package_json.path
        directory = path.parent
        cooldown = manager.cooldown_for(path)
        listed_before = manager.locked_versions(directory)
        catalogs_before = self.catalogs()
        root_path = directory / "package.json"
        snapshots = [ProjectSnapshot(ROOT_PROJECT, root_path, package_json, listed_before.get(ROOT_PROJECT, {}))]
        for member, locked in listed_before.items():
            if member != ROOT_PROJECT:
                member_path = directory / member / "package.json"
                snapshots.append(ProjectSnapshot(member, member_path, PackageJson.readable(member_path), locked))
        if (packages := manager.updated_packages(listed_before)) is None:
            return package_json
        update = Command(*manager.update, *cooldown.options, *packages)
        manager.run_command(update, directory, cooldown.environment)
        listed_after = manager.locked_versions(directory)
        updates = [ProjectUpdate.of(snapshot, listed_after) for snapshot in snapshots]
        log_updates(manager.name, updates, catalogs_before, self.catalogs())
        return updates[0].after


@cache
def _version_policy(workspace: _Workspace) -> ManagerPolicy[Declaration] | None:
    """Return the policy for a workspace whose manager's version cannot update it, warning at its root once."""
    return workspace.manager.version_policy(workspace.root_file(), workspace.root)


@cache
def _binary_lock_policy(workspace: _Workspace, binary_lock: Path) -> ManagerPolicy[Declaration]:
    """Return the policy for a workspace locked in a binary lock, warning at its root once how to convert the lock."""
    _LOG.binary_lock(workspace.root_file(), binary_lock, workspace.root / workspace.manager.lock_files[0])
    return BINARY_LOCK_POLICY


@dataclass(frozen=True)
class PackageManager:
    """A supported Node package manager, the commands Update-time runs for it, and how Update-time reads its lock."""

    name: str
    update: Command
    find_workspace_root: Callable[[Path], Path]  # Find the root of the workspace the project in a directory belongs to
    lock_files: tuple[str, ...]
    binary_lock: str | None  # Update-time cannot read this lock, which the manager may write instead
    workspace_file: str | None
    cooldown_setting: Callable[[Path], str | None]  # The setting that holds the project's own cooldown, if one does
    cooldown_arguments: Callable[[int], CooldownArguments]
    locked_versions: Callable[[Path], LockedVersions]
    catalogs: Callable[[Path], Catalogs] | None  # Read the catalogs that the file at the workspace root holds
    # Name the packages to update, or return None where there are none. An update that names none updates them all.
    updated_packages: Callable[[LockedVersions], tuple[DependencyName, ...] | None] = lambda _locked: ()
    # The policy for a package.json whose manager, at the version that runs at the workspace root, cannot update it.
    version_policy: Callable[[Path, Path], ManagerPolicy[Declaration] | None] = lambda _path, _root: None
    # Run a command of the manager in a directory, with the variables added to its environment.
    run_command: Callable[[Command, Path, Mapping[str, str]], Result] = run

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

    def cooldown_for(self, path: Path) -> CooldownArguments:
        """Return the arguments that apply Update-time's cooldown, or log the project's own and return none."""
        if (setting := self.cooldown_setting(path.parent)) is None:
            return self.cooldown_arguments(COOLDOWN.get())
        _LOG.project_cooldown(path, f"{self.name} {setting}", COOLDOWN.get())
        return CooldownArguments()

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


def catalogs_of(catalog_file: PackageJson | CatalogsYaml | None) -> Catalogs:
    """Return the catalogs the file holds, or none where the file could not be read."""
    return Catalogs() if catalog_file is None else catalog_file.catalogs()


def package_json_workspace_root(directory: Path) -> Path:
    """Return the nearest ancestor whose package.json declares the directory a workspace member, else the directory."""
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

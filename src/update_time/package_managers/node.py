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
from update_time.manifests.package_json import DEPENDENCY_SECTIONS, dependency_locations
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

    def update_package_json(self, package_json: Path) -> None:
        """Update the package.json and its lockfile using this package manager, and log each version it moved.

        The update moves the dependencies of the project in the directory, and those of the workspace members below.
        """
        _LOG.path(package_json)
        directory = package_json.parent
        workspace_root = self._workspace_root(directory)
        project = PurePosixPath(os.path.relpath(directory.resolve(), workspace_root.resolve()))
        cooldown = self._cooldown_options(workspace_root)
        locked = self._locked_in(directory)
        run(Command(*self.update, *cooldown), cwd=directory)
        for listed, versions in self._locked_in(directory).items():
            if not listed.is_relative_to(project):
                continue  # A project above the one in the directory, such as the root of its workspace
            listed_package_json = directory / listed.relative_to(project) / "package.json"
            _log_new_versions(listed_package_json, locked.get(listed, {}), versions)


def _log_new_versions(
    package_json: Path, locked: dict[DependencyName, VersionString], versions: dict[DependencyName, VersionString]
) -> None:
    """Log each version that differs from the locked one, at the package.json that declares the dependency."""
    # A package that the lock file did not record before the update counts as unmoved:
    moved = {package: version for package, version in versions.items() if locked.get(package, version) != version}
    if not moved:
        return
    try:
        declarations = dependency_locations(package_json)
    except (OSError, ValueError) as error:
        _LOG.unreadable_package_json(package_json, error)
        declarations = {}
    for package, new_version in moved.items():
        current = locked[package]
        changes = get_changes(package, new_version)
        published = get_publication_datetime(package, new_version)
        package_version = DependencyVersion(new_version, changes, publication=LookedUp(published))
        for location in declarations.get(package, [Location(package_json)]):
            _LOG.new_version(Reference(package, current, location), package_version)


_NPM = _PackageManager(
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
SUPPORTED_MANAGERS = {"npm": _NPM, "pnpm": _PNPM}


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

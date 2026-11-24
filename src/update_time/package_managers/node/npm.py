"""npm: its commands, its cooldown settings, and how Update-time finds its workspace root and reads its lock."""

import posixpath
from pathlib import Path
from typing import TYPE_CHECKING, Final

from update_time.io.process import run
from update_time.lock_files.locked_versions import ROOT_PROJECT, listed_versions
from update_time.primitives.command import Command

from .package_manager import (
    ConfiguredCooldown,
    CooldownArguments,
    ManagerCooldown,
    PackageManager,
)

if TYPE_CHECKING:
    from update_time.lock_files.locked_versions import LockedVersions


_COMMON_NPM_OPTIONS: Final[tuple[str, ...]] = ("--include=dev", "--silent")
_NPM_LIST: Final[Command] = Command("npm", "list", "--json", "--depth=0", *_COMMON_NPM_OPTIONS, "--package-lock-only")


def _npm_locked_versions(directory: Path) -> LockedVersions:
    """Return the top-level versions that `npm list` reports in the directory, per project.

    In a workspace, npm lists each member as a dependency of the root, with the member's dependencies nested in its
    entry. The entry resolves the member to its directory, relative to the folder holding the member's link: the
    root's `node_modules`, or `node_modules/@scope` for a member named `@scope/b`.
    """
    listed = run(_NPM_LIST, directory).json
    dependencies = listed.get("dependencies", {}) if isinstance(listed, dict) else {}
    projects = {ROOT_PROJECT: listed_versions(dependencies)}
    for name, info in dependencies.items():
        if (resolved := info.get("resolved", "")).startswith("file:"):
            link_folder = posixpath.dirname(f"node_modules/{name}")
            member = posixpath.normpath(posixpath.join(link_folder, resolved.removeprefix("file:")))
            projects[Path(member)] = listed_versions(info.get("dependencies", {}))
    return projects


def _npm_workspace_root(directory: Path) -> Path:
    """Return the workspace root that `npm prefix` reports for the project in the directory, or else the directory."""
    reported = run(Command("npm", "prefix"), cwd=directory).stdout.strip()
    return Path(reported) if reported else directory


NPM: Final[PackageManager] = PackageManager(
    name="npm",
    update=Command("npm", "update", "--save", *_COMMON_NPM_OPTIONS),
    # Inside a workspace member, `npm prefix` reports the workspace root, and the run there updates the member too.
    find_workspace_root=_npm_workspace_root,
    lock_files=("package-lock.json", "npm-shrinkwrap.json"),  # The first is the one npm writes where neither exists.
    binary_lock=None,
    workspace_file=None,
    cooldown=ManagerCooldown(
        setting=ConfiguredCooldown(
            Command("npm", "config", "get"),
            # npm config keys that hold back fresh releases. If the project sets either, we leave its cooldown alone
            # (and `min-release-age` can't be combined with `before`, so a project-level `before` also means we add
            # nothing).
            keys=("min-release-age", "before"),
            unset="null",
        ).setting,
        arguments=lambda days: CooldownArguments((f"--min-release-age={days}",)),
    ),
    locked_versions=_npm_locked_versions,
    catalogs=None,  # npm has no catalogs
)

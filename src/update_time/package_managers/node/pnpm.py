"""pnpm: its commands, its cooldown setting, and how Update-time finds its workspace root, lock, and catalogs."""

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

from update_time.io.filesystem import nearest_path
from update_time.io.process import run
from update_time.lock_files.locked_versions import ROOT_PROJECT, listed_versions
from update_time.manifests.catalogs_yaml import CatalogsYaml
from update_time.manifests.package_json import DEPENDENCY_SECTIONS
from update_time.primitives.command import Command
from update_time.primitives.timestamp import MINUTES_PER_DAY

from .package_manager import (
    ConfiguredCooldown,
    CooldownArguments,
    ManagerCooldown,
    PackageManager,
    catalogs_of,
)

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.lock_files.locked_versions import LockedVersions


_PNPM_LIST: Final[Command] = Command("pnpm", "list", "--json", "--depth=0", "--lockfile-only")

# The file that marks the root of a pnpm workspace.
_PNPM_WORKSPACE_FILE: Final[str] = "pnpm-workspace.yaml"


def _pnpm_locked_versions(directory: Path) -> LockedVersions:
    """Return the top-level versions that `pnpm list` reports in the directory, per project.

    At the root of a workspace, pnpm lists each project of the workspace, naming the project's directory as its `path`.
    A project listed without one is the project in the directory. pnpm splits the dependencies of a project over
    `dependencies`, `devDependencies`, and `optionalDependencies`, unlike npm's single `dependencies` object.
    """
    listed = run(_PNPM_LIST, directory).json
    projects: LockedVersions = {}
    for project in listed if isinstance(listed, list) else []:
        path = project.get("path")
        listed_path = Path(os.path.relpath(path, directory.resolve())) if isinstance(path, str) else ROOT_PROJECT
        versions: dict[DependencyName, VersionString] = {}
        for section in DEPENDENCY_SECTIONS:
            versions |= listed_versions(project.get(section, {}))
        projects[listed_path] = versions
    return projects


def _pnpm_workspace_root(directory: Path) -> Path:
    """Return the nearest directory, from the directory upwards, that holds a pnpm-workspace.yaml, else the directory.

    pnpm locks a project outside a workspace in the project's own directory.
    """
    return root.parent if (root := nearest_path(directory, _PNPM_WORKSPACE_FILE)) else directory


PNPM: Final[PackageManager] = PackageManager(
    name="pnpm",
    # `-r` updates every project of the workspace, and rewrites the ranges of its catalogs, from the root. Deliberately
    # without `--latest`, which would cross the version ranges declared in package.json; without it, pnpm stays within
    # the declared ranges, like `npm update` does.
    update=Command("pnpm", "update", "-r"),
    find_workspace_root=_pnpm_workspace_root,
    lock_files=("pnpm-lock.yaml",),
    binary_lock=None,
    workspace_file=_PNPM_WORKSPACE_FILE,
    cooldown=ManagerCooldown(
        setting=ConfiguredCooldown(
            Command("pnpm", "config", "get"),
            keys=("minimumReleaseAge",),
            unset="undefined",  # Rather than pnpm's built-in default
        ).setting,
        arguments=lambda days: CooldownArguments((f"--config.minimumReleaseAge={days * MINUTES_PER_DAY}",)),
    ),
    locked_versions=_pnpm_locked_versions,
    catalogs=lambda root: catalogs_of(CatalogsYaml.readable(root / _PNPM_WORKSPACE_FILE)),
)

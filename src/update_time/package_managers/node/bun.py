"""bun: its commands, and how Update-time finds its workspace root, lock, catalogs, and cooldown."""

import os
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.formats import toml
from update_time.io.log import FileContent, get_logger
from update_time.lock_files import bun_lock
from update_time.manifests.package_json import PackageJson
from update_time.package_managers.node.package_manager import (
    CooldownArguments,
    PackageManager,
    catalogs_of,
    package_json_workspace_root,
)
from update_time.primitives.command import Command
from update_time.primitives.timestamp import SECONDS_PER_DAY

if TYPE_CHECKING:
    from update_time.lock_files.locked_versions import LockedVersions


_LOG = get_logger("package.json")
_BUN_LOCK = "bun.lock"  # The lock bun writes as text, which Update-time reads
_BUN_BINARY_LOCK = "bun.lockb"  # The lock bun wrote before version 1.2, and still writes where a project asks for it
# The setting under `[install]` in a bunfig.toml that holds bun's cooldown.
_BUNFIG_COOLDOWN = "minimumReleaseAge"
# Why a bunfig.toml whose `install` is not a table cannot be read for the cooldown it sets.
_INSTALL_NOT_A_TABLE = "its install is not a table"


def _bun_locked_versions(directory: Path) -> LockedVersions:
    """Return the top-level versions that the bun.lock in the directory records, or warn why it can't."""
    lock = directory / _BUN_LOCK
    try:
        return bun_lock.locked_versions(lock)
    except (OSError, ValueError) as error:
        _LOG.unreadable_file(lock, error, FileContent.LOCKED_VERSIONS)
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
        _LOG.unreadable_file(bunfig, error, FileContent.BUN_COOLDOWN)
        return False
    if not isinstance(install, dict):
        _LOG.unreadable_file(bunfig, ValueError(_INSTALL_NOT_A_TABLE), FileContent.BUN_COOLDOWN)
        return False
    return _BUNFIG_COOLDOWN in install


BUN = PackageManager(
    name="bun",
    # `-r` updates every workspace, from the root and from a member alike, as `npm update` does. Deliberately without
    # `--latest`, which would cross the version ranges declared in package.json.
    update=Command("bun", "update", "-r"),
    # bun keeps one lock at the workspace root, and locks a project outside a workspace in the project's own directory.
    find_workspace_root=package_json_workspace_root,
    lock_files=(_BUN_LOCK,),
    binary_lock=_BUN_BINARY_LOCK,
    workspace_file=None,
    cooldown_setting=_bun_cooldown_setting,
    cooldown_arguments=lambda days: CooldownArguments((f"--minimum-release-age={days * SECONDS_PER_DAY}",)),
    locked_versions=_bun_locked_versions,
    catalogs=lambda root: catalogs_of(PackageJson.readable(root / "package.json")),
)

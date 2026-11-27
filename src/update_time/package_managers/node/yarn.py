"""yarn: its commands, its cooldown, its version, and how Update-time reads its lock and catalogs."""

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Final

from update_time.io.log import get_logger
from update_time.io.process import run, tool_version
from update_time.manifests.catalogs_yaml import CatalogsYaml
from update_time.primitives.command import Command
from update_time.primitives.timestamp import MINUTES_PER_DAY

from .marker_policy import MARKER_POLICY, YARN_WITHOUT_A_COOLDOWN_POLICY
from .package_manager import (
    CooldownArguments,
    ManagerCooldown,
    PackageManager,
    catalogs_of,
    package_json_workspace_root,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from update_time.domain.dependency import DependencyName
    from update_time.io.log import Logger
    from update_time.lock_files.locked_versions import LockedVersions
    from update_time.manifests.npm_declaration import Declaration
    from update_time.package_managers.policy import ManagerPolicy
    from update_time.primitives.command import Result


_LOG: Final[Logger] = get_logger("package.json")
_YARN_INFO: Final[Command] = Command("yarn", "info", "--all", "--json")
_YARNRC: Final[str] = ".yarnrc.yml"  # The file at the workspace root that holds yarn's catalogs
_YARN_CONFIG: Final[Command] = Command("yarn", "config", "--json")
_FIRST_YARN_WITH_A_COOLDOWN: Final[tuple[int, ...]] = (4, 10)  # The first version of yarn that knows npmMinimalAgeGate
# yarn colours its output wherever COLORTERM is set, even where its output goes to a pipe.
_NO_COLOR: Final[dict[str, str]] = {"NO_COLOR": "1"}
_YARN_COOLDOWN: Final[str] = "npmMinimalAgeGate"  # The setting that holds yarn's cooldown
_UNSET: Final[str] = "<default>"  # Where `yarn config` reports a setting that nothing sets to come from
_WORKSPACE: Final[str] = (
    "@workspace:"  # What separates a workspace's name from its path, in the line yarn reports for it
)
# A locator holds a registry version after `@npm:`. yarn gives a dependency with peer dependencies a virtual locator,
# which holds the version after `#npm:`.
_REGISTRY_VERSION: Final[re.Pattern[str]] = re.compile(r"[@#]npm:(?P<version>[^@#:]+)$")
# The locator of a built-in patch holds its version after `::version=`. yarn applies such a patch to typescript, say.
_BUILTIN_PATCH_VERSION: Final[re.Pattern[str]] = re.compile(r"#optional!builtin<.*::version=(?P<version>[^&]+)")


def _run_yarn(command: Command, directory: Path, environment: Mapping[str, str] | None = None) -> Result:
    """Run a yarn command without colour, and report what yarn printed where it failed.

    yarn reports an error on stdout, and `run` reports a failed command's stderr alone.
    """
    result = run(command, directory, _NO_COLOR | dict(environment or {}))
    if not result.succeeded and result.stdout:
        _LOG.command_failed(command, result.stdout)
    return result


def _yarn_locked_versions(directory: Path) -> LockedVersions:
    """Return the top-level versions that `yarn info` reports in the directory, per workspace.

    yarn reports a line per package, and names a workspace `name@workspace:path` in its line. That line names each
    dependency by its descriptor. It resolves the dependency to a locator, and leaves the dependencies out where the
    workspace declares none. The descriptor starts with the name the workspace declares, which an alias holds. A
    dependency on a workspace, a file, or a patch of the project's own resolves to a locator without a registry version.
    """
    listing = _run_yarn(_YARN_INFO, directory)
    if not listing.succeeded:
        return {}
    projects: LockedVersions = {}
    for line in listing.stdout.splitlines():
        package = json.loads(line)
        _name, workspace, path = package["value"].partition(_WORKSPACE)
        if workspace:
            dependencies = package["children"].get("Dependencies", [])
            versions = (
                (_declared_name(each["descriptor"]), _registry_version(each["locator"])) for each in dependencies
            )
            projects[Path(path)] = {name: version for name, version in versions if version is not None}
    return projects


def _declared_name(descriptor: str) -> str:
    """Return the name that a descriptor, `name@range`, gives the dependency, the scope of a scoped name included."""
    return descriptor[: descriptor.index("@", 1)]


def _registry_version(locator: str) -> str | None:
    """Return the registry version that the locator resolves a dependency to, or None where it resolves to another."""
    match = _REGISTRY_VERSION.search(locator) or _BUILTIN_PATCH_VERSION.search(locator)
    return None if match is None else match["version"]


def _every_locked_package(locked: LockedVersions) -> tuple[DependencyName, ...] | None:
    """Return the name of each package the lock records, in any of its projects, or None where it records none."""
    return tuple(sorted({package for versions in locked.values() for package in versions})) or None


def _yarn_cooldown_setting(directory: Path) -> str | None:
    """Return the setting that holds yarn's cooldown where a file or the environment sets it, warning where unknown."""
    try:
        sources = _setting_sources(run(_YARN_CONFIG, directory, _NO_COLOR))
    except ValueError as error:
        _LOG.settings_unreadable(directory / "package.json", "yarn", _YARN_COOLDOWN, str(error))
        return None
    return None if sources.get(_YARN_COOLDOWN, _UNSET) == _UNSET else _YARN_COOLDOWN


def _setting_sources(config: Result) -> dict[str, str]:
    """Return where each setting that `yarn config` reports comes from, raising a `ValueError` where yarn failed.

    yarn reports a line per setting, and reports an error on stdout.
    """
    if not config.succeeded:
        raise ValueError(config.error or "yarn failed without saying why")
    return {setting["key"]: setting["source"] for setting in map(json.loads, config.stdout.splitlines())}


def _yarn_version_policy(path: Path, root: Path) -> ManagerPolicy[Declaration] | None:
    """Return the policy for a package.json that yarn cannot update, warning why, or None where yarn can.

    yarn classic hands a run over to the yarn that the `yarnPath` of a .yarnrc.yml names, so the version that runs
    is the one yarn reports at the workspace root. Where yarn does not report a version, yarn does not run.
    """
    if (version := tool_version("yarn", root)) is None:
        return MARKER_POLICY
    numbers = version.split(".")[:2]
    if not all(number.isdigit() for number in numbers):
        _LOG.tool_version_unknown("yarn", root, f"it printed {version}, which is not a version")
        return MARKER_POLICY
    if tuple(map(int, numbers)) >= _FIRST_YARN_WITH_A_COOLDOWN:
        return None
    _LOG.yarn_without_a_cooldown(path, root, version)
    return YARN_WITHOUT_A_COOLDOWN_POLICY


YARN: Final[PackageManager] = PackageManager(
    name="yarn",
    # `-R` resolves each package named again, within the ranges the workspaces declare, and leaves the ranges as they
    # are. Without it, `yarn up` moves a package to its latest version, and rewrites the ranges. `--mode` updates the
    # lock without installing.
    update=Command("yarn", "up", "-R", "--mode=update-lockfile"),
    find_workspace_root=package_json_workspace_root,
    lock_files=("yarn.lock",),
    binary_lock=None,
    workspace_file=None,
    cooldown=ManagerCooldown(
        setting=_yarn_cooldown_setting,
        arguments=lambda days: CooldownArguments(
            environment={"YARN_NPM_MINIMAL_AGE_GATE": str(days * MINUTES_PER_DAY)}
        ),
    ),
    locked_versions=_yarn_locked_versions,
    catalogs=lambda root: catalogs_of(CatalogsYaml.readable(root / _YARNRC)),
    updated_packages=_every_locked_package,
    version_policy=_yarn_version_policy,
    run_command=_run_yarn,
)

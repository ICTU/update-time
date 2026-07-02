"""Package.json updater script finds package.json files and updates dependencies to latest versions."""

import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from update_time.domain.cooldown import cooldown_days
from update_time.domain.version import DependencyVersion
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.io.process import run, run_json
from update_time.sources.npmjs import get_changes, get_publication_datetime

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

LOG = get_logger("package.json")
COMMON_NPM_OPTIONS = ["--include=dev", "--silent"]
# Lockfiles that signal which package manager a project uses, checked when there is no corepack `packageManager`
# field. pnpm is handled like npm; yarn and bun are not supported yet and are skipped (see SUPPORTED_MANAGERS). npm's
# `package-lock.json` is deliberately absent: npm is the default when nothing else matches, so listing it would be a
# no-op, and leaving it out lets a real non-npm lockfile win over a stale `package-lock.json` left by a migration.
LOCKFILES = {"pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "bun.lockb": "bun"}


def _npm_installed_versions(listed: dict | list) -> dict[str, str]:
    """Return the installed top-level versions from `npm list --json` output."""
    dependencies = listed.get("dependencies", {}) if isinstance(listed, dict) else {}
    return {package: info["version"] for package, info in dependencies.items() if "version" in info}


def _pnpm_installed_versions(listed: dict | list) -> dict[str, str]:
    """Return the installed top-level versions from `pnpm list --json` output.

    pnpm reports a list of projects (one per workspace package, so just the root here) and splits its dependencies
    over `dependencies`, `devDependencies`, and `optionalDependencies`, unlike npm's single `dependencies` object.
    """
    versions = {}
    for project in listed if isinstance(listed, list) else []:
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            for package, info in project.get(section, {}).items():
                if "version" in info:
                    versions[package] = info["version"]
    return versions


@dataclass(frozen=True)
class PackageManager:
    """A supported Node package manager and the commands Update-time runs for it.

    The three commands share the same shape across managers (detect outdated → update → read the installed
    versions), but the executables, flags, and lockfiles differ, as does the cooldown option (npm measures its
    `min-release-age` in days, pnpm its `minimumReleaseAge` in minutes).
    """

    outdated: list[str]
    update: list[str]
    installed: list[str]
    config_get: list[str]
    cooldown_config_keys: tuple[str, ...]
    cooldown_unset: str
    cooldown_option: Callable[[int], str]
    installed_versions: Callable[[dict | list], dict[str, str]]


NPM = PackageManager(
    outdated=["npm", "outdated", "--json", *COMMON_NPM_OPTIONS],
    update=["npm", "update", "--save", *COMMON_NPM_OPTIONS],
    installed=["npm", "list", "--json", "--depth=0", *COMMON_NPM_OPTIONS],
    config_get=["npm", "config", "get"],
    # npm config keys that hold back fresh releases. If the project sets either, we leave its cooldown alone (and
    # `min-release-age` can't be combined with `before`, so a project-level `before` also means we add nothing).
    cooldown_config_keys=("min-release-age", "before"),
    cooldown_unset="null",  # `npm config get` reports an unset key as `null`.
    cooldown_option=lambda days: f"--min-release-age={days}",
    installed_versions=_npm_installed_versions,
)
PNPM = PackageManager(
    outdated=["pnpm", "outdated", "--format", "json"],
    # `--latest` bumps the package.json ranges (not just the lockfile) to the newest release the cooldown allows.
    update=["pnpm", "update", "--latest"],
    installed=["pnpm", "list", "--json", "--depth=0"],
    config_get=["pnpm", "config", "get"],
    cooldown_config_keys=("minimumReleaseAge",),
    cooldown_unset="undefined",  # `pnpm config get` reports an unset key as `undefined` (not its built-in default).
    cooldown_option=lambda days: f"--config.minimumReleaseAge={days * 24 * 60}",  # pnpm measures the age in minutes.
    installed_versions=_pnpm_installed_versions,
)
# Package managers Update-time can update. Others (yarn, bun) are detected only to skip them with a clear warning
# rather than mishandle them by running npm (which would write a stray package-lock.json and ignore the real lock).
SUPPORTED_MANAGERS = {"npm": NPM, "pnpm": PNPM}


def package_manager(package_json: Path) -> str:
    """Return the name of the project's package manager (npm, pnpm, yarn, bun), defaulting to npm.

    The corepack `packageManager` field (e.g. `"pnpm@9.15.0"`) is authoritative; otherwise a sibling lockfile is
    used as a fallback signal.
    """
    if declared := json.loads(package_json.read_text()).get("packageManager", ""):
        return declared.split("@", maxsplit=1)[0]
    for lockfile, manager in LOCKFILES.items():
        if (package_json.parent / lockfile).exists():
            return manager
    return "npm"


def cooldown_options(manager: PackageManager, directory: Path) -> list[str]:
    """Return the option that applies Update-time's cooldown, or nothing if the project configures its own.

    `<manager> config get` reports the effective value from the whole config cascade (an unset key reads back as
    the manager's own sentinel), so a project that sets its own cutoff wins and we add nothing.
    """
    config = (run([*manager.config_get, key], cwd=directory).strip() for key in manager.cooldown_config_keys)
    if any(value != manager.cooldown_unset for value in config):
        return []
    return [manager.cooldown_option(cooldown_days())]


def update_package_json(package_json: Path) -> None:
    """Update the package.json and its lockfile using the project's package manager."""
    if (manager := SUPPORTED_MANAGERS.get(name := package_manager(package_json))) is None:
        LOG.unsupported_package_manager(package_json, name, "npm and pnpm")
        return
    LOG.path(package_json)
    original_contents = package_json.read_text()
    cooldown = cooldown_options(manager, package_json.parent)
    outdated = run_json([*manager.outdated, *cooldown], package_json.parent)
    outdated_packages = outdated if isinstance(outdated, dict) else {}
    run([*manager.update, *cooldown], cwd=package_json.parent)
    # The manager may install an older version than "latest" (e.g. when the cooldown holds back fresh releases), so
    # log the version that was actually installed rather than the latest one reported by the outdated command.
    installed = manager.installed_versions(run_json(manager.installed, package_json.parent))
    updated = False
    for package, version in outdated_packages.items():
        new_version = installed.get(package)
        if new_version is not None and new_version != version.get("current"):
            updated = True
            changes = get_changes(package, new_version)
            published = get_publication_datetime(package, new_version)
            package_version = DependencyVersion(new_version, changes, published=published)
            LOG.new_version(package, package_version, package_json)
    # The manager normalizes specs (e.g. npm rewrites git URLs to the github: shorthand) whenever it saves
    # package.json. When nothing was actually updated, restore the original manifest so reformatting doesn't
    # produce a spurious diff.
    if not updated and package_json.read_text() != original_contents:
        package_json.write_text(original_contents)


def update_package_jsons() -> int:
    """Find all package.json files and update them, including their lockfiles."""
    for package_json in glob("package.json"):
        update_package_json(package_json)
    return 0


def main() -> int:  # pragma: no cover
    """Update the dependencies in the repository's package.json files."""
    return update_package_jsons()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

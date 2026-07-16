"""Update package.json dependencies with the project's Node package manager (npm or pnpm)."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from update_time.domain.cooldown import cooldown_days
from update_time.domain.version import DependencyVersion
from update_time.file_formats.package_json import DEPENDENCY_SECTIONS
from update_time.io.log import get_logger
from update_time.io.process import run
from update_time.sources.npmjs import get_changes, get_publication_datetime

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

LOG = get_logger("package.json")
COMMON_NPM_OPTIONS = ["--include=dev", "--silent"]

# pnpm measures its cooldown (`minimumReleaseAge`) in minutes, while Update-time's --cooldown option is in days.
MINUTES_PER_DAY = 24 * 60


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
        for section in DEPENDENCY_SECTIONS:
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

    def cooldown_options(self, directory: Path) -> list[str]:
        """Return the option that applies Update-time's cooldown, or nothing if the project configures its own.

        `<manager> config get` reports the effective value from the whole config cascade (an unset key reads back as
        the manager's own sentinel), so a project that sets its own cutoff wins and we add nothing.
        """
        config = (run([*self.config_get, key], cwd=directory).stdout.strip() for key in self.cooldown_config_keys)
        if any(value != self.cooldown_unset for value in config):
            return []
        return [self.cooldown_option(cooldown_days())]

    def update_package_json(self, package_json: Path) -> None:
        """Update the package.json and its lockfile using this package manager."""
        LOG.path(package_json)
        original_contents = package_json.read_text()
        cooldown = self.cooldown_options(package_json.parent)
        outdated = run([*self.outdated, *cooldown], package_json.parent)
        if not outdated.ok:
            return  # The outdated check failed (e.g. the registry is unreachable); update and list would fail too.
        parsed = outdated.json
        outdated_packages = parsed if isinstance(parsed, dict) else {}
        run([*self.update, *cooldown], cwd=package_json.parent)
        # The manager may install an older version than "latest" (e.g. when the cooldown holds back fresh releases),
        # so log the version that was actually installed rather than the latest one reported by the outdated command.
        installed = self.installed_versions(run(self.installed, package_json.parent).json)
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
    cooldown_option=lambda days: f"--config.minimumReleaseAge={days * MINUTES_PER_DAY}",
    installed_versions=_pnpm_installed_versions,
)
# The managers Update-time can update, keyed by name. The updater resolves a detected manager name against this
# catalog and skips a project whose manager isn't here (yarn, bun) rather than mishandling it by running npm (which
# would write a stray package-lock.json and ignore the real lock).
SUPPORTED_MANAGERS = {"npm": NPM, "pnpm": PNPM}

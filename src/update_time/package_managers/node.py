"""Update package.json dependencies with the project's Node package manager (npm or pnpm), and its marker policy."""

from dataclasses import dataclass
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
    from pathlib import Path

    from update_time.domain.dependency import DependencyName, VersionString
    from update_time.manifests.package_json import Declaration

_LOG = get_logger("package.json")
_COMMON_NPM_OPTIONS = ["--include=dev", "--silent"]

# pnpm measures its cooldown (`minimumReleaseAge`) in minutes, while Update-time's --cooldown option is in days.
_MINUTES_PER_DAY = 24 * 60


def _npm_locked_versions(listed: dict | list) -> dict[DependencyName, VersionString]:
    """Return the locked top-level versions from `npm list --json` output."""
    dependencies = listed.get("dependencies", {}) if isinstance(listed, dict) else {}
    return {package: info["version"] for package, info in dependencies.items() if "version" in info}


def _pnpm_locked_versions(listed: dict | list) -> dict[DependencyName, VersionString]:
    """Return the locked top-level versions from `pnpm list --json` output.

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
class _PackageManager:
    """A supported Node package manager and the commands Update-time runs for it.

    The executables, flags, and lockfiles differ across managers, as does the cooldown option (npm measures its
    `min-release-age` in days, pnpm its `minimumReleaseAge` in minutes).
    """

    update: Command
    list_locked: Command
    config_get: Command
    cooldown_config_keys: tuple[str, ...]
    cooldown_unset: str
    cooldown_option: Callable[[int], str]
    locked_versions: Callable[[dict | list], dict[DependencyName, VersionString]]

    def cooldown_options(self, directory: Path) -> list[str]:
        """Return the option that applies Update-time's cooldown, or nothing if the project configures its own.

        `<manager> config get` reports the effective value from the whole config cascade (an unset key reads back as
        the manager's own sentinel), so a project that sets its own cutoff wins and we add nothing.
        """
        config = (
            run(Command(*self.config_get, key), cwd=directory).stdout.strip() for key in self.cooldown_config_keys
        )
        if any(value != self.cooldown_unset for value in config):
            return []
        return [self.cooldown_option(COOLDOWN.get())]

    def _locked_in(self, directory: Path) -> dict[DependencyName, VersionString]:
        """Return the top-level versions the lock file in the directory records."""
        return self.locked_versions(run(self.list_locked, directory).json)

    def update_package_json(self, package_json: Path) -> None:
        """Update the package.json and its lockfile using this package manager, and log each version it moved."""
        _LOG.path(package_json)
        cooldown = self.cooldown_options(package_json.parent)
        locked = self._locked_in(package_json.parent)
        run(Command(*self.update, *cooldown), cwd=package_json.parent)
        declarations = dependency_locations(package_json)
        for package, new_version in self._locked_in(package_json.parent).items():
            current = locked.get(package)
            if current is None or current == new_version:
                continue  # The lock file did not record the package before the update, or the update left it there
            changes = get_changes(package, new_version)
            published = get_publication_datetime(package, new_version)
            package_version = DependencyVersion(new_version, changes, publication=LookedUp(published))
            for location in declarations.get(package, [Location(package_json)]):
                _LOG.new_version(Reference(package, current, location), package_version)


_NPM = _PackageManager(
    update=Command("npm", "update", "--save", *_COMMON_NPM_OPTIONS),
    list_locked=Command("npm", "list", "--json", "--depth=0", *_COMMON_NPM_OPTIONS, "--package-lock-only"),
    config_get=Command("npm", "config", "get"),
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

"""Package.json updater script finds package.json files and updates dependencies to latest versions."""

import json
import sys
from typing import TYPE_CHECKING

from update_time.domain.cooldown import cooldown_days
from update_time.domain.version import DependencyVersion
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.io.process import run
from update_time.sources.npmjs import get_changes, get_publication_datetime

if TYPE_CHECKING:
    from pathlib import Path

LOG = get_logger("package.json")
COMMON_NPM_OPTIONS = ["--include=dev", "--silent"]
# npm config keys that hold back fresh releases. If the project sets either, we leave its cooldown alone (and
# `min-release-age` can't be combined with `before`, so a project-level `before` also means we add nothing).
NPM_COOLDOWN_CONFIG = ("min-release-age", "before")


def cooldown_options(directory: Path) -> list[str]:
    """Return the npm option that applies Update-time's cooldown, or nothing if the project configures its own.

    npm's `min-release-age` is measured in days, like our cooldown. `npm config get` reports the effective value
    from the whole .npmrc cascade (returning `null` when unset), so a project that sets its own cutoff wins.
    """
    if any(run(["npm", "config", "get", key], cwd=directory).strip() != "null" for key in NPM_COOLDOWN_CONFIG):
        return []
    return [f"--min-release-age={cooldown_days()}"]


def installed_versions(directory: Path) -> dict[str, str]:
    """Return the installed top-level dependency versions in the given directory."""
    npm_list = ["npm", "list", "--json", "--depth=0", *COMMON_NPM_OPTIONS]
    dependencies = json.loads(run(npm_list, cwd=directory)).get("dependencies", {})
    return {package: info["version"] for package, info in dependencies.items() if "version" in info}


def update_package_json(package_json: Path) -> int:
    """Update the package.json and package-lock.json."""
    LOG.path(package_json)
    original_contents = package_json.read_text()
    cooldown = cooldown_options(package_json.parent)
    npm_outdated = ["npm", "outdated", "--json", *COMMON_NPM_OPTIONS, *cooldown]
    outdated_packages = json.loads(run(npm_outdated, cwd=package_json.parent))
    npm_update = ["npm", "update", "--save", *COMMON_NPM_OPTIONS, *cooldown]
    run(npm_update, cwd=package_json.parent)
    # npm may install an older version than "latest" (e.g. when min-release-age holds back fresh releases), so log
    # the version that was actually installed rather than the latest one reported by npm outdated.
    installed = installed_versions(package_json.parent)
    updated = False
    for package, version in outdated_packages.items():
        new_version = installed.get(package)
        if new_version is not None and new_version != version.get("current"):
            updated = True
            changes = get_changes(package, new_version)
            published = get_publication_datetime(package, new_version)
            package_version = DependencyVersion(new_version, changes, published=published)
            LOG.new_version(package, package_version, package_json)
    # npm normalizes specs (e.g. git URLs to the github: shorthand) whenever it saves package.json. When nothing
    # was actually updated, restore the original manifest so that reformatting doesn't produce a spurious diff.
    if not updated and package_json.read_text() != original_contents:
        package_json.write_text(original_contents)
    return 0


def update_package_jsons() -> int:
    """Find all package.json files and update them, including the package-lock.json files."""
    results = {update_package_json(package_json) for package_json in glob("package.json")}
    return max(results, default=0)


def main() -> int:  # pragma: no cover
    """Update the dependencies in the repository's package.json files."""
    return update_package_jsons()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

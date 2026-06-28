"""Package.json updater script finds package.json files and updates dependencies to latest versions."""

import json
import sys
from typing import TYPE_CHECKING

from update_time.domain.version import DependencyVersion
from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.io.process import run
from update_time.sources.npmjs import get_changes, get_publication_datetime

if TYPE_CHECKING:
    from pathlib import Path

LOG = get_logger("package.json")
COMMON_NPM_OPTIONS = ["--include=dev", "--silent"]


def installed_versions(directory: Path) -> dict[str, str]:
    """Return the installed top-level dependency versions in the given directory."""
    npm_list = ["npm", "list", "--json", "--depth=0", *COMMON_NPM_OPTIONS]
    dependencies = json.loads(run(npm_list, cwd=directory)).get("dependencies", {})
    return {package: info["version"] for package, info in dependencies.items() if "version" in info}


def update_package_json(package_json: Path) -> int:
    """Update the package.json and package-lock.json."""
    LOG.path(package_json)
    original_contents = package_json.read_text()
    npm_outdated = ["npm", "outdated", "--json", *COMMON_NPM_OPTIONS]
    outdated_packages = json.loads(run(npm_outdated, cwd=package_json.parent))
    npm_update = ["npm", "update", "--save", *COMMON_NPM_OPTIONS]
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
            LOG.new_version(package, package_version)
    # npm normalizes specs (e.g. git URLs to the github: shorthand) whenever it saves package.json. When nothing
    # was actually updated, restore the original manifest so that reformatting doesn't produce a spurious diff.
    if not updated and package_json.read_text() != original_contents:
        package_json.write_text(original_contents)
    return 0


def update_package_jsons() -> int:
    """Find all package.json files and update them, including the package-lock.json files."""
    results = {update_package_json(package_json) for package_json in glob("package.json")}
    return max(results, default=0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(update_package_jsons())

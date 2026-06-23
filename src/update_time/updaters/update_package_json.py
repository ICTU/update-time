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


def update_package_json(package_json: Path) -> int:
    """Update the package.json and package-lock.json."""
    LOG.path(package_json)
    npm_outdated = ["npm", "outdated", "--silent", "--json", "--include=dev"]
    outdated_packages = json.loads(run(npm_outdated, cwd=package_json.parent))
    for package, version in outdated_packages.items():
        changes = get_changes(package, version["latest"])
        published = get_publication_datetime(package, version["latest"])
        package_version = DependencyVersion(version["latest"], changes, published=published)
        LOG.new_version(package, package_version)
    npm_update = ["npm", "update", "--save", "--silent", "--include=dev"]
    run(npm_update, cwd=package_json.parent)
    return 0


def update_package_jsons() -> int:
    """Find all package.json files and update them, including the package-lock.json files."""
    results = {update_package_json(package_json) for package_json in glob("package.json")}
    return max(results, default=0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(update_package_jsons())

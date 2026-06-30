"""Node engine updater script updates the Node engine in package.json files to the Node base image version.

Note: this script does not update package-lock.json.
"""

import json
import re
import sys
from typing import TYPE_CHECKING

from update_time.domain.version import DependencyVersion
from update_time.io.filesystem import glob, update_file
from update_time.io.log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

LOG = get_logger("node engine")
NODE_IMAGE_RE = r"FROM node:(?P<version>[\d\.]+)"
NODE_BASE_IMAGE_RE = r"FROM node:(?P<tag>\S+)"
NODE_ENGINE_RE = r'"(?P<dependency>node)": "(?P<version>[\d\.]+)"'


def has_node_engine(package_json: Path) -> bool:
    """Return whether the package.json file contains a Node engine."""
    package_json_contents = json.loads(package_json.read_text())
    return "engines" in package_json_contents and "node" in package_json_contents["engines"]


def _first_match(dockerfile: Path, regexp: str, group: str) -> str:
    """Return the named group of the first Node `FROM` line matching the regexp, or '' when there is none.

    A missing Dockerfile yields '' too, so callers can treat it the same as a Dockerfile without a Node base image.
    """
    if not dockerfile.exists():
        return ""
    for line in dockerfile.read_text().splitlines():
        if match := re.match(regexp, line):
            return match.group(group)
    return ""


def node_base_image_version(dockerfile: Path) -> str:
    """Return the numeric Node base image version (e.g. '22' from 'node:22-alpine').

    Returns an empty string if the Dockerfile is missing, has no Node base image, or its Node base image uses a
    non-numeric tag such as 'node:lts' (from which no concrete version can be derived).
    """
    return _first_match(dockerfile, NODE_IMAGE_RE, "version")


def node_base_image_tag(dockerfile: Path) -> str:
    """Return the tag of the Node base image (e.g. '22.1.0', '22-alpine' or 'lts'), or empty string if none."""
    return _first_match(dockerfile, NODE_BASE_IMAGE_RE, "tag")


def find_node_dockerfile(package_json: Path) -> Path:
    """Find the Dockerfile to derive the Node engine from.

    Prefers the Dockerfile next to the package.json; for package.json files without a local Node-base Dockerfile
    (e.g. docs/), falls back to any other Dockerfile in the repo. A Dockerfile with a numeric Node base image (the
    one we can actually sync to) is preferred over one whose Node base image uses a non-numeric tag like 'node:lts'.
    If no Node base image is found at all, returns the local Dockerfile path so the caller can log the error.
    """
    local_dockerfile = package_json.parent / "Dockerfile"
    candidates = [local_dockerfile, *glob("Dockerfile")]
    for dockerfile in candidates:
        if node_base_image_version(dockerfile):
            return dockerfile
    for dockerfile in candidates:
        if node_base_image_tag(dockerfile):
            return dockerfile
    return local_dockerfile


def update_node_engine(package_json: Path) -> int:
    """Update the Node engine version based on the Docker base image."""
    dockerfile = find_node_dockerfile(package_json)
    if version := node_base_image_version(dockerfile):
        return update_file(package_json, NODE_ENGINE_RE, lambda *_args: DependencyVersion(version=version), LOG)
    if tag := node_base_image_tag(dockerfile):
        # A Node base image exists but uses a non-numeric tag (e.g. 'node:lts'); we can't derive a concrete
        # version to sync the engine to, so skip without failing the run.
        LOG.non_numeric_node_base_image(dockerfile, tag)
        return 0
    LOG.expected_node_base_image(dockerfile)
    return 1


def update_node_engines() -> int:
    """Find all package.json files and update the Node engine."""
    results = {update_node_engine(pkg_json) for pkg_json in glob("package.json") if has_node_engine(pkg_json)}
    return max(results, default=0)


def main() -> int:  # pragma: no cover
    """Update the Node engines in the repository's package.json files."""
    return update_node_engines()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

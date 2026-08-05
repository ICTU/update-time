"""Node engine updater script updates the Node engine in package.json files to the Node base image version.

Note: this script does not update package-lock.json.
"""

from typing import TYPE_CHECKING

from update_time.domain.version import DependencyVersion
from update_time.file_formats import package_json as package_json_format
from update_time.io.filesystem import DOCKERFILE_GLOB_PATTERNS, DOCKERFILE_NAME, first_line_match, glob
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.oci import get_latest_tag

if TYPE_CHECKING:
    from pathlib import Path

_LOG = get_logger("node engine")
_NODE_IMAGE_RE = r"FROM node:(?P<version>[\d\.]+)"
_NODE_BASE_IMAGE_RE = r"FROM node:(?P<tag>\S+)"
_NODE_ENGINE_RE = r'"(?P<dependency>node)": "(?P<version>[\d\.]+)"'


def has_node_engine(package_json: Path) -> bool:
    """Return whether the package.json file contains a Node engine."""
    package_json_contents = package_json_format.read(package_json)
    return "engines" in package_json_contents and "node" in package_json_contents["engines"]


def node_base_image_version(dockerfile: Path) -> str:
    """Return the numeric Node base image version (e.g. '22' from 'node:22-alpine').

    Returns an empty string if the Dockerfile is missing, has no Node base image, or its Node base image uses a
    non-numeric tag such as 'node:lts' (from which no concrete version can be derived).
    """
    return first_line_match(dockerfile, _NODE_IMAGE_RE, "version")


def node_base_image_tag(dockerfile: Path) -> str:
    """Return the tag of the Node base image (e.g. '22.1.0', '22-alpine' or 'lts'), or empty string if none."""
    return first_line_match(dockerfile, _NODE_BASE_IMAGE_RE, "tag")


def find_node_dockerfile(package_json: Path) -> Path:
    """Find the Dockerfile to derive the Node engine from.

    Prefers the Dockerfile next to the package.json; for package.json files without a local Node-base Dockerfile
    (e.g. docs/), falls back to any other Dockerfile in the repo. A Dockerfile with a numeric Node base image (the
    one we can actually sync to) is preferred over one whose Node base image uses a non-numeric tag like 'node:lts'.
    If no Node base image is found at all, returns the local Dockerfile path; the caller finds no version on it and
    falls back to the latest Node release instead.
    """
    local_dockerfile = package_json.parent / DOCKERFILE_NAME
    candidates = [local_dockerfile, *glob(*DOCKERFILE_GLOB_PATTERNS)]
    for dockerfile in candidates:
        if node_base_image_version(dockerfile):
            return dockerfile
    for dockerfile in candidates:
        if node_base_image_tag(dockerfile):
            return dockerfile
    return local_dockerfile


def update_node_engine(package_json: Path) -> None:
    """Update the Node engine version to the Docker Node base image version, or the latest Node release."""
    dockerfile = find_node_dockerfile(package_json)
    if version := node_base_image_version(dockerfile):
        update_file(
            package_json,
            _NODE_ENGINE_RE,
            get_new_version=lambda *_args: DependencyVersion(version=version),
            logger=_LOG,
        )
        return
    if tag := node_base_image_tag(dockerfile):
        # A Node base image exists but uses a non-numeric tag (e.g. 'node:lts'); we can't derive a concrete
        # version to sync the engine to, so skip without failing the run.
        _LOG.non_numeric_node_base_image(dockerfile, tag)
        return
    update_file(package_json, _NODE_ENGINE_RE, get_new_version=get_latest_tag, logger=_LOG)


def update_node_engines() -> None:
    """Find all package.json files and update the Node engine."""
    for pkg_json in glob("package.json"):
        if has_node_engine(pkg_json):
            update_node_engine(pkg_json)


def main() -> None:  # pragma: no cover
    """Update the Node engines in the repository's package.json files."""
    update_node_engines()


if __name__ == "__main__":  # pragma: no cover
    main()

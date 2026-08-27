"""Node engine updater script updates the Node engine in package.json files to the Node base image version.

Note: this script does not update package-lock.json.
"""

from typing import TYPE_CHECKING

from update_time.domain.base_image import image_version_getter
from update_time.domain.dependency import is_valid
from update_time.domain.file_type import DOCKERFILE_GLOB_PATTERNS, DOCKERFILE_NAME, PACKAGE_JSON
from update_time.file_formats import package_json as package_json_format
from update_time.io.filesystem import first_line_match, glob, glob_for
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.oci import get_latest_tag

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.marker import Marker


_LOG = get_logger("node engine")

# The section a package.json declares the Node engine in, and the name it declares it under. Its marker is named
# the same way, in the file's `update-time` field.
_ENGINES = "engines"
_NODE = "node"
_NODE_IMAGE_RE = r"FROM node:(?P<version>[\d\.]+)"
_NODE_BASE_IMAGE_RE = r"FROM node:(?P<tag>\S+)"
_NODE_ENGINE_RE = rf'"(?P<dependency>{_NODE})": "(?P<version>[\d\.]+)"'


def _has_node_engine(contents: dict) -> bool:
    """Return whether the parsed package.json declares a Node engine."""
    return _ENGINES in contents and _NODE in contents[_ENGINES]


def _node_base_image_version(dockerfile: Path) -> str:
    """Return the numeric Node base image version (e.g. '22' from 'node:22-alpine').

    Returns an empty string if the Dockerfile is missing, has no Node base image, or its Node base image carries no
    version to derive one from: a non-numeric tag such as 'node:lts', and one whose digits do not form a version,
    such as the '22.' the tag 'node:22.x' offers. The caller reports either as a tag it cannot sync the engine to.
    """
    version = first_line_match(dockerfile, _NODE_IMAGE_RE, "version")
    return version if is_valid(version) else ""


def _node_base_image_tag(dockerfile: Path) -> str:
    """Return the tag of the Node base image (e.g. '22.1.0', '22-alpine' or 'lts'), or empty string if none."""
    return first_line_match(dockerfile, _NODE_BASE_IMAGE_RE, "tag")


def _find_node_dockerfile(package_json: Path) -> Path:
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
        if _node_base_image_version(dockerfile):
            return dockerfile
    for dockerfile in candidates:
        if _node_base_image_tag(dockerfile):
            return dockerfile
    return local_dockerfile


def _engine_marker(contents: dict) -> Marker:
    """Return the marker the package.json names for its Node engine, in the section and under the name it uses."""
    return package_json_format.marker(contents, _ENGINES, _NODE)


def _update_node_engine(package_json: Path, contents: dict) -> None:
    """Update the Node engine version to the Docker Node base image version, or the latest Node release."""
    dockerfile = _find_node_dockerfile(package_json)
    if version := _node_base_image_version(dockerfile):
        update_file(
            package_json,
            _NODE_ENGINE_RE,
            # The engine declares the runtime the project ships, so it follows the base image down as well as up.
            get_new_version=image_version_getter(version, allow_downgrade=True),
            logger=_LOG,
            marker=_engine_marker(contents),
        )
        return
    if tag := _node_base_image_tag(dockerfile):
        # A Node base image exists but uses a non-numeric tag (e.g. 'node:lts'); we can't derive a concrete
        # version to sync the engine to, so skip without failing the run.
        _LOG.non_numeric_node_base_image(dockerfile, tag)
        return
    marker = _engine_marker(contents)
    update_file(package_json, _NODE_ENGINE_RE, get_new_version=get_latest_tag, logger=_LOG, marker=marker)


def update_node_engines() -> None:
    """Find all package.json files and update the Node engine."""
    for pkg_json in glob_for(PACKAGE_JSON):
        contents = package_json_format.read(pkg_json)
        if _has_node_engine(contents):
            _update_node_engine(pkg_json, contents)


def main() -> None:  # pragma: no cover
    """Update the Node engines in the repository's package.json files."""
    update_node_engines()


if __name__ == "__main__":  # pragma: no cover
    main()

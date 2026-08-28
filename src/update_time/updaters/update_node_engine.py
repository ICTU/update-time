"""Node engine updater script updates the Node engine in package.json files to the Node base image version.

Note: this script does not update package-lock.json.
"""

from typing import TYPE_CHECKING

from update_time.domain.dependency import is_valid
from update_time.domain.file_type import DOCKERFILE_NAME, DOCKERFILES, PACKAGE_JSON
from update_time.file_formats import json as json_format
from update_time.file_formats import package_json as package_json_format
from update_time.io.filesystem import first_line_match, glob_for
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.base_image import following_image_version_getter
from update_time.sources.oci import get_latest_tag

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.file_formats.json import JsonFile
    from update_time.markers.marker import ReferenceMarker


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
    engines = contents.get(_ENGINES)
    return isinstance(engines, dict) and _NODE in engines


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
    candidates = [local_dockerfile, *glob_for(DOCKERFILES)]
    for dockerfile in candidates:
        if _node_base_image_version(dockerfile):
            return dockerfile
    for dockerfile in candidates:
        if _node_base_image_tag(dockerfile):
            return dockerfile
    return local_dockerfile


def _engine_reference_marker(package_json: JsonFile) -> ReferenceMarker:
    """Return the entry declaring the Node engine, and the marker the package.json names for it.

    The engine is the entry the `engines` section declares under `node`. A `node` version another section
    declares — a Volta pin, a dependency of that name — is neither taken for the engine nor steered by the
    engine's marker.
    """
    return package_json_format.reference_marker(package_json, _ENGINES, _NODE)


def _update_node_engine(package_json: JsonFile) -> None:
    """Update the Node engine version to the Docker Node base image version, or the latest Node release.

    An engine the file declares but whose entry cannot be found is reported rather than passed over, since the
    file's two readings disagree. A file declaring the section twice reads that way: the parse keeps the last
    section, and the entry is looked for in the first.
    """
    engine_marker = _engine_reference_marker(package_json)
    if engine_marker.reference_location.line_number is None:
        _LOG.no_entry(_NODE, package_json.path)
        return
    dockerfile = _find_node_dockerfile(package_json.path)
    if version := _node_base_image_version(dockerfile):
        update_file(
            package_json.path,
            _NODE_ENGINE_RE,
            get_new_version=following_image_version_getter(version),
            logger=_LOG,
            reference_marker=engine_marker,
        )
        return
    if tag := _node_base_image_tag(dockerfile):
        # A Node base image exists but uses a non-numeric tag (e.g. 'node:lts'); we can't derive a concrete
        # version to sync the engine to, so skip without failing the run.
        _LOG.non_numeric_node_base_image(dockerfile, tag)
        return
    update_file(
        package_json.path, _NODE_ENGINE_RE, get_new_version=get_latest_tag, logger=_LOG, reference_marker=engine_marker
    )


def update_node_engines() -> None:
    """Find all package.json files and update the Node engine."""
    for path in glob_for(PACKAGE_JSON):
        package_json = json_format.read(path)
        if _has_node_engine(package_json.contents):
            _update_node_engine(package_json)


def main() -> None:  # pragma: no cover
    """Update the Node engines in the repository's package.json files."""
    update_node_engines()


if __name__ == "__main__":  # pragma: no cover
    main()

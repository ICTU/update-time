"""Node engine updater script updates the Node engine in package.json files to the latest Node release.

Note: this script does not update package-lock.json.
"""

from typing import TYPE_CHECKING

from update_time.domain.file_type import PACKAGE_JSON
from update_time.formats import json as json_format
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests import package_json as package_json_format
from update_time.references.file import update_file
from update_time.sources.oci import get_latest_tag

if TYPE_CHECKING:
    from update_time.formats.json import JsonFile
    from update_time.markers.marker import ReferenceMarker


_LOG = get_logger("node engine")

# The section a package.json declares the Node engine in, and the name it declares it under. Its marker is named
# the same way, in the file's `update-time` field.
_ENGINES = "engines"
_NODE = "node"
_NODE_ENGINE_RE = rf'"(?P<dependency>{_NODE})": "(?P<version>[\d\.]+)"'


def _has_node_engine(contents: dict) -> bool:
    """Return whether the parsed package.json declares a Node engine."""
    engines = contents.get(_ENGINES)
    return isinstance(engines, dict) and _NODE in engines


def _engine_reference_marker(package_json: JsonFile) -> ReferenceMarker:
    """Return the entry declaring the Node engine, and the marker the package.json names for it.

    The engine is the entry the `engines` section declares under `node`. A `node` version another section
    declares — a Volta pin, a dependency of that name — is neither taken for the engine nor steered by the
    engine's marker.
    """
    return package_json_format.reference_marker(package_json, _ENGINES, _NODE)


def _update_node_engine(package_json: JsonFile) -> None:
    """Update the Node engine version to the latest Node release.

    An engine the file declares but whose entry cannot be found is reported rather than passed over, since the
    file's two readings disagree. A file declaring the section twice reads that way: the parse keeps the last
    section, and the entry is looked for in the first.
    """
    engine_marker = _engine_reference_marker(package_json)
    if engine_marker.reference_location.line_number is None:
        _LOG.no_entry(_NODE, package_json.path)
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

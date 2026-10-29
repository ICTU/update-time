"""Node engine updater script updates the Node engine in package.json files to the latest Node release.

Note: this script does not update package-lock.json.
"""

from update_time.domain.file_type import PACKAGE_JSON
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests import package_json as package_json_manifest
from update_time.references.file import update_file
from update_time.sources.oci import get_latest_tag

_LOG = get_logger("node engine")


def _update_node_engine(package_json: package_json_manifest.PackageJson) -> None:
    """Update the Node engine version to the latest Node release.

    An engine the file declares but whose entry cannot be found is reported rather than passed over, since the
    file's two readings disagree. A file declaring the section twice reads that way: the parse keeps the last
    section, and the entry is looked for in the first.
    """
    engine_marker = package_json.node_engine_marker()
    if engine_marker.reference_location.line_number is None:
        _LOG.no_entry(package_json_manifest.NODE_ENGINE, package_json.path)
        return
    update_file(
        package_json.path,
        package_json_manifest.NODE_ENGINE_REFERENCE,
        get_new_version=get_latest_tag,
        logger=_LOG,
        reference_marker=engine_marker,
    )


def update_node_engines() -> None:
    """Find all package.json files and update the Node engine."""
    for path in glob_for(PACKAGE_JSON):
        package_json = package_json_manifest.PackageJson.readable(path, _LOG.unreadable_node_engine)
        if package_json is not None and package_json.declares_a_node_engine():
            _update_node_engine(package_json)


def main() -> None:  # pragma: no cover
    """Update the Node engines in the repository's package.json files."""
    update_node_engines()


if __name__ == "__main__":  # pragma: no cover
    main()

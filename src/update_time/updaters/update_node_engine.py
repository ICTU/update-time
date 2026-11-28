"""Node engine updater script updates the Node engine in package.json files to the latest Node release."""

from functools import partial
from typing import TYPE_CHECKING, Final

from update_time.domain.file_type import PACKAGE_JSON
from update_time.io.filesystem import glob_for
from update_time.io.log import FileContent, get_logger
from update_time.manifests.package_json import NODE_ENGINE, NODE_ENGINE_REFERENCE, PackageJson
from update_time.references.file import update_file
from update_time.sources.oci import SOURCE

if TYPE_CHECKING:
    from update_time.io.log import Logger

_LOG: Final[Logger] = get_logger("node engine")


def _update_node_engine(package_json: PackageJson) -> None:
    """Update the Node engine version to the latest Node release.

    An engine the file declares but whose entry cannot be found is reported rather than passed over, since the
    file's two readings disagree. A file declaring the section twice reads that way: the parse keeps the last
    section, and the entry is looked for in the first.
    """
    engine = package_json.node_engine()
    if engine.location.line_number is None:
        _LOG.no_entry(NODE_ENGINE, package_json.path)
        return
    update_file(package_json.path, NODE_ENGINE_REFERENCE, source=SOURCE, logger=_LOG, declared=engine)


def _update_node_engines() -> None:
    """Find all package.json files and update the Node engine."""
    for path in glob_for(PACKAGE_JSON):
        package_json = PackageJson.readable(path, partial(_LOG.unreadable_file, content=FileContent.NODE_ENGINE))
        if package_json is not None and package_json.declares_a_node_engine():
            _update_node_engine(package_json)


if __name__ == "__main__":
    _update_node_engines()

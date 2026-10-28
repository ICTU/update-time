"""Python version file updater bumps the CPython version pinned in `.python-version` files to the latest release."""

from update_time.domain.file_type import PYTHON_VERSION_FILE
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.python_version import PYTHON, PYTHON_VERSION_REFERENCE
from update_time.references.file import update_file
from update_time.sources.oci import get_latest_tag

_LOG = get_logger("python version file")


def update_python_version_files() -> None:
    """Update the CPython version in all `.python-version` files found recursively from the start directory."""
    for version_file in glob_for(PYTHON_VERSION_FILE):
        update_file(
            version_file, PYTHON_VERSION_REFERENCE, get_new_version=get_latest_tag, logger=_LOG, dependency=PYTHON
        )


def main() -> None:  # pragma: no cover
    """Update the CPython version in the repository's `.python-version` files."""
    update_python_version_files()


if __name__ == "__main__":  # pragma: no cover
    main()

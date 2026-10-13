"""Python version file updater bumps the CPython version pinned in `.python-version` files to the latest release."""

import re

from update_time.domain.file_type import PYTHON_VERSION_FILE
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.oci import get_latest_tag

_LOG = get_logger("python version file")

# The dependency name reported for every `.python-version` entry. Unlike most references, the name is not in the file
# (the file holds only a bare version), so it is supplied here rather than captured from the line.
_PYTHON = "python"

# A `.python-version` entry that is a plain CPython version, `X.Y` or `X.Y.Z` (e.g. `3.12` or `3.12.6`), alone on
# its line except for an optional trailing `#` comment. That comment is what lets an inline `# update-time:` marker
# be recognised, and preserved when the version is rewritten. Everything else is left untouched: alternative
# implementations (`pypy3.10-…`), free-threaded/variant suffixes (`3.13t`), prefixed forms (`cpython@3.12`,
# `>=3.10`), sentinels (`system`), and any other trailing text.
_VERSION_RE = re.compile(r"^\s*(?P<version>\d+\.\d+(?:\.\d+)?)\s*(?:#.*)?$")


def update_python_version_files() -> None:
    """Update the CPython version in all `.python-version` files found recursively from the start directory."""
    for version_file in glob_for(PYTHON_VERSION_FILE):
        update_file(version_file, _VERSION_RE, get_new_version=get_latest_tag, logger=_LOG, dependency=_PYTHON)


def main() -> None:  # pragma: no cover
    """Update the CPython version in the repository's `.python-version` files."""
    update_python_version_files()


if __name__ == "__main__":  # pragma: no cover
    main()

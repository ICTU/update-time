"""Python version file updater bumps the CPython version pinned in `.python-version` files.

The version comes from the Python base image in a Dockerfile, or from Docker Hub when no Dockerfile declares one.
"""

import re
from typing import TYPE_CHECKING

from update_time.domain.file_type import DOCKERFILE_NAME, DOCKERFILES, PYTHON_VERSION_FILE
from update_time.io.filesystem import first_line_match, glob_for
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.base_image import advancing_image_version_getter
from update_time.sources.oci import get_latest_tag

if TYPE_CHECKING:
    from pathlib import Path


_LOG = get_logger("python version file")

# The dependency name reported for every `.python-version` entry. Unlike most references, the name is not in the file
# (the file holds only a bare version), so it is supplied here rather than captured from the line.
_PYTHON = "python"

# A `.python-version` entry that is a plain CPython version, `X.Y` or `X.Y.Z` (e.g. `3.12` or `3.12.6`), alone on
# its line except for an optional trailing `#` comment. That comment is what lets an inline `# update-time:` marker
# be recognised, and preserved when the version is rewritten. Everything else is left untouched: alternative
# implementations
# (`pypy3.10-…`), free-threaded/variant suffixes (`3.13t`), prefixed forms (`cpython@3.12`, `>=3.10`), sentinels
# (`system`), and any other trailing text.
_VERSION_RE = re.compile(r"^\s*(?P<version>\d+\.\d+(?:\.\d+)?)\s*(?:#.*)?$")

# A `FROM python:<version>` base image in a Dockerfile, capturing the numeric main version at the precision the tag
# provides (`3.14` from `python:3.14`, `3.14.2` from `python:3.14.2-slim@sha256:…`). An optional `--platform=…` flag
# is matched but not captured. A non-numeric tag (`python:slim`, `python:latest`) doesn't match, so the entry falls
# back to Docker Hub instead. Only the official `python` image is recognised; other Python base images fall back too.
_PYTHON_IMAGE_RE = re.compile(r"FROM (?:--platform=\S+\s+)?python:(?P<version>\d+\.\d+(?:\.\d+)?)")


def _find_python_base_image_version(version_file: Path) -> str:
    """Return the numeric Python base image version to sync the version file to, or `''` when none can be derived.

    Prefers the Dockerfile next to the `.python-version` file; for a version file without a local Python-base
    Dockerfile, falls back to any other Dockerfile in the repo, so the two runtimes stay in step even when they don't
    sit side by side. A missing Dockerfile, or one without a numeric `FROM python:<version>` base image, yields no
    version, so when none is found `''` is returned and the caller updates the entry from Docker Hub instead.
    """
    local_dockerfile = version_file.parent / DOCKERFILE_NAME
    for dockerfile in (local_dockerfile, *glob_for(DOCKERFILES)):
        if version := first_line_match(dockerfile, _PYTHON_IMAGE_RE, "version"):
            return version
    return ""


def update_python_version_files() -> None:
    """Update the CPython version in all `.python-version` files found recursively from the start directory."""
    for version_file in glob_for(PYTHON_VERSION_FILE):
        image_version = _find_python_base_image_version(version_file)
        getter = advancing_image_version_getter(image_version) if image_version else get_latest_tag
        update_file(version_file, _VERSION_RE, get_new_version=getter, logger=_LOG, dependency=_PYTHON)


def main() -> None:  # pragma: no cover
    """Update the CPython version in the repository's `.python-version` files."""
    update_python_version_files()


if __name__ == "__main__":  # pragma: no cover
    main()

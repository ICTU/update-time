"""Python version file updater bumps the CPython version pinned in `.python-version` files.

A `.python-version` file pins the project's development Python, one version per line (pyenv allows several). Each
plain CPython entry (`3.12` or `3.12.6`) is moved forward to a fuller version, in one of two tiers:
- when a Dockerfile in the same folder has a `FROM python:<version>` base image (already updated by the Dockerfile
  updater), the entry adopts that image's version at the precision the tag provides, so the production and development
  runtimes stay in step;
- otherwise the entry is updated to the latest `python` release on Docker Hub, honouring the cooldown and staleness
  check.
Alternative implementations (`pypy3.10-…`), variant suffixes (`3.13t`), prefixed forms (`cpython@3.12`, `>=3.10`), and
sentinels (`system`) don't match and are left untouched. An `# update-time:` marker holds an entry back or bounds it,
and wins over a Dockerfile-derived version, so a deliberately held-back development version is never dragged forward by
an image update. The marker works both inline and on the line directly above the entry; note that uv rejects an inline
comment on a `.python-version` line, so the line-above form is the safer placement for a uv project.

If an environment variable DOCKER_HUB_USERNAME and DOCKER_HUB_TOKEN are set, the fallback uses them to increase the
Docker Hub rate limit.
"""

import re
from typing import TYPE_CHECKING

from packaging.version import Version

from update_time.domain.version import DependencyVersion, is_valid
from update_time.io.filesystem import DOCKERFILE_GLOB_PATTERNS, DOCKERFILE_NAME, first_line_match, glob
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.oci import get_latest_tag

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.bound import NewVersionGetter, VersionBound

LOG = get_logger("python version file")

# The Python version file, read from the repository root but supported per package too (a monorepo can carry one per
# package), so it is looked up recursively from the scan root. It is a hidden (dot-prefixed) file, but naming it in
# the glob pattern makes `glob` visit it anyway.
PYTHON_VERSION_FILE = ".python-version"

# The dependency name reported for every `.python-version` entry. Unlike most references, the name is not in the file
# (the file holds only a bare version), so it is supplied here rather than captured from the line.
PYTHON = "python"

# A `.python-version` entry that is a plain CPython version, `X.Y` or `X.Y.Z` (e.g. `3.12` or `3.12.6`), alone on its
# line except for an optional trailing `#` comment — which lets an inline `# update-time:` marker be recognised (and
# preserved when the version is rewritten). Everything else is left untouched: alternative implementations
# (`pypy3.10-…`), free-threaded/variant suffixes (`3.13t`), prefixed forms (`cpython@3.12`, `>=3.10`), sentinels
# (`system`), and any other trailing text.
VERSION_RE = re.compile(r"^\s*(?P<version>\d+\.\d+(?:\.\d+)?)\s*(?:#.*)?$")

# A `FROM python:<version>` base image in a Dockerfile, capturing the numeric main version at the precision the tag
# provides (`3.14` from `python:3.14`, `3.14.2` from `python:3.14.2-slim@sha256:…`). An optional `--platform=…` flag
# is matched but not captured. A non-numeric tag (`python:slim`, `python:latest`) doesn't match, so the entry falls
# back to Docker Hub instead. Only the official `python` image is recognised; other Python base images fall back too.
PYTHON_IMAGE_RE = re.compile(r"FROM (?:--platform=\S+\s+)?python:(?P<version>\d+\.\d+(?:\.\d+)?)")


def _find_python_base_image_version(version_file: Path) -> str:
    """Return the numeric Python base image version to sync the version file to, or `''` when none can be derived.

    Prefers the Dockerfile next to the `.python-version` file; for a version file without a local Python-base
    Dockerfile, falls back to any other Dockerfile in the repo, so the two runtimes stay in step even when they don't
    sit side by side. A missing Dockerfile, or one without a numeric `FROM python:<version>` base image, yields no
    version, so when none is found `''` is returned and the caller updates the entry from Docker Hub instead.
    """
    local_dockerfile = version_file.parent / DOCKERFILE_NAME
    for dockerfile in (local_dockerfile, *glob(*DOCKERFILE_GLOB_PATTERNS)):
        if version := first_line_match(dockerfile, PYTHON_IMAGE_RE, "version"):
            return version
    return ""


def _image_version_getter(image_version: str) -> NewVersionGetter:
    """Return a new-version getter that offers the Dockerfile's Python base image version, honouring the marker's bound.

    The image is the source of truth in the Dockerfile tier, so the entry adopts its version — but only when the
    image is newer than the current entry (never a downgrade) and the entry's own version bound admits it, so an
    `# update-time: allow[update<3.13]` on the entry still wins over an image that jumped to `3.13`. The cooldown and
    staleness check have already been applied to the image itself, so the offered version carries no dates and is
    neither held back nor flagged here.
    """

    def get_new_version(_dependency: str, current_version: str, version_bound: VersionBound) -> DependencyVersion:
        candidate = Version(image_version)
        newer = is_valid(current_version) and candidate > Version(current_version)
        if newer and version_bound.keeps(candidate, current_version):
            return DependencyVersion(version=image_version)
        return DependencyVersion(version=current_version)

    return get_new_version


def update_python_version_files(start: Path | None = None) -> None:
    """Update the CPython version in all `.python-version` files found recursively from the start directory."""
    for version_file in glob(PYTHON_VERSION_FILE, start=start):
        image_version = _find_python_base_image_version(version_file)
        get_new_version = _image_version_getter(image_version) if image_version else get_latest_tag
        update_file(version_file, VERSION_RE, get_new_version=get_new_version, logger=LOG, dependency=PYTHON)


def main() -> None:  # pragma: no cover
    """Update the CPython version in the repository's `.python-version` files."""
    update_python_version_files()


if __name__ == "__main__":  # pragma: no cover
    main()

"""Docker tag updater script finds Dockerfiles and updates base image tags to latest compatible versions."""

import re
from typing import TYPE_CHECKING

from update_time.domain.file_type import DOCKERFILES
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.oci import OPTIONALLY_TAGGED_IMAGE_REFERENCE, tag_getter

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.dependency import DependencyName


_LOG = get_logger("dockerfile")
# Allow an optional `--platform=…` flag between `FROM` and the image reference (common in multi-arch builds). The flag
# is matched but not captured, so only the image reference is rewritten and the flag is left untouched.
_IMAGE_RE = rf"^\s*(?i:FROM)\s+(?:--platform=\S+\s+)?{OPTIONALLY_TAGGED_IMAGE_REFERENCE}"

_SCRATCH = "scratch"

# The name a `FROM ... AS name` gives a build stage, which a later `FROM` in the same file names to build on it.
# Read case-insensitively, since Dockerfile keywords are, and a lower-case `as` is as common as an upper-case one.
_STAGE_NAME_RE = re.compile(r"^FROM\s.*\sAS\s+(?P<stage>\S+)", re.IGNORECASE | re.MULTILINE)


def _stage_names(dockerfile: Path) -> frozenset[str]:
    """Return the names the Dockerfile gives its build stages, in the lower case Docker matches them in."""
    return frozenset(match.group("stage").lower() for match in _STAGE_NAME_RE.finditer(dockerfile.read_text()))


def _update_dockerfile(dockerfile: Path) -> None:
    """Update the base images in one Dockerfile, leaving the references no registry serves unchanged."""
    stages = _stage_names(dockerfile)

    def registry_serves(image: DependencyName) -> bool:
        """Return whether a registry serves the image, which it does for neither `scratch` nor a build stage."""
        return image != _SCRATCH and image.lower() not in stages

    update_file(dockerfile, _IMAGE_RE, get_new_version=tag_getter(registry_serves), logger=_LOG)


def update_dockerfiles() -> None:
    """Update the base image of Dockerfiles."""
    for dockerfile in glob_for(DOCKERFILES):
        _update_dockerfile(dockerfile)


def main() -> None:  # pragma: no cover
    """Update the base images in the repository's Dockerfiles."""
    update_dockerfiles()


if __name__ == "__main__":  # pragma: no cover
    main()

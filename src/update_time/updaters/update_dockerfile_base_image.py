"""Docker tag updater script finds Dockerfiles and updates base image tags to latest compatible versions."""

from update_time.io.filesystem import DOCKERFILE_GLOB_PATTERNS
from update_time.io.log import get_logger
from update_time.references.file import update_files
from update_time.sources.oci import IMAGE_REFERENCE, get_latest_tag

_LOG = get_logger("dockerfile")
# Allow an optional `--platform=…` flag between `FROM` and the image reference (common in multi-arch builds). The flag
# is matched but not captured, so only the image reference is rewritten and the flag is left untouched.
_IMAGE_RE = rf"FROM (?:--platform=\S+\s+)?{IMAGE_REFERENCE}"


def update_dockerfiles() -> None:
    """Update the base image of Dockerfiles."""
    update_files(
        *DOCKERFILE_GLOB_PATTERNS, regexp=_IMAGE_RE, get_new_version=get_latest_tag, logger=_LOG, case_sensitive=False
    )


def main() -> None:  # pragma: no cover
    """Update the base images in the repository's Dockerfiles."""
    update_dockerfiles()


if __name__ == "__main__":  # pragma: no cover
    main()

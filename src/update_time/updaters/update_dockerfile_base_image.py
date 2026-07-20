"""Docker tag updater script finds Dockerfiles and updates base image tags to latest compatible versions.

Base images referenced by tag only are automatically pinned by appending the digest of the (latest) tag. The
digest is optional in the regex but a concrete version tag is still required, so base images without a version
(e.g. `FROM scratch`) and stage references are left untouched.
"""

import sys

from update_time.io.filesystem import DOCKERFILE_GLOB_PATTERNS
from update_time.io.log import get_logger
from update_time.references.file import update_files
from update_time.sources.oci import IMAGE_REFERENCE, get_latest_tag

LOG = get_logger("dockerfile")
# Allow an optional `--platform=…` flag between `FROM` and the image reference (common in multi-arch builds). The flag
# is matched but not captured, so only the image reference is rewritten and the flag is left untouched.
IMAGE_RE = rf"FROM (?:--platform=\S+\s+)?{IMAGE_REFERENCE}"


def update_dockerfiles() -> int:
    """Update the base image of Dockerfiles."""
    return update_files(
        *DOCKERFILE_GLOB_PATTERNS, regexp=IMAGE_RE, get_new_version=get_latest_tag, logger=LOG, case_sensitive=False
    )


def main() -> int:  # pragma: no cover
    """Update the base images in the repository's Dockerfiles."""
    return update_dockerfiles()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

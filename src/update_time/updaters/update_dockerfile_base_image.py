"""Docker tag updater script finds Dockerfiles and updates base image tags to latest compatible versions.

Base images referenced by tag only are automatically pinned by appending the digest of the (latest) tag. The
digest is optional in the regex but a concrete version tag is still required, so base images without a version
(e.g. `FROM scratch`) and stage references are left untouched.
"""

import sys

from update_time.io.filesystem import update_files
from update_time.io.log import get_logger
from update_time.sources.docker import get_latest_tag

LOG = get_logger("dockerfile")
IMAGE_RE = r"FROM (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)(?:@(?P<sha>sha256:[a-f0-9]{64}))?"


def update_dockerfiles() -> int:
    """Update the base image of Dockerfiles."""
    return update_files("Dockerfile", regexp=IMAGE_RE, get_new_version=get_latest_tag, logger=LOG)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(update_dockerfiles())

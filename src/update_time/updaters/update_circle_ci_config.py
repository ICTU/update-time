"""CircleCI config updater script finds images and updates to the latest versions.

Images referenced by tag only are automatically pinned by appending the digest of the (latest) tag. The digest
is optional in the regex but a concrete version tag is still required, so images referenced through variable
substitution (``${VAR}``) are left untouched.
"""

import sys
from pathlib import Path

from update_time.io.filesystem import YAML_GLOB_PATTERNS, update_files
from update_time.io.log import get_logger
from update_time.sources.docker import get_latest_tag

LOG = get_logger("circleci")
IMAGE_RE = r"image: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)(?:@(?P<sha>sha256:[a-f0-9]{64}))?"


def update_circle_ci_config(circle_ci_dir: Path) -> int:
    """Update the images in all YAML files under the CircleCI directory."""
    return update_files(
        *YAML_GLOB_PATTERNS, regexp=IMAGE_RE, get_new_version=get_latest_tag, logger=LOG, start=circle_ci_dir
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(update_circle_ci_config(Path.cwd() / ".circleci"))

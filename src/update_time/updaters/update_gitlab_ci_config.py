"""GitLab CI config updater script finds images and updates to the latest versions.

Images referenced by tag only are automatically pinned by appending the digest of the (latest) tag. The digest
is optional in the regex but a concrete version tag is still required, so images referenced through variable
substitution (`$VAR` / `${VAR}`, e.g. `$CI_REGISTRY_IMAGE`) are left untouched.

GitLab CI uses a single configuration file, `.gitlab-ci.yml`, at the repository root, so it is addressed directly
with `update_file` rather than searched for with `glob` (which is recursive and would also match any nested
`.gitlab-ci.yml`).
"""

import sys
from pathlib import Path

from update_time.io.filesystem import update_file
from update_time.io.log import get_logger
from update_time.sources.oci import YAML_IMAGE_REFERENCE, get_latest_tag

LOG = get_logger("gitlab ci")


def update_gitlab_ci_config(gitlab_ci_config: Path) -> int:
    """Update the images in the GitLab CI configuration file, if it exists."""
    if not gitlab_ci_config.exists():
        return 0
    return update_file(gitlab_ci_config, YAML_IMAGE_REFERENCE, get_new_version=get_latest_tag, logger=LOG)


def main() -> int:  # pragma: no cover
    """Update the images in the repository's GitLab CI configuration."""
    return update_gitlab_ci_config(Path.cwd() / ".gitlab-ci.yml")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

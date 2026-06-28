"""GitLab CI config updater script finds images and updates to the latest versions.

Images referenced by tag only are automatically pinned by appending the digest of the (latest) tag. The digest
is optional in the regex but a concrete version tag is still required, so images referenced through variable
substitution (`$VAR` / `${VAR}`, e.g. `$CI_REGISTRY_IMAGE`) are left untouched.

The default config file `.gitlab-ci.yml` is a dotfile at the repository root. The shared `glob` helper skips
dotfiles, so the file is targeted directly with `update_file` instead of being discovered through globbing.
"""

import sys
from pathlib import Path

from update_time.io.filesystem import update_file
from update_time.io.log import get_logger
from update_time.sources.docker import get_latest_tag

LOG = get_logger("gitlab ci")
IMAGE_RE = r"image: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)(?:@(?P<sha>sha256:[a-f0-9]{64}))?"


def update_gitlab_ci_config(gitlab_ci_config: Path) -> int:
    """Update the images in the GitLab CI configuration file, if it exists."""
    if not gitlab_ci_config.exists():
        return 0
    return update_file(gitlab_ci_config, IMAGE_RE, get_latest_tag, LOG)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(update_gitlab_ci_config(Path.cwd() / ".gitlab-ci.yml"))

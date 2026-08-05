"""GitLab CI config updater script finds images and updates to the latest versions.

GitLab CI uses a single configuration file, `.gitlab-ci.yml`, at the repository root, so it is addressed directly
with `update_file` rather than searched for with `glob` (which is recursive and would also match any nested
`.gitlab-ci.yml`).
"""

from pathlib import Path

from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.oci import YAML_IMAGE_REFERENCE, get_latest_tag

_LOG = get_logger("gitlab ci")


def update_gitlab_ci_config(gitlab_ci_config: Path) -> None:
    """Update the images in the GitLab CI configuration file, if it exists."""
    if gitlab_ci_config.exists():
        update_file(gitlab_ci_config, YAML_IMAGE_REFERENCE, get_new_version=get_latest_tag, logger=_LOG)


def main() -> None:  # pragma: no cover
    """Update the images in the repository's GitLab CI configuration."""
    update_gitlab_ci_config(Path.cwd() / ".gitlab-ci.yml")


if __name__ == "__main__":  # pragma: no cover
    main()

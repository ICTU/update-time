"""GitLab CI config updater script finds images and updates to the latest versions."""

from update_time.domain.file_type import GITLAB_CI_CONFIG
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.oci import YAML_IMAGE_REFERENCE, get_latest_tag

_LOG = get_logger("gitlab ci")


def update_gitlab_ci_config() -> None:
    """Update the images in the GitLab CI configuration file, if the repository has one."""
    for gitlab_ci_config in glob_for(GITLAB_CI_CONFIG):
        update_file(gitlab_ci_config, YAML_IMAGE_REFERENCE, get_new_version=get_latest_tag, logger=_LOG)


def main() -> None:  # pragma: no cover
    """Update the images in the repository's GitLab CI configuration."""
    update_gitlab_ci_config()


if __name__ == "__main__":  # pragma: no cover
    main()

"""GitLab CI config updater script finds images and updates to the latest versions."""

from typing import TYPE_CHECKING, Final

from update_time.domain.file_type import GITLAB_CI_CONFIG
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.yaml_image import YAML_IMAGE_REFERENCE
from update_time.references.file import update_file
from update_time.sources.oci import TagGetter

if TYPE_CHECKING:
    from update_time.io.log import Logger

_LOG: Final[Logger] = get_logger("gitlab ci")


def _update_gitlab_ci_config() -> None:
    """Update the images in the GitLab CI configuration file, if the repository has one."""
    for gitlab_ci_config in glob_for(GITLAB_CI_CONFIG):
        update_file(gitlab_ci_config, YAML_IMAGE_REFERENCE, source=TagGetter().source, logger=_LOG)


if __name__ == "__main__":
    _update_gitlab_ci_config()

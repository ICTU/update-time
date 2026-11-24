"""CircleCI config updater script finds images and updates them to the latest versions."""

from typing import TYPE_CHECKING, Final

from update_time.domain.dependency import ImageNotLookedUp
from update_time.domain.file_type import CIRCLE_CI_CONFIGS
from update_time.io.log import get_logger
from update_time.manifests.circle_ci_config import machine_images
from update_time.manifests.yaml_image import YAML_IMAGE_REFERENCE
from update_time.references.file import update_yaml_files

if TYPE_CHECKING:
    from update_time.io.log import Logger

_LOG: Final[Logger] = get_logger("circleci")


def _update_circle_ci_config() -> None:
    """Update the images in all YAML files under the CircleCI directory.

    A config is parsed, so one whose YAML does not parse is skipped.
    """
    update_yaml_files(
        CIRCLE_CI_CONFIGS,
        regexp=YAML_IMAGE_REFERENCE,
        find_images=machine_images,
        reason=ImageNotLookedUp.MACHINE_EXECUTOR_IMAGE,
        logger=_LOG,
    )


if __name__ == "__main__":
    _update_circle_ci_config()

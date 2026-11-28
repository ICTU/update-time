"""Container image updater script finds the images in CI configs, manifests, and devcontainers and updates them."""

from itertools import chain
from typing import TYPE_CHECKING, Final

from update_time.domain.dependency import ImageNotLookedUp
from update_time.domain.file_type import (
    CIRCLE_CI_CONFIGS,
    DEVCONTAINER_CONFIGS,
    DOCKER_COMPOSE_FILES,
    GITLAB_CI_CONFIG,
    HELM_CHARTS,
)
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.circle_ci_config import machine_images
from update_time.manifests.devcontainer_json import BASE_IMAGE_REFERENCE, FEATURE_REFERENCE
from update_time.manifests.docker_compose import built_images
from update_time.manifests.yaml_image import YAML_IMAGE_REFERENCE
from update_time.references.file import update_file, update_yaml_files
from update_time.sources.oci import image_source

if TYPE_CHECKING:
    from update_time.io.log import Logger

_LOG: Final[Logger] = get_logger("container images")


def _update_container_images() -> None:
    """Update the images in the CircleCI, GitLab CI, Docker Compose, Helm, and devcontainer files.

    A CircleCI config and a Compose file are parsed, so one whose YAML does not parse is skipped. A Helm chart is read
    line by line, because a Go-templated chart is not valid YAML.
    """
    update_yaml_files(
        CIRCLE_CI_CONFIGS,
        regexp=YAML_IMAGE_REFERENCE,
        find_images=machine_images,
        reason=ImageNotLookedUp.MACHINE_EXECUTOR_IMAGE,
        logger=_LOG,
    )
    update_yaml_files(
        DOCKER_COMPOSE_FILES,
        regexp=YAML_IMAGE_REFERENCE,
        find_images=built_images,
        reason=ImageNotLookedUp.BUILT_IMAGE,
        logger=_LOG,
    )
    for path in chain(glob_for(GITLAB_CI_CONFIG), glob_for(HELM_CHARTS)):
        update_file(path, YAML_IMAGE_REFERENCE, source=image_source(), logger=_LOG)
    # A Dockerfile or a Compose file that a devcontainer.json points at is updated as a file of its own.
    for devcontainer in glob_for(DEVCONTAINER_CONFIGS):
        update_file(devcontainer, BASE_IMAGE_REFERENCE, FEATURE_REFERENCE, source=image_source(), logger=_LOG)


if __name__ == "__main__":
    _update_container_images()

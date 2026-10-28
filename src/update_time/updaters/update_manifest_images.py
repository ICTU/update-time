"""Manifest image updater script finds image tags and updates them to latest compatible versions."""

from update_time.domain.dependency import ImageNotLookedUp
from update_time.domain.file_type import DOCKER_COMPOSE_FILES, HELM_CHARTS
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.docker_compose import built_images
from update_time.manifests.yaml_image import YAML_IMAGE_REFERENCE
from update_time.references.file import update_file, update_yaml_files
from update_time.sources.oci import TagGetter

_LOG = get_logger("manifest images")


def update_manifest_images() -> None:
    """Update the image tags and digests in the Docker Compose files and the Helm folder.

    A Compose file is parsed, so one whose YAML does not parse is skipped. A Helm chart is read line by line,
    because a Go-templated chart is not valid YAML.
    """
    update_yaml_files(
        DOCKER_COMPOSE_FILES,
        regexp=YAML_IMAGE_REFERENCE,
        find_images=built_images,
        reason=ImageNotLookedUp.BUILT_IMAGE,
        logger=_LOG,
    )
    for helm_chart in glob_for(HELM_CHARTS):
        update_file(helm_chart, YAML_IMAGE_REFERENCE, get_new_version=TagGetter(), logger=_LOG)


def main() -> None:  # pragma: no cover
    """Update the images in the repository's Docker Compose and Helm manifests."""
    update_manifest_images()


if __name__ == "__main__":  # pragma: no cover
    main()

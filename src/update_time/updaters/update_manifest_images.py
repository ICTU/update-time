"""Manifest image updater script finds image tags and updates them to latest compatible versions."""

from update_time.domain.file_type import DOCKER_COMPOSE_FILES, HELM_CHARTS
from update_time.io.log import get_logger
from update_time.references.file import update_files
from update_time.sources.oci import YAML_IMAGE_REFERENCE, get_latest_tag

_LOG = get_logger("manifest images")


def update_manifest_images() -> None:
    """Update the image tags and digests in the Docker Compose files and the Helm folder."""
    for file_type in (DOCKER_COMPOSE_FILES, HELM_CHARTS):
        update_files(file_type, regexp=YAML_IMAGE_REFERENCE, get_new_version=get_latest_tag, logger=_LOG)


def main() -> None:  # pragma: no cover
    """Update the images in the repository's Docker Compose and Helm manifests."""
    update_manifest_images()


if __name__ == "__main__":  # pragma: no cover
    main()

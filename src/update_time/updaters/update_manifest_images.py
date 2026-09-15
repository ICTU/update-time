"""Manifest image updater script finds image tags and updates them to latest compatible versions."""

from typing import TYPE_CHECKING

from update_time.domain.dependency import Unserved
from update_time.domain.file_type import DOCKER_COMPOSE_FILES, HELM_CHARTS
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.references.file import update_file, update_yaml_files
from update_time.sources.oci import YAML_IMAGE_REFERENCE, get_latest_tag, tag_getter_excluding

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter

_LOG = get_logger("manifest images")


def _built_images(compose_file_contents: object) -> set[str]:
    """Return the images the Compose file builds: the `image:` of each service that also declares a `build:`."""
    services = compose_file_contents.get("services") if isinstance(compose_file_contents, dict) else None
    if not isinstance(services, dict):
        return set()
    return {
        service["image"]
        for service in services.values()
        if isinstance(service, dict) and "build" in service and isinstance(service.get("image"), str)
    }


def _tag_getter_for(compose_file_contents: object) -> NewVersionGetter:
    """Return the getter resolving one Compose file's images, which leaves the images that file builds unchanged."""
    return tag_getter_excluding(_built_images(compose_file_contents), Unserved.BUILT_IMAGE)


def update_manifest_images() -> None:
    """Update the image tags and digests in the Docker Compose files and the Helm folder.

    A Compose file is parsed, so one whose YAML does not parse is skipped. A Helm chart is read line by line,
    because a Go-templated chart is not valid YAML.
    """
    update_yaml_files(
        DOCKER_COMPOSE_FILES, regexp=YAML_IMAGE_REFERENCE, get_new_version_for=_tag_getter_for, logger=_LOG
    )
    for helm_chart in glob_for(HELM_CHARTS):
        update_file(helm_chart, YAML_IMAGE_REFERENCE, get_new_version=get_latest_tag, logger=_LOG)


def main() -> None:  # pragma: no cover
    """Update the images in the repository's Docker Compose and Helm manifests."""
    update_manifest_images()


if __name__ == "__main__":  # pragma: no cover
    main()

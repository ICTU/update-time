"""Manifest image updater script finds image tags and updates them to latest compatible versions."""

from pathlib import Path

from update_time.io.filesystem import YAML_GLOB_PATTERNS
from update_time.io.log import get_logger
from update_time.references.file import update_files
from update_time.sources.oci import YAML_IMAGE_REFERENCE, get_latest_tag

_LOG = get_logger("manifest images")


def update_manifest_images() -> None:
    """Update the image tags and digests in the Docker Compose files and the Helm folder."""
    update_files("docker-compose*.yml", regexp=YAML_IMAGE_REFERENCE, get_new_version=get_latest_tag, logger=_LOG)
    update_files(
        *YAML_GLOB_PATTERNS,
        regexp=YAML_IMAGE_REFERENCE,
        get_new_version=get_latest_tag,
        logger=_LOG,
        start=Path.cwd() / "helm",
    )


def main() -> None:  # pragma: no cover
    """Update the images in the repository's Docker Compose and Helm manifests."""
    update_manifest_images()


if __name__ == "__main__":  # pragma: no cover
    main()

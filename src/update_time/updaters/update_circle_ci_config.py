"""CircleCI config updater script finds images and updates them to the latest versions.

CircleCI machine-executor images (the `image:` under a `machine:` key, e.g. `ubuntu-2204:2024.01.1`) are not on
Docker Hub and have no registry to query, so they are detected by parsing the YAML and left unchanged.
"""

from typing import TYPE_CHECKING

from update_time.domain.dependency import AccountedFor
from update_time.domain.file_type import CIRCLE_CI_CONFIGS
from update_time.io.log import get_logger
from update_time.references.file import update_yaml_files
from update_time.sources.oci import YAML_IMAGE_REFERENCE, tag_getter_excluding_each

if TYPE_CHECKING:
    from update_time.formats.yaml import Document

_LOG = get_logger("circleci")


def _machine_images(document: Document) -> set[str]:
    """Return the machine-executor image references (the `image:` under any `machine:` key) in a parsed config."""
    images: set[str] = set()
    if isinstance(document, dict):
        machine = document.get("machine")
        if isinstance(machine, dict) and isinstance(image := machine.get("image"), str):
            images.add(image)
        for value in document.values():
            images |= _machine_images(value)
    elif isinstance(document, list):
        for item in document:
            images |= _machine_images(item)
    return images


def update_circle_ci_config() -> None:
    """Update the images in all YAML files under the CircleCI directory.

    A config is parsed, so one whose YAML does not parse is skipped.
    """
    update_yaml_files(
        CIRCLE_CI_CONFIGS,
        regexp=YAML_IMAGE_REFERENCE,
        get_new_version_for=tag_getter_excluding_each(_machine_images, AccountedFor.MACHINE_EXECUTOR_IMAGE),
        logger=_LOG,
    )


def main() -> None:  # pragma: no cover
    """Update the images in the repository's CircleCI configuration."""
    update_circle_ci_config()


if __name__ == "__main__":  # pragma: no cover
    main()

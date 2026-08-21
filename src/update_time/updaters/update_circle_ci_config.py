"""CircleCI config updater script finds images and updates to the latest versions.

CircleCI machine-executor images (the `image:` under a `machine:` key, e.g. `ubuntu-2204:2024.01.1`) are not on
Docker Hub and have no registry to query, so they are detected by parsing the YAML and left unchanged.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from update_time.file_formats import yaml as yaml_format
from update_time.io.filesystem import YAML_GLOB_PATTERNS, glob
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.oci import YAML_IMAGE_REFERENCE, tag_getter

if TYPE_CHECKING:
    from update_time.domain.dependency import DependencyName

_LOG = get_logger("circleci")


def _machine_images(config: object) -> set[str]:
    """Return the machine-executor image references (the `image:` under any `machine:` key) in a parsed config."""
    images: set[str] = set()
    if isinstance(config, dict):
        machine = config.get("machine")
        if isinstance(machine, dict) and isinstance(image := machine.get("image"), str):
            images.add(image)
        for value in config.values():
            images |= _machine_images(value)
    elif isinstance(config, list):
        for item in config:
            images |= _machine_images(item)
    return images


def _update_circle_ci_yaml(config_file: Path) -> None:
    """Update the Docker images in a single CircleCI YAML file, leaving machine-executor images unchanged."""
    machine = _machine_images(yaml_format.read(config_file))
    machine_names = {image.split(":", maxsplit=1)[0] for image in machine}

    def registry_serves(image: DependencyName) -> bool:
        """Return whether a registry serves the image, which it does for every image but a machine-executor one.

        A machine image is recognised by name rather than by name and version, a capability being asked about the
        dependency alone.
        """
        return image not in machine_names

    update_file(config_file, YAML_IMAGE_REFERENCE, get_new_version=tag_getter(registry_serves), logger=_LOG)


def update_circle_ci_config(circle_ci_dir: Path) -> None:
    """Update the images in all YAML files under the CircleCI directory."""
    for config_file in glob(*YAML_GLOB_PATTERNS, start=circle_ci_dir):
        _update_circle_ci_yaml(config_file)


def main() -> None:  # pragma: no cover
    """Update the images in the repository's CircleCI configuration."""
    update_circle_ci_config(Path.cwd() / ".circleci")


if __name__ == "__main__":  # pragma: no cover
    main()

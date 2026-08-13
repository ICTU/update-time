"""CircleCI config updater script finds images and updates to the latest versions.

CircleCI machine-executor images (the `image:` under a `machine:` key, e.g. `ubuntu-2204:2024.01.1`) are not on
Docker Hub and have no registry to query, so they are detected by parsing the YAML and left unchanged.
"""

from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain.dependency import DependencyName, DependencyVersion, VersionString
from update_time.domain.publication import publication_date_reporting, reports_publication_dates
from update_time.file_formats import yaml as yaml_format
from update_time.io.filesystem import YAML_GLOB_PATTERNS, glob
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.oci import YAML_IMAGE_REFERENCE, get_latest_tag

if TYPE_CHECKING:
    from update_time.domain.bound import VersionBound

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

    def dates_the_versions_of(image: DependencyName) -> bool:
        """Return whether the image's versions carry a publication date.

        A machine image carries none, since no registry serves it at all, and is recognised by name here rather
        than by name and version, since a capability is asked about the dependency alone. Any other image is
        judged by `get_latest_tag`, which resolves it.
        """
        return image not in machine_names and reports_publication_dates(get_latest_tag, image)

    @partial(publication_date_reporting, when=dates_the_versions_of)
    def get_new_version(
        dependency: DependencyName, version: VersionString, version_bound: VersionBound, cooldown_days: int
    ) -> DependencyVersion:
        if f"{dependency}:{version}" in machine:
            return DependencyVersion(version=version)  # Leave machine images unchanged; they aren't on a registry
        return get_latest_tag(dependency, version, version_bound, cooldown_days)

    update_file(config_file, YAML_IMAGE_REFERENCE, get_new_version=get_new_version, logger=_LOG)


def update_circle_ci_config(circle_ci_dir: Path) -> None:
    """Update the images in all YAML files under the CircleCI directory."""
    for config_file in glob(*YAML_GLOB_PATTERNS, start=circle_ci_dir):
        _update_circle_ci_yaml(config_file)


def main() -> None:  # pragma: no cover
    """Update the images in the repository's CircleCI configuration."""
    update_circle_ci_config(Path.cwd() / ".circleci")


if __name__ == "__main__":  # pragma: no cover
    main()

"""CircleCI config updater script finds images and updates to the latest versions.

Images referenced by tag only are automatically pinned by appending the digest of the (latest) tag. The digest
is optional in the regex but a concrete version tag is still required, so images referenced through variable
substitution (`${VAR}`) are left untouched.

CircleCI machine-executor images (the `image:` under a `machine:` key, e.g. `ubuntu-2204:2024.01.1`) are not on
Docker Hub and have no registry to query, so they are detected by parsing the YAML and left unchanged.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from update_time.domain.version import DependencyName, DependencyVersion, VersionString
from update_time.file_formats import yaml as yaml_format
from update_time.io.filesystem import YAML_GLOB_PATTERNS, glob
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.oci import YAML_IMAGE_REFERENCE, get_latest_tag

if TYPE_CHECKING:
    from update_time.domain.bound import VersionBound

LOG = get_logger("circleci")


def machine_images(config: object) -> set[str]:
    """Return the machine-executor image references (the `image:` under any `machine:` key) in a parsed config."""
    images: set[str] = set()
    if isinstance(config, dict):
        machine = config.get("machine")
        if isinstance(machine, dict) and isinstance(image := machine.get("image"), str):
            images.add(image)
        for value in config.values():
            images |= machine_images(value)
    elif isinstance(config, list):
        for item in config:
            images |= machine_images(item)
    return images


def update_circle_ci_yaml(config_file: Path) -> None:
    """Update the Docker images in a single CircleCI YAML file, leaving machine-executor images unchanged."""
    machine = machine_images(yaml_format.read(config_file))

    def get_new_version(
        dependency: DependencyName, version: VersionString, version_bound: VersionBound
    ) -> DependencyVersion:
        if f"{dependency}:{version}" in machine:
            return DependencyVersion(version=version)  # Leave machine images unchanged; they aren't on a registry
        return get_latest_tag(dependency, version, version_bound)

    update_file(config_file, YAML_IMAGE_REFERENCE, get_new_version=get_new_version, logger=LOG)


def update_circle_ci_config(circle_ci_dir: Path) -> None:
    """Update the images in all YAML files under the CircleCI directory."""
    for config_file in glob(*YAML_GLOB_PATTERNS, start=circle_ci_dir):
        update_circle_ci_yaml(config_file)


def main() -> None:  # pragma: no cover
    """Update the images in the repository's CircleCI configuration."""
    update_circle_ci_config(Path.cwd() / ".circleci")


if __name__ == "__main__":  # pragma: no cover
    main()

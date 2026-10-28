"""Devcontainer updater script finds devcontainer.json files and updates their image and feature references.

Images in a Dockerfile or Compose file the devcontainer.json points at (`build.dockerfile`, `dockerComposeFile`)
are left to those updaters.
"""

from update_time.domain.file_type import DEVCONTAINER_CONFIGS
from update_time.io.filesystem import glob_for
from update_time.io.log import get_logger
from update_time.manifests.devcontainer_json import BASE_IMAGE_REFERENCE, FEATURE_REFERENCE
from update_time.references.file import update_file
from update_time.sources.oci import TagGetter

_LOG = get_logger("devcontainer")


def update_devcontainers() -> None:
    """Update the base image and feature references in the repository's devcontainer.json files."""
    for devcontainer in glob_for(DEVCONTAINER_CONFIGS):
        update_file(devcontainer, BASE_IMAGE_REFERENCE, FEATURE_REFERENCE, get_new_version=TagGetter(), logger=_LOG)


def main() -> None:  # pragma: no cover
    """Update the images and features in the repository's devcontainer.json files."""
    update_devcontainers()


if __name__ == "__main__":  # pragma: no cover
    main()

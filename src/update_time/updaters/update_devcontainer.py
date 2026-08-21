"""Devcontainer updater script finds devcontainer.json files and updates their image and feature references.

Images in a Dockerfile or Compose file the devcontainer.json points at (`build.dockerfile`, `dockerComposeFile`)
are left to those updaters.

The file is edited line by line with the same machinery as the other image updaters rather than parsed as JSON, so
comments and trailing commas (which devcontainer.json allows, and plain JSON forbids) are preserved untouched.
"""

from update_time.io.filesystem import glob
from update_time.io.log import get_logger
from update_time.references.file import update_file
from update_time.sources.oci import IMAGE_REFERENCE, OPTIONALLY_TAGGED_IMAGE_REFERENCE, get_latest_tag

_LOG = get_logger("devcontainer")

# The base image, as a JSON string value: `"image": "mcr.microsoft.com/devcontainers/typescript-node:1"`. Its tag
# is optional, the `"image"` key naming what a value without one is, so it is pinned like a Dockerfile's `FROM`.
_IMAGE_RE = rf'"image":\s*"{OPTIONALLY_TAGGED_IMAGE_REFERENCE}"'
# A feature, as a JSON object key: `"ghcr.io/devcontainers/features/node:1": { ... }`. The trailing `: {` anchors
# the match to a feature key (an OCI reference mapping to an options object), so ordinary string values whose text
# happens to look like `name:version` (e.g. `"appPort": "3000:3000"`) are not matched. Here the tag is required,
# since the reference is the key itself: without one, every object key would read as a feature, `"customizations"`
# and a local feature's `"./my-feature"` alike.
_FEATURE_RE = rf'"{IMAGE_REFERENCE}":\s*{{'

# Standard devcontainer.json locations: a top-level file, the conventional `.devcontainer/` folder, and
# per-configuration subfolders under it. `glob` visits these dot-paths because they are named in the patterns.
_DEVCONTAINER_GLOBS = (".devcontainer.json", ".devcontainer/devcontainer.json", ".devcontainer/*/devcontainer.json")


def update_devcontainers() -> None:
    """Update the base image and feature references in the repository's devcontainer.json files."""
    for devcontainer in glob(*_DEVCONTAINER_GLOBS):
        update_file(devcontainer, _IMAGE_RE, _FEATURE_RE, get_new_version=get_latest_tag, logger=_LOG)


def main() -> None:  # pragma: no cover
    """Update the images and features in the repository's devcontainer.json files."""
    update_devcontainers()


if __name__ == "__main__":  # pragma: no cover
    main()

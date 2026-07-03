"""Devcontainer updater script finds devcontainer.json files and updates their image and feature references.

A devcontainer.json pins two kinds of OCI artifact: a base `image` (e.g.
`mcr.microsoft.com/devcontainers/typescript-node:1`) and each key under `features` (e.g.
`ghcr.io/devcontainers/features/node:1`). Both are referenced by tag and pinned by appending the tag's digest,
exactly like the Dockerfile and manifest image updaters, and resolved on any OCI registry (`ghcr.io`,
`mcr.microsoft.com`, ...). References through a Dockerfile or Compose file (`build.dockerfile`,
`dockerComposeFile`) are left to those updaters.

The file is edited line by line with the same machinery as the other image updaters rather than parsed as JSON, so
comments and trailing commas (which devcontainer.json allows, and plain JSON forbids) are preserved untouched.
"""

import sys

from update_time.io.filesystem import glob, update_file
from update_time.io.log import get_logger
from update_time.sources.oci import IMAGE_REFERENCE, get_latest_tag

LOG = get_logger("devcontainer")

# The base image, as a JSON string value: `"image": "mcr.microsoft.com/devcontainers/typescript-node:1"`.
IMAGE_RE = rf'"image":\s*"{IMAGE_REFERENCE}"'
# A feature, as a JSON object key: `"ghcr.io/devcontainers/features/node:1": { ... }`. The trailing `: {` anchors
# the match to a feature key (an OCI reference mapping to an options object), so ordinary string values whose text
# happens to look like `name:version` (e.g. `"appPort": "3000:3000"`) are not matched.
FEATURE_RE = rf'"{IMAGE_REFERENCE}":\s*{{'

# Standard devcontainer.json locations: a top-level file, the conventional `.devcontainer/` folder, and
# per-configuration subfolders under it. `glob` visits these dot-paths because they are named in the patterns.
DEVCONTAINER_GLOBS = (".devcontainer.json", ".devcontainer/devcontainer.json", ".devcontainer/*/devcontainer.json")


def update_devcontainers() -> int:
    """Update the base image and feature references in the repository's devcontainer.json files."""
    results = {
        update_file(devcontainer, IMAGE_RE, FEATURE_RE, get_new_version=get_latest_tag, logger=LOG)
        for devcontainer in glob(*DEVCONTAINER_GLOBS)
    }
    return max(results, default=0)


def main() -> int:  # pragma: no cover
    """Update the images and features in the repository's devcontainer.json files."""
    return update_devcontainers()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""Read the base image and feature references of devcontainer.json files.

The patterns match a line at a time rather than the parsed JSON. So a rewrite leaves the comments and trailing commas
untouched, which devcontainer.json allows and plain JSON forbids.
"""

import re

from update_time.manifests.image_reference import IMAGE_REFERENCE, OPTIONALLY_TAGGED_IMAGE_REFERENCE

# The base image, as a JSON string value: `"image": "mcr.microsoft.com/devcontainers/typescript-node:1"`. Its tag
# is optional, the `"image"` key naming what a value without one is, so it is pinned like a Dockerfile's `FROM`.
BASE_IMAGE_REFERENCE = re.compile(rf'"image":\s*"{OPTIONALLY_TAGGED_IMAGE_REFERENCE}"')
# A feature, as a JSON object key: `"ghcr.io/devcontainers/features/node:1": { ... }`. The trailing `: {` anchors
# the match to a feature key (an OCI reference mapping to an options object), so ordinary string values whose text
# happens to look like `name:version` (e.g. `"appPort": "3000:3000"`) are not matched. Here the tag is required,
# since the reference is the key itself: without one, every object key would read as a feature, `"customizations"`
# and a local feature's `"./my-feature"` alike.
FEATURE_REFERENCE = re.compile(rf'"{IMAGE_REFERENCE}":\s*{{')

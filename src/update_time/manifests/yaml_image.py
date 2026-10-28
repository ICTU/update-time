"""Read the images that CI configs and manifests written in YAML name under an `image:` key."""

import re

from update_time.manifests.image_reference import OPTIONALLY_TAGGED_IMAGE_REFERENCE

# An image reference under a YAML `image:` key, as CircleCI, GitLab CI, Docker Compose, and Helm files write it. Its
# tag is optional, an `image:` naming none as readable a reference as one that names a tag.
YAML_IMAGE_REFERENCE = re.compile(rf"image: {OPTIONALLY_TAGGED_IMAGE_REFERENCE}")

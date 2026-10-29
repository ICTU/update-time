"""Read the image references that Dockerfiles, CI configs, manifests, and devcontainer configs write."""

from update_time.domain.reference import VARIABLE
from update_time.primitives.digest import SHA256_DIGEST

# A Docker image reference as it appears in files: `dependency:version` with an optional `@sha256:digest`. A file
# format prefixes it with the keyword that introduces the reference (`FROM `, `image: `). The image name and the tag
# may hold a variable.
_IMAGE_NAME = rf"(?P<dependency>(?:[\w\d\./-]|{VARIABLE})+)"
_TAG_CHARACTER = rf"(?:[\d\w\.\-]|{VARIABLE})"
_IMAGE_DIGEST = rf"(?:@(?P<sha>{SHA256_DIGEST}))?"

# The reference as most file formats write it, its digest optional but its tag required.
IMAGE_REFERENCE = rf"{_IMAGE_NAME}:(?P<version>{_TAG_CHARACTER}+){_IMAGE_DIGEST}"

# The reference for a file format that writes it without a tag as well, such as a Dockerfile's `FROM python`. The
# tag then matches empty, at the position pinning writes it to. A colon is consumed only when a tag follows it. The
# reference ends at whitespace, a double quote, the end of the line, or a YAML flow collection's `,`, `]`, or `}`, so
# a reference holding syntax Update-time does not read as a variable does not match.
_OPTIONALLY_TAGGED_IMAGE = rf"{_IMAGE_NAME}(?::(?={_TAG_CHARACTER}))?(?P<version>{_TAG_CHARACTER}*)"
_REFERENCE_END = r'(?=[\s",\]}]|$)'
OPTIONALLY_TAGGED_IMAGE_REFERENCE = rf"{_OPTIONALLY_TAGGED_IMAGE}{_IMAGE_DIGEST}{_REFERENCE_END}"

# The same reference without a digest.
DIGESTLESS_IMAGE_REFERENCE = rf"{_OPTIONALLY_TAGGED_IMAGE}{_REFERENCE_END}"

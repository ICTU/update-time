"""Get the latest available tags from Docker Hub."""

import os
import re
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from typing import cast

import requests
from packaging.version import InvalidVersion, Version

from update_time.domain.cooldown import within_cooldown
from update_time.domain.version import DependencyName, DependencyVersion, VersionString
from update_time.io.log import get_logger

LOG = get_logger("docker")

# A Docker image reference as it appears in files: `dependency:version` with an optional `@sha256:digest`. Updaters
# prefix this with the keyword that introduces the reference in their file format (e.g. `FROM ` or `image: `). The
# digest is optional but a concrete version tag is required, so references through a variable (`${VAR}`) don't match.
IMAGE_REFERENCE = r"(?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)(?:@(?P<sha>sha256:[a-f0-9]{64}))?"

# Listing tag names is done via the OCI registry's lightweight names-only endpoint (a single request returns every
# tag name), after which only the chosen tag's metadata (digest and push date) is fetched from the Docker Hub API.
OCI_REGISTRY = "https://registry-1.docker.io"
OCI_AUTH_URL = "https://auth.docker.io/token"
DOCKER_HUB_REGISTRY = "https://registry.hub.docker.com"


@dataclass(frozen=True)
class Tag:
    """A result from the Docker Hub tags endpoint."""

    name: str
    digest: str = ""
    last_pushed: datetime | None = None

    @classmethod
    def from_json(cls, tag: dict) -> Tag:
        """Create a Tag from a Docker Hub tags endpoint result."""
        last_pushed = tag.get("tag_last_pushed")
        last_pushed_datetime_or_none = datetime.fromisoformat(last_pushed) if last_pushed else None
        return cls(name=tag["name"], digest=tag.get("digest", ""), last_pushed=last_pushed_datetime_or_none)

    @property
    def version(self) -> Version | None:
        """Return the parsed version, or None if the tag does not contain a valid version."""
        version_string = self.name.split("-", maxsplit=1)[0]
        try:
            return Version(version_string)
        except InvalidVersion:
            return None

    @property
    def suffix(self) -> str:
        """Return the non-version suffix of the tag (e.g., 'slim' in '3.12-slim'), or empty string if none."""
        return self.name.split("-", maxsplit=1)[1] if "-" in self.name else ""

    @property
    def within_cooldown(self) -> bool:
        """Return whether the tag was pushed within the configured cooldown period."""
        return within_cooldown(self.last_pushed)

    def with_version(self, version: Version) -> Tag:
        """Return a new Tag with the same suffix but the given version."""
        name = f"{version}-{self.suffix}" if self.suffix else str(version)
        return Tag(name=name)

    def is_candidate_for(self, current: Tag) -> bool:
        """Return whether this tag (known by name only) is a possible update of the current tag.

        These are the checks that can be made from the tag name alone, used to narrow the set of tags whose
        metadata (digest and push date) is worth fetching.
        """
        if self.version is None:
            return False  # Ignore tags if the version is not valid
        if self.version.is_prerelease:
            return False  # Ignore tags if the version is a prerelease
        if self.suffix != current.suffix:
            return False  # Ignore tags with a different suffix because we don't want to change e.g. fat to slim
        return cast("Version", current.version) <= self.version  # Ignore tags older than the current tag

    @property
    def is_eligible(self) -> bool:
        """Return whether this tag (with metadata fetched) can be used: it has a digest and is past the cooldown.

        The name-only checks have already been made by `is_candidate_for` before the metadata was fetched.
        """
        return bool(self.digest) and not self.within_cooldown


def is_docker_hub_image(image: str) -> bool:
    """Return whether the image reference points to Docker Hub rather than another registry.

    A reference is on Docker Hub when it uses an explicit Docker Hub host (e.g. `docker.io/library/redis`) or no
    registry host at all. Following the rule Docker's reference parser uses to split the registry domain from the
    image name (`splitDockerDomain` in https://github.com/distribution/reference/blob/main/normalize.go), the
    first path component is a registry host when it contains a `.` or a `:`, equals `localhost`, or contains
    an uppercase character (repository namespaces must be lowercase). So `registry.gitlab.com/...`, `gcr.io/...`
    and `localhost:5000/...` are not on Docker Hub; `python`, `cimg/go` and `docker.io/library/redis` are.
    """
    host = image.split("/", maxsplit=1)[0]
    if host.endswith("docker.io"):
        return True
    return "." not in host and ":" not in host and host != "localhost" and host == host.lower()


def get_latest_tag(image: DependencyName, current_tag: VersionString) -> DependencyVersion:
    """Find the latest compatible tag for an image. Keeps the same non-numerical parts while upgrading the version.

    Returns the digest of the resulting tag, including when the current version is already the latest, so that
    unpinned references can be pinned without bumping their version.

    Lists all tag names in one request, then fetches metadata for the highest candidate versions until one is
    eligible (it has a digest and was pushed before the cooldown). Normally that's the very first fetch; only
    versions newer than the latest eligible one (those still within the cooldown) are fetched and skipped.
    """
    if not is_docker_hub_image(image):
        # Images on other registries aren't on Docker Hub; leave them unchanged without making a request.
        return DependencyVersion(version=current_tag)
    current = Tag(name=current_tag)
    if current.version is None:
        # Can't determine a newer tag if the tag doesn't contain a valid version
        return DependencyVersion(version=current_tag)
    tags = [Tag(name=name) for name in _tag_names(image)]
    candidates = [tag for tag in tags if tag.is_candidate_for(current)]
    candidates.sort(key=lambda tag: cast("Version", tag.version), reverse=True)
    for candidate in candidates:
        latest = _get_tag(image, candidate.name)
        if latest is not None and latest.is_eligible:
            name = current.with_version(cast("Version", latest.version)).name
            return DependencyVersion(version=name, sha=latest.digest, published=latest.last_pushed)
    return DependencyVersion(version=current_tag)


def _repository(image: str) -> str:
    """Return the `namespace/repository` path for an image, e.g. `node` -> `library/node`."""
    host, _, remainder = image.partition("/")
    if remainder and host.endswith("docker.io"):
        image = remainder  # Drop the explicit Docker Hub host, e.g. docker.io/library/redis -> library/redis.
    return image if "/" in image else f"library/{image}"


@cache
def _tag_names(image: str) -> list[str]:
    """Fetch all tag names for a Docker image from the OCI registry's names-only listing.

    Some references look like Docker Hub images but aren't, e.g. CircleCI machine images such as `ubuntu-2204`
    (other-registry images are already filtered out by `is_docker_hub_image`). These return a 404 (or another
    error); log the response and skip the image so the reference is left unchanged, rather than crashing the run.
    """
    repository = _repository(image)
    headers = {"Authorization": f"Bearer {_oci_token(repository)}"}
    url: str | None = f"{OCI_REGISTRY}/v2/{repository}/tags/list?n=1000"
    names: list[str] = []
    while url:
        response = requests.get(url, headers=headers, timeout=10)
        if not response.ok:
            LOG.response(response)
            break
        names.extend(response.json().get("tags") or [])
        url = _next_page_url(response)
    return names


def _next_page_url(response: requests.Response) -> str | None:
    """Return the URL of the next page of tag names from the response's Link header, if any."""
    if match := re.search(r'<([^>]+)>\s*;\s*rel="next"', response.headers.get("Link", "")):
        path = match.group(1)
        return f"{OCI_REGISTRY}{path}" if path.startswith("/") else path
    return None


@cache
def _oci_token(repository: str) -> str:
    """Return a pull token for the OCI registry's tag-listing endpoint.

    Authenticates with the DOCKER_HUB_USERNAME/DOCKER_HUB_TOKEN credentials when set (for a higher rate limit),
    like the per-tag metadata requests do; otherwise an anonymous token is requested.
    """
    url = f"{OCI_AUTH_URL}?service=registry.docker.io&scope=repository:{repository}:pull"
    response = requests.get(url, timeout=10, auth=_docker_hub_credentials())
    response.raise_for_status()
    return response.json()["token"]


@cache
def _get_tag(image: str, name: str) -> Tag | None:
    """Fetch a single tag's metadata (digest and push date) from the Docker Hub API, or None if it can't be found."""
    namespace, repository = _repository(image).split("/", maxsplit=1)
    url = f"{DOCKER_HUB_REGISTRY}/v2/namespaces/{namespace}/repositories/{repository}/tags/{name}"
    response = requests.get(url, headers=_docker_hub_headers(), timeout=10)
    if not response.ok:
        LOG.response(response)
        return None
    return Tag.from_json(response.json())


def _docker_hub_credentials() -> tuple[str, str] | None:
    """Return the (username, token) Docker Hub credentials if both DOCKER_HUB_USERNAME and DOCKER_HUB_TOKEN are set."""
    username = os.environ.get("DOCKER_HUB_USERNAME")
    token = os.environ.get("DOCKER_HUB_TOKEN")
    return (username, token) if username and token else None


@cache
def _docker_hub_headers() -> dict[str, str]:
    """Return Docker Hub API request headers with bearer token if DOCKER_HUB_USERNAME and DOCKER_HUB_TOKEN are set."""
    if credentials := _docker_hub_credentials():
        username, token = credentials
        url = "https://hub.docker.com/v2/auth/token"
        response = requests.post(url, timeout=10, json={"identifier": username, "secret": token})
        response.raise_for_status()
        return {"Authorization": f"Bearer {response.json()['access_token']}"}
    return {}

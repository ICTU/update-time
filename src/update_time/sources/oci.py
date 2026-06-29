"""Get the latest available image tags from OCI registries (Docker Hub and others).

This is a generic OCI distribution client: it resolves image references on any registry (Docker Hub, ghcr.io,
mcr.microsoft.com, quay.io, ...) by listing tag names, discovering the registry's auth via the OCI
`WWW-Authenticate` challenge, and reading the digest to pin from the tag's manifest. Docker-Hub-specific behavior
(the push date the cooldown relies on, and credentials that raise the rate limit) lives in `docker_hub`.
"""

import re
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, cast

import requests
from packaging.version import InvalidVersion, Version

from update_time.domain.cooldown import within_cooldown
from update_time.domain.version import DependencyName, DependencyVersion, VersionString
from update_time.io.log import get_logger
from update_time.sources import docker_hub

if TYPE_CHECKING:
    from datetime import datetime

LOG = get_logger("oci")

# A Docker image reference as it appears in files: `dependency:version` with an optional `@sha256:digest`. Updaters
# prefix this with the keyword that introduces the reference in their file format (e.g. `FROM ` or `image: `). The
# digest is optional but a concrete version tag is required, so references through a variable (`${VAR}`) don't match.
IMAGE_REFERENCE = r"(?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)(?:@(?P<sha>sha256:[a-f0-9]{64}))?"

# Image references without a registry host are on Docker Hub, whose OCI registry host differs from the user-facing
# `docker.io` alias and whose images live under an implicit `library/` namespace.
DOCKER_HUB_OCI_HOST = "registry-1.docker.io"

# Media types offered when resolving a manifest digest, so the registry returns the multi-arch index digest (the
# digest to pin) when the image is multi-arch, and the single image-manifest digest otherwise.
MANIFEST_MEDIA_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)


@dataclass(frozen=True)
class Tag:
    """An image tag: name-only when listed, with a digest and (Docker Hub only) push date once resolved."""

    name: str
    digest: str = ""
    last_pushed: datetime | None = None

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
        """Return whether the tag was pushed within the configured cooldown period.

        Only Docker Hub exposes a push date; for tags without one (other registries) no cooldown is applied.
        """
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


def _split_domain(image: str) -> tuple[str | None, str]:
    """Split an image reference into its registry host and the remaining repository path.

    Following the rule Docker's reference parser uses to split the registry domain from the image name
    (`splitDockerDomain` in https://github.com/distribution/reference/blob/main/normalize.go), the first path
    component is a registry host when it contains a `.` or `:`, equals `localhost`, or contains an uppercase
    character (repository namespaces must be lowercase). When there is no such host the image is on Docker Hub
    (under the implicit `library/` namespace) and the host is returned as None with the full reference as the path.
    So `registry.gitlab.com/group/image` -> (`registry.gitlab.com`, `group/image`) and `cimg/go` -> (None, `cimg/go`).
    """
    first, _, remainder = image.partition("/")
    if "." in first or ":" in first or first == "localhost" or first != first.lower():
        return first, remainder
    return None, image


def is_docker_hub_image(image: str) -> bool:
    """Return whether the image reference points to Docker Hub rather than another registry.

    A reference is on Docker Hub when it uses an explicit Docker Hub host (e.g. `docker.io/library/redis`) or no
    registry host at all. So `registry.gitlab.com/...`, `gcr.io/...` and `localhost:5000/...` are not on Docker
    Hub; `python`, `cimg/go` and `docker.io/library/redis` are.
    """
    host, _ = _split_domain(image)
    return host is None or host.endswith("docker.io")


def get_latest_tag(image: DependencyName, current_tag: VersionString) -> DependencyVersion:
    """Find the latest compatible tag for an image. Keeps the same non-numerical parts while upgrading the version.

    Resolves images on any OCI registry (Docker Hub, ghcr.io, mcr.microsoft.com, quay.io, ...). Returns the digest
    of the resulting tag, including when the current version is already the latest, so that unpinned references can
    be pinned without bumping their version.

    Lists all tag names in one request, then resolves the digest for the highest candidate versions until one is
    eligible (it has a digest and is past the cooldown). Normally that's the very first one; only versions newer
    than the latest eligible one (those still within Docker Hub's cooldown) are resolved and skipped.
    """
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


def _registry_host(image: str) -> str:
    """Return the OCI registry API host for an image reference, defaulting to Docker Hub's registry."""
    host, _ = _split_domain(image)
    return DOCKER_HUB_OCI_HOST if host is None or host.endswith("docker.io") else host


def _repository(image: str) -> str:
    """Return the `namespace/repository` path for an image, e.g. `node` -> `library/node`."""
    host, remainder = _split_domain(image)
    if host and host.endswith("docker.io"):
        image = remainder  # Drop the explicit Docker Hub host, e.g. docker.io/library/redis -> library/redis.
    return image if "/" in image else f"library/{image}"


def _credentials(image: str) -> tuple[str, str] | None:
    """Return Docker Hub credentials (for a higher rate limit) when the image is on Docker Hub, else None.

    Docker Hub is currently the only registry we hold credentials for; all other registries are queried anonymously.
    """
    return docker_hub.credentials() if is_docker_hub_image(image) else None


@cache
def _tag_names(image: str) -> list[str]:
    """Fetch all tag names for an image from its OCI registry's names-only listing.

    Some references look like images but aren't, e.g. CircleCI machine images such as `ubuntu-2204`, or `${VAR}`
    substitutions and other registries' private images we can't authenticate for. These return a 404 (or another
    error); log the response and skip the image so the reference is left unchanged, rather than crashing the run.
    """
    host = _registry_host(image)
    repository = _repository(image)
    headers = _auth_headers(host, repository, _credentials(image))
    url: str | None = f"https://{host}/v2/{repository}/tags/list?n=1000"
    names: list[str] = []
    while url:
        response = requests.get(url, headers=headers, timeout=10)
        if not response.ok:
            LOG.response(response)
            break
        names.extend(response.json().get("tags") or [])
        url = _next_page_url(response, host)
    return names


def _next_page_url(response: requests.Response, host: str) -> str | None:
    """Return the URL of the next page of tag names from the response's Link header, if any."""
    if match := re.search(r'<([^>]+)>\s*;\s*rel="next"', response.headers.get("Link", "")):
        path = match.group(1)
        return f"https://{host}{path}" if path.startswith("/") else path
    return None


@cache
def _get_tag(image: str, name: str) -> Tag | None:
    """Resolve a tag's digest (from its OCI manifest) and push date (Docker Hub only), or None if it has no digest.

    The digest comes from the OCI manifest, which works on every registry. The publish date needed for the cooldown
    is only available from Docker Hub's proprietary API (the OCI protocol exposes none; see `docker_hub`), so other
    registries' tags have no publish date and no cooldown.
    """
    digest = _manifest_digest(image, name)
    if not digest:
        return None
    pushed = docker_hub.last_pushed(_repository(image), name) if is_docker_hub_image(image) else None
    return Tag(name=name, digest=digest, last_pushed=pushed)


def _manifest_digest(image: str, tag: str) -> str:
    """Return the digest to pin for an image tag, read from its OCI manifest, or empty string if unavailable.

    A `HEAD` on the manifest returns the canonical digest in the `Docker-Content-Digest` header (the multi-arch
    index digest when the Accept header offers the index/list media types). This works on every OCI registry.
    """
    host = _registry_host(image)
    repository = _repository(image)
    headers = {"Accept": ", ".join(MANIFEST_MEDIA_TYPES), **_auth_headers(host, repository, _credentials(image))}
    response = requests.head(f"https://{host}/v2/{repository}/manifests/{tag}", headers=headers, timeout=10)
    if not response.ok:
        LOG.response(response)
        return ""
    return response.headers.get("Docker-Content-Digest", "")


def _auth_headers(host: str, repository: str, credentials: tuple[str, str] | None) -> dict[str, str]:
    """Return the Authorization header for pulling from the registry, or no header for anonymous access."""
    token = _registry_token(host, repository, credentials)
    return {"Authorization": f"Bearer {token}"} if token else {}


@cache
def _registry_token(host: str, repository: str, credentials: tuple[str, str] | None = None) -> str | None:
    """Discover and fetch a pull token for the registry via the OCI `WWW-Authenticate` challenge.

    Probe `https://<host>/v2/`; a registry that requires auth replies `401` with a `WWW-Authenticate: Bearer
    realm=...,service=...` header pointing at its token endpoint. Fetch a token from that realm, scoped to pull the
    repository. This auto-discovers each registry's auth (Docker Hub's `auth.docker.io`, ghcr.io's token endpoint,
    ...). Registries that allow anonymous access (e.g. mcr.microsoft.com) don't challenge, so None is returned and
    requests are made without a token. The given credentials, if any, authenticate the token request.
    """
    probe = requests.get(f"https://{host}/v2/", timeout=10)
    challenge = probe.headers.get("WWW-Authenticate", "")
    if probe.status_code != requests.codes.unauthorized or not challenge.lower().startswith("bearer "):
        return None  # Anonymous registry (or an unexpected response): proceed without a token.
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.pop("realm", "")
    params["scope"] = f"repository:{repository}:pull"
    response = requests.get(realm, params=params, timeout=10, auth=credentials)
    response.raise_for_status()
    token = response.json()
    return token.get("token") or token.get("access_token")

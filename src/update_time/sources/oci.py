"""Get the latest available image tags from OCI registries (Docker Hub and others).

This is a generic OCI distribution client: it resolves image references on any registry (Docker Hub, ghcr.io,
mcr.microsoft.com, quay.io, ...) by listing tag names, discovering the registry's auth via the OCI
`WWW-Authenticate` challenge, and reading the digest to pin from the tag's manifest. Docker-Hub-specific behavior
(the push date the cooldown relies on, and credentials that raise the rate limit) lives in `docker_hub`.
"""

import re
from dataclasses import dataclass
from functools import cache, cached_property
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

from packaging.version import InvalidVersion, Version

from update_time.domain.cooldown import within_cooldown
from update_time.domain.version import DependencyName, DependencyVersion, VersionString, first_eligible
from update_time.io.fetch import fetch, next_page_url
from update_time.io.log import get_logger
from update_time.sources import docker_hub

if TYPE_CHECKING:
    from datetime import datetime

LOG = get_logger("oci")

# A Docker image reference as it appears in files: `dependency:version` with an optional `@sha256:digest`. Updaters
# prefix this with the keyword that introduces the reference in their file format (e.g. `FROM ` or `image: `). The
# digest is optional but a concrete version tag is required, so references through a variable (`${VAR}`) don't match.
IMAGE_REFERENCE = r"(?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)(?:@(?P<sha>sha256:[a-f0-9]{64}))?"

# The same reference under a YAML `image:` key, shared by the CircleCI, GitLab CI, Docker Compose, and Helm updaters.
YAML_IMAGE_REFERENCE = rf"image: {IMAGE_REFERENCE}"

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

# The structure of an image tag: an optional non-numeric label prefix, a version, and an optional suffix, e.g.
# `python3.12-bookworm-slim` -> prefix `python`, version `3.12`, suffix `bookworm-slim`. Prefix and version both
# exclude `-`, so the version ends where the suffix begins; numeric-leading tags (`3.12-slim`) get an empty prefix.
# Tags without a version (e.g. `bookworm-slim`) don't match at all; `version` still validates the match with packaging.
# The prefix and version classes are possessive (`*+`) so the engine never backtracks across them: neither can give
# back characters to salvage a failing match (a non-digit before `\d`, or a dash inside the version), which keeps
# matching linear.
_TAG = re.compile(r"(?P<prefix>[^\d-]*+)(?P<version>\d[^-]*+)-?(?P<suffix>.*)$")


@dataclass(frozen=True)
class Tag:
    """An image tag: name-only when listed, with a digest and (Docker Hub only) push date once resolved."""

    name: str
    digest: str = ""
    last_pushed: datetime | None = None

    @cached_property
    def _match(self) -> re.Match[str] | None:
        """Parse the tag name into its prefix, version, and suffix once, or None when the tag has no version."""
        return _TAG.match(self.name)

    @property
    def prefix(self) -> str:
        """Return the non-numeric label before the version (e.g. 'python' in 'python3.12-slim'), or '' if none.

        Language/runtime images prefix the version with a label. The prefix is kept when bumping and must match
        between the current tag and a candidate, so e.g. `python3.x` is never replaced by `pypy3.x`.
        """
        return self._match.group("prefix") if self._match else ""

    @cached_property
    def version(self) -> Version | None:
        """Return the parsed version, or None if the tag does not contain a valid version.

        A leading label prefix (e.g. the `python` in `python3.12`) is stripped before parsing the version.
        """
        if not (match := self._match):
            return None
        try:
            return Version(match.group("version"))
        except InvalidVersion:
            return None

    @property
    def suffix(self) -> str:
        """Return the non-version suffix of the tag (e.g., 'slim' in '3.12-slim'), or empty string if none.

        Only meaningful for a tag that has a version (it's compared between a candidate and the current tag, both of
        which have one); a tag without a version doesn't match and the suffix is reported as empty.
        """
        return self._match.group("suffix") if self._match else ""

    @property
    def within_cooldown(self) -> bool:
        """Return whether the tag was pushed within the configured cooldown period.

        Only Docker Hub exposes a push date; for tags without one (other registries) no cooldown is applied.
        """
        return within_cooldown(self.last_pushed)

    def with_version(self, version: Version) -> Tag:
        """Return a new Tag with the same prefix and suffix but the given version."""
        name = f"{self.prefix}{version}"
        if self.suffix:
            name += f"-{self.suffix}"
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
        if self.prefix != current.prefix:
            return False  # Ignore tags with a different prefix so e.g. python3.x isn't replaced by pypy3.x
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


def _is_docker_hub_host(host: str | None) -> bool:
    """Return whether an OCI registry host is Docker Hub's, including a hostless reference (host None)."""
    return host is None or host.endswith("docker.io")


def is_docker_hub_image(image: str) -> bool:
    """Return whether the image reference points to Docker Hub rather than another registry.

    A reference is on Docker Hub when it uses an explicit Docker Hub host (e.g. `docker.io/library/redis`) or no
    registry host at all. So `registry.gitlab.com/...`, `gcr.io/...` and `localhost:5000/...` are not on Docker
    Hub; `python`, `cimg/go` and `docker.io/library/redis` are.
    """
    host, _ = _split_domain(image)
    return _is_docker_hub_host(host)


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
    return first_eligible(candidates, lambda candidate: _eligible_tag(image, current, candidate), current_tag)


def _eligible_tag(image: str, current: Tag, candidate: Tag) -> DependencyVersion | None:
    """Resolve the candidate's digest and push date and return it when eligible, or None when it isn't."""
    latest = _get_tag(image, candidate.name)
    if latest is None or not latest.is_eligible:
        return None
    name = current.with_version(cast("Version", latest.version)).name
    return DependencyVersion(version=name, sha=latest.digest, published=latest.last_pushed)


def _registry_host(image: str) -> str:
    """Return the OCI registry API host for an image reference, defaulting to Docker Hub's registry."""
    host, _ = _split_domain(image)
    # In the else branch `_is_docker_hub_host` was False, so `host` is a real (non-None) registry host.
    return DOCKER_HUB_OCI_HOST if _is_docker_hub_host(host) else cast("str", host)


def _repository(image: str) -> str:
    """Return the `namespace/repository` path for an image, relative to its registry host.

    The repository path in the registry API is host-relative, so an explicit registry host is dropped, e.g.
    `ghcr.io/devcontainers/features/node` -> `devcontainers/features/node` and `docker.io/library/redis` ->
    `library/redis`. Dropping it for every registry (not just Docker Hub) matters because strict registries such as
    `mcr.microsoft.com` return a 404 for a host-prefixed path. A hostless reference is on Docker Hub, where a
    single-segment name lives under the implicit `library/` namespace (`node` -> `library/node`).
    """
    host, remainder = _split_domain(image)
    path = image if host is None else remainder
    if _is_docker_hub_host(host):
        return path if "/" in path else f"library/{path}"
    return path


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
        response = fetch(url, LOG, headers=headers)
        if response is None:
            break
        names.extend(response.json().get("tags") or [])
        url = next_page_url(response)
    return names


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
    response = fetch(f"https://{host}/v2/{repository}/manifests/{tag}", LOG, method="head", headers=headers)
    if response is None:
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
    probe = fetch(f"https://{host}/v2/", LOG, require_ok=False)
    if probe is None:
        return None
    challenge = probe.headers.get("WWW-Authenticate", "")
    if probe.status_code != HTTPStatus.UNAUTHORIZED or not challenge.lower().startswith("bearer "):
        return None  # Anonymous registry (or an unexpected response): proceed without a token.
    # Pull out each `key="value"` pair; the key and quoted value each scan linearly with no nested quantifiers.
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.pop("realm", "")
    params["scope"] = f"repository:{repository}:pull"
    response = fetch(realm, LOG, params=params, auth=credentials)
    if response is None:
        return None
    token = response.json()
    return token.get("token") or token.get("access_token")

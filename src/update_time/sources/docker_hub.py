"""Docker Hub specifics: credentials and the proprietary push-date API the cooldown and staleness check rely on.

The OCI protocol exposes no publish date. Its optional `org.opencontainers.image.created` annotation dates the
image's *build*, not the push of its tag. Reproducible builds commonly zero that annotation (`SOURCE_DATE_EPOCH`),
so it is no substitute. Docker Hub's proprietary tags API is the only source of a real push date, so the
cooldown can only be enforced for Docker Hub images, and only there can an image be dated for the staleness check.
This module isolates everything Docker-Hub-specific. The generic OCI client in `oci` uses this module for those
push dates, for the digests Docker Hub's listing gives, and for credentials, which raise the rate limit.
"""

import os
from functools import cache
from typing import TYPE_CHECKING, NotRequired, TypedDict

from update_time.io.fetch import fetch
from update_time.io.log import get_logger
from update_time.primitives.timestamp import parse_timestamp

if TYPE_CHECKING:
    from datetime import datetime

_LOG = get_logger("docker hub")

# Docker Hub's proprietary tags API; the only source of a tag's real push date (the OCI protocol exposes none).
_REGISTRY = "https://registry.hub.docker.com"

# The token endpoint the credentials are exchanged at; note that it lives on the user-facing hub.docker.com host,
# not on the _REGISTRY host above.
_AUTH_URL = "https://hub.docker.com/v2/auth/token"


def credentials() -> tuple[str, str] | None:
    """Return the (username, token) Docker Hub credentials if both DOCKER_HUB_USERNAME and DOCKER_HUB_TOKEN are set."""
    username = os.environ.get("DOCKER_HUB_USERNAME")
    token = os.environ.get("DOCKER_HUB_TOKEN")
    return (username, token) if username and token else None


@cache
def api_headers() -> dict[str, str]:
    """Return Docker Hub API request headers, with a bearer token when the token endpoint answers with one.

    A run that sets no DOCKER_HUB_USERNAME and DOCKER_HUB_TOKEN leaves the requests anonymous. So does an answer
    that carries no token, which Docker Hub's schema allows, since it makes the token an optional field.
    """
    if creds := credentials():
        username, token = creds
        response = fetch(_AUTH_URL, _LOG, method="post", json={"identifier": username, "secret": token})
        if response is not None and (access_token := response.json().get("access_token")):
            return {"Authorization": f"Bearer {access_token}"}
    return {}


class _TagJSON(TypedDict):
    """One tag as Docker Hub's tag listing carries it."""

    name: str
    digest: NotRequired[str]
    tag_last_pushed: NotRequired[str]


# The page size of Docker Hub's tag listing, which is also the largest page it serves.
_TAG_LISTING_PAGE_SIZE = 100

# How many pages of that listing are read at most, so that a repository with thousands of tags cannot turn one
# reference into dozens of requests. A tag is re-pushed with every release it follows, so it sorts high in a listing
# ordered by push date; a tag listed further down than these pages reach is left as it is.
_MAX_TAG_LISTING_PAGES = 5


def tag_digests(repository: str, tag: str) -> dict[str, str]:
    """Return the digest each listed tag of a Docker Hub repository serves, read until `tag`'s aliases are listed.

    `repository` is the `namespace/repository` path (e.g. `library/python`). The OCI listing gives tag names only,
    so this is the only listing that says which tags serve one digest. The listing is ordered by push date and a
    tag is pushed together with the other tags serving its digest, so reading stops at the first page holding none
    of them, and at `_MAX_TAG_LISTING_PAGES` pages whatever the listing holds.
    """
    url = _listing_url(repository)
    digests: dict[str, str] = {}
    for _page in range(_MAX_TAG_LISTING_PAGES):
        entries, url = _listing_page(url)
        page = {entry["name"]: entry.get("digest", "") for entry in entries}
        digests |= page
        if not url or _lists_every_alias(digests, page, tag):
            break
    return digests


def newest_pushes(repository: str) -> dict[str, datetime]:
    """Return each tag on the first page of a Docker Hub repository's listing, with the date it was pushed.

    The listing is ordered by push date, so its first page holds the repository's most recent pushes, which is
    what dates the dependency. One page answers that, however many tags the repository has. A tag without a push
    date is left out.
    """
    entries, _next_page = _listing_page(_listing_url(repository))
    return {
        entry["name"]: published
        for entry in entries
        if (published := parse_timestamp(entry.get("tag_last_pushed"))) is not None
    }


def _listing_url(repository: str) -> str:
    """Return the URL of the first page of a Docker Hub repository's tag listing."""
    return f"{_tags_url(repository)}?page_size={_TAG_LISTING_PAGE_SIZE}"


def _tags_url(repository: str) -> str:
    """Return the URL of a Docker Hub repository's tags, which the listing and each tag's metadata hang off.

    `repository` is the `namespace/repository` path (e.g. `library/redis`), which the API splits into two segments.
    """
    namespace, repo = repository.split("/", maxsplit=1)
    return f"{_REGISTRY}/v2/namespaces/{namespace}/repositories/{repo}/tags"


@cache
def _listing_page(url: str) -> tuple[tuple[_TagJSON, ...], str]:
    """Return the tags listed on one page, and the URL of the page after it.

    Cached by page rather than by reference, so a repository holding a second floating tag reads the pages the
    first one read without asking the registry for them again. A page that can't be fetched is logged and read as
    no tags at all, which ends the reading.
    """
    response = fetch(url, _LOG, headers=api_headers())
    if response is None:
        return (), ""
    listing = response.json()
    return tuple(listing.get("results") or []), listing.get("next") or ""


def _lists_every_alias(digests: dict[str, str], page: dict[str, str], tag: str) -> bool:
    """Return whether every tag serving the tag's digest has been listed, so the pages after this one hold none.

    The listing is ordered by push date, and the tags serving one digest are pushed together but interleaved with
    the tags of other digests: `python:latest` is listed at position 0 with its aliases at 49, 51 and 57. So the
    run of them ends at the first page holding none, and what follows was pushed earlier still. Until the tag
    itself is listed there is no digest to compare against, so reading goes on.
    """
    digest = digests.get(tag)
    return bool(digest) and digest not in page.values()


def last_pushed(repository: str, tag: str) -> datetime | None:
    """Return the push date of a Docker Hub tag from Docker Hub's proprietary API, used only for the cooldown.

    `repository` is the `namespace/repository` path (e.g. `library/redis`). This proprietary API is the only source
    of a real push date (see the module docstring); when it can't be fetched the response is logged and None is
    returned, which means no cooldown is applied to the tag.
    """
    response = fetch(f"{_tags_url(repository)}/{tag}", _LOG, headers=api_headers())
    if response is None:
        return None
    return parse_timestamp(response.json().get("tag_last_pushed"))

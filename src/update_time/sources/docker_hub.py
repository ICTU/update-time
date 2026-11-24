"""Docker Hub specifics: credentials, the proprietary push dates the cooldown and staleness check rely on, and archival.

The OCI protocol exposes no publish date. Its optional `org.opencontainers.image.created` annotation dates the
image's *build*, not the push of its tag. Reproducible builds commonly zero that annotation (`SOURCE_DATE_EPOCH`),
so it is no substitute. Docker Hub's proprietary tags API is the only source of a real push date, so the
cooldown can only be enforced for Docker Hub images, and only there can an image be dated for the staleness check.

This module isolates everything Docker-Hub-specific. The generic OCI client in `oci` uses this module for those
push dates, for the digests Docker Hub's listing gives, for a repository's archival, and for credentials, which raise
the rate limit.
"""

import os
from dataclasses import dataclass
from functools import cache
from http import HTTPStatus
from typing import TYPE_CHECKING, Final, NotRequired, TypedDict

from update_time.domain.dependency import Archival, ArchivedSubject
from update_time.io.fetch import fetch_json
from update_time.io.log import get_logger
from update_time.primitives.lookup import LookedUp
from update_time.primitives.timestamp import parse_timestamp

if TYPE_CHECKING:
    from datetime import datetime

    from update_time.io.log import Logger

_LOG: Final[Logger] = get_logger("docker hub")

# The host of Docker Hub's proprietary API, which dates a tag's push (the OCI protocol exposes no push date) and says
# whether a repository is archived.
_REGISTRY: Final[str] = "https://registry.hub.docker.com"

# The token endpoint the credentials are exchanged at; note that it lives on the user-facing hub.docker.com host,
# not on the _REGISTRY host above.
_AUTH_URL: Final[str] = "https://hub.docker.com/v2/auth/token"

# The environment variables holding the credentials, which raise the rate limit.
USERNAME_VARIABLE: Final[str] = "DOCKER_HUB_USERNAME"
TOKEN_VARIABLE: Final[str] = "DOCKER_HUB_TOKEN"  # noqa: S105 # nosec: B105


# The page size of Docker Hub's tag listing, which is also the largest page it serves.
_TAG_LISTING_PAGE_SIZE: Final[int] = 100

# How many pages of that listing are read at most, so that a repository with thousands of tags cannot turn one
# reference into dozens of requests. A tag is re-pushed with every release it follows, so it sorts high in a listing
# ordered by push date; a tag listed further down than these pages reach is left as it is.
_MAX_TAG_LISTING_PAGES: Final[int] = 5


# Why a tag's push date is unknown when Docker Hub answered without one.
_NO_PUSH_DATE: Final[str] = "Docker Hub did not report a push date"


def tag_listing(repository: str, tag: str) -> LookedUp[_TagListing]:
    """Return a Docker Hub repository's tag listing, read until it holds every tag serving the tag's digest.

    `repository` is the `namespace/repository` path (e.g. `library/python`). Reading stops after
    `_MAX_TAG_LISTING_PAGES` pages, so a tag the listing lacks may sit on a later page. The listing is absent where
    Docker Hub does not serve the repository.
    """
    url = _listing_url(repository)
    digests: dict[str, str] = {}
    pushes: dict[str, datetime] = {}
    for _page in range(_MAX_TAG_LISTING_PAGES):
        if (listing_page := _listing_page(url).value) is None:
            return LookedUp(None, absent=True)
        url = listing_page.next_url
        page = {entry["name"]: entry.get("digest", "") for entry in listing_page.entries}
        digests |= page
        pushes |= _push_dates(listing_page.entries)
        if not url or _lists_every_alias(digests, page, tag):
            break
    return LookedUp(_TagListing(digests=digests, pushes=pushes, examined_all=not url))


def _listing_url(repository: str) -> str:
    """Return the URL of the first page of a Docker Hub repository's tag listing."""
    return f"{_tags_url(repository)}?page_size={_TAG_LISTING_PAGE_SIZE}"


def _tags_url(repository: str) -> str:
    """Return the URL of a Docker Hub repository's tags, which the listing and each tag's metadata hang off."""
    return f"{_repository_url(repository)}/tags"


def _repository_url(repository: str) -> str:
    """Return the URL of a Docker Hub repository.

    `repository` is the `namespace/repository` path (e.g. `library/redis`), which the API splits into two segments.
    """
    namespace, repo = repository.split("/", maxsplit=1)
    return f"{_REGISTRY}/v2/namespaces/{namespace}/repositories/{repo}"


@cache
def _listing_page(url: str) -> LookedUp[_ListingPage]:
    """Return one page of the listing, cached so a second floating tag of the repository reuses the pages read.

    A 404 means Docker Hub does not serve the repository, so the page is absent. Any other failure is logged, and
    reads as a page listing nothing.
    """
    found = fetch_json(
        url, _LOG, body=_TagListingJSON, headers=_api_headers(), statuses_meaning_absent=(HTTPStatus.NOT_FOUND,)
    )
    if found.absent:
        return found.failure()
    if (listing := found.value) is None:
        return LookedUp(_ListingPage())
    return LookedUp(_ListingPage(entries=tuple(listing.get("results") or []), next_url=listing.get("next") or ""))


class _TagListingJSON(TypedDict):
    """A page of Docker Hub's tag listing: its tags, and the URL of the page after it, null on the last page."""

    results: NotRequired[list[_TagJSON] | None]
    next: NotRequired[str | None]


@cache
def _api_headers() -> dict[str, str]:
    """Return Docker Hub API request headers, with a bearer token when the token endpoint answers with one.

    A run that sets no DOCKER_HUB_USERNAME and DOCKER_HUB_TOKEN leaves the requests anonymous. So does an answer
    that carries no token, which Docker Hub's schema allows, since it makes the token an optional field.
    """
    if creds := credentials():
        username, token = creds
        answer = fetch_json(
            _AUTH_URL, _LOG, body=_LoginJSON, method="post", json={"identifier": username, "secret": token}
        ).value
        if answer is not None and (access_token := answer.get("access_token")):
            return {"Authorization": f"Bearer {access_token}"}
    return {}


def credentials() -> tuple[str, str] | None:
    """Return the (username, token) Docker Hub credentials if both DOCKER_HUB_USERNAME and DOCKER_HUB_TOKEN are set."""
    username = os.environ.get(USERNAME_VARIABLE)
    token = os.environ.get(TOKEN_VARIABLE)
    return (username, token) if username and token else None


class _LoginJSON(TypedDict):
    """Docker Hub's answer to a login with the credentials."""

    access_token: NotRequired[str]


@dataclass(frozen=True, slots=True, kw_only=True)
class _ListingPage:
    """One page of a Docker Hub repository's listing: its tags, and the URL of the page after it."""

    entries: tuple[_TagJSON, ...] = ()
    next_url: str = ""


def _push_dates(entries: tuple[_TagJSON, ...]) -> dict[str, datetime]:
    """Return each tag listed with the date it was pushed, leaving out a tag without a push date."""
    return {
        entry["name"]: published
        for entry in entries
        if (published := parse_timestamp(entry.get("tag_last_pushed"))) is not None
    }


def _lists_every_alias(digests: dict[str, str], page: dict[str, str], tag: str) -> bool:
    """Return whether every tag serving the tag's digest has been listed, so the pages after this one hold none.

    The listing is ordered by push date, and the tags serving one digest are pushed together but interleaved with
    the tags of other digests: `python:latest` is listed at position 0 with its aliases at 49, 51 and 57. So the
    run of them ends at the first page holding none, and what follows was pushed earlier still. Until the tag
    itself is listed there is no digest to compare against, so reading goes on.
    """
    digest = digests.get(tag)
    return bool(digest) and digest not in page.values()


@dataclass(frozen=True, slots=True, kw_only=True)
class _TagListing:
    """A Docker Hub repository's listed tags, their digests and push dates, and whether the listing was read out."""

    digests: dict[str, str]
    pushes: dict[str, datetime]
    examined_all: bool

    def pushed(self, tag: str) -> LookedUp[datetime]:
        """Return when the listed tag was pushed, or why the listing can't say."""
        return LookedUp(self.pushes[tag]) if tag in self.pushes else LookedUp(None, _NO_PUSH_DATE)


def newest_pushes(repository: str) -> dict[str, datetime]:
    """Return each tag on the first page of a Docker Hub repository's listing, with the date it was pushed.

    The listing is ordered by push date, so its first page holds the repository's most recent pushes, which is
    what dates the dependency. One page answers that, however many tags the repository has. A tag without a push
    date is left out.
    """
    listing_page = _listing_page(_listing_url(repository)).value
    return _push_dates(listing_page.entries) if listing_page else {}


# The status Docker Hub's repository endpoint gives an archived repository.
_ARCHIVED_STATUS: Final[str] = "archived"


@cache
def archival(repository: str) -> Archival | None:
    """Return what Docker Hub declares about the repository: whether it is archived, or None where it can't be read.

    `repository` is the `namespace/repository` path (e.g. `nginxinc/nginx-unprivileged`).
    """
    url = _repository_url(repository)
    found = fetch_json(
        url,
        _LOG,
        body=_DockerHubRepositoryJSON,
        headers=_api_headers(),
        statuses_meaning_absent=(HTTPStatus.NOT_FOUND,),
    )
    if (metadata := found.value) is None:
        return None
    return Archival(archived=metadata.get("status_description") == _ARCHIVED_STATUS, subject=ArchivedSubject.REPOSITORY)


class _DockerHubRepositoryJSON(TypedDict):
    """What Docker Hub's repository endpoint declares about a repository."""

    status_description: NotRequired[str]


def last_pushed(repository: str, tag: str) -> LookedUp[datetime]:
    """Return the push date of a Docker Hub tag.

    `repository` is the `namespace/repository` path (e.g. `library/redis`).
    """
    found = fetch_json(f"{_tags_url(repository)}/{tag}", _LOG, body=_TagJSON, headers=_api_headers())
    if found.value is None:
        return found.failure()
    if (pushed := parse_timestamp(found.value.get("tag_last_pushed"))) is None:
        return LookedUp(None, _NO_PUSH_DATE)
    return LookedUp(pushed)


class _TagJSON(TypedDict):
    """One tag as Docker Hub's tag listing carries it."""

    name: str
    digest: NotRequired[str]
    tag_last_pushed: NotRequired[str]

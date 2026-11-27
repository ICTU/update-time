"""Shared HTTP helpers: the one place the tool reaches the network.

Every source talks to a flaky external registry, so a single slow or unreachable host must not abort the whole run
with a traceback. `fetch` turns transport-level failures (timeouts, connection errors) into a logged `None`, so the
caller can leave the reference unchanged and the run continues. It lives in `io` (next to the file and process I/O)
so that network access is centralized: sources and updaters go through it rather than calling `requests` directly,
which the architecture tests enforce.
"""

import threading
from contextlib import suppress
from typing import TYPE_CHECKING, Final, TypedDict, Unpack
from urllib.parse import urljoin

import requests

from update_time.formats.json import FORMAT
from update_time.primitives.lookup import LookedUp

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

    from .log import Logger

# A session reuses its connections. Each thread gets a session of its own, since requests does not document a session
# as thread-safe.
_SESSIONS: Final[threading.local] = threading.local()

type Response = requests.Response
type _Fetched = Response | None


class _Request(TypedDict, total=False):
    """What a request sends besides its URL."""

    headers: Mapping[str, str]
    params: Mapping[str, str]
    auth: tuple[str, str] | None
    json: object  # simplint: ignore[imprecise-annotation] # a TypedDict does not fit a JSON type


def fetch_json[Body](
    url: str,
    logger: Logger,
    *,
    body: type[Body],
    method: str = "get",
    statuses_meaning_absent: Collection[int] = (),
    **request: Unpack[_Request],
) -> LookedUp[Body]:
    """Return the JSON body of the URL's response as the body type, or why the request failed, logged unless absent."""
    found = look_up(url, logger, method=method, statuses_meaning_absent=statuses_meaning_absent, **request)
    return json_body(found, logger, body=body)


def look_up(
    url: str,
    logger: Logger,
    *,
    method: str = "get",
    statuses_meaning_absent: Collection[int] = (),
    on_response: Callable[[Response], None] | None = None,
    **request: Unpack[_Request],
) -> LookedUp[Response]:
    """Return the URL's OK response, or why the request failed, logged unless its status means absent."""
    if (response := fetch(url, logger, method=method, require_ok=False, on_response=on_response, **request)) is None:
        return LookedUp(None, _failure_reason(None))
    if response.status_code in statuses_meaning_absent:
        return LookedUp(None, _failure_reason(response), absent=True)
    if not response.ok:
        logger.request_failed(response.url, response)
        return LookedUp(None, _failure_reason(response))
    return LookedUp(response)


def fetch(
    url: str,
    logger: Logger,
    *,
    method: str = "get",
    require_ok: bool = True,
    on_response: Callable[[Response], None] | None = None,
    **request: Unpack[_Request],
) -> _Fetched:
    """Make an HTTP request and return the response, or None (logged) on a network error or non-OK status.

    `method` selects the session method (`get`, `head`, `post`); `request` (headers, params, auth, json) is
    forwarded to it verbatim. Pass `require_ok=False` to receive the response regardless of status.
    """
    if not hasattr(_SESSIONS, "session"):
        _SESSIONS.session = requests.Session()
    try:
        response = getattr(_SESSIONS.session, method)(url, timeout=10, **request)
    except requests.exceptions.RequestException as error:
        logger.request_failed(url, error)
        return None
    if on_response is not None:
        on_response(response)
    if require_ok and not response.ok:
        logger.request_failed(response.url, response)
        return None
    return response


def _failure_reason(response: _Fetched) -> str:
    """Return why a request failed: at the transport level, or with the status code and message of its response."""
    if response is None:
        return "the request failed"
    body = None
    with suppress(ValueError):  # A body that isn't JSON has no message to include
        body = response.json()
    message = body.get("message", "") if isinstance(body, dict) else ""
    return f"HTTP {response.status_code}{f', {message}' if message else ''}"


def json_body[Body](found: LookedUp[Response], logger: Logger, *, body: type[Body]) -> LookedUp[Body]:
    """Return the JSON body of the response the lookup found as the body type, or why there is none.

    The body type tells the type checker what the caller reads; the response is not checked against it. A body that
    isn't JSON is logged.
    """
    del body
    if (response := found.value) is None:
        return found.failure()
    try:
        return LookedUp(response.json())
    except ValueError:  # The body isn't JSON
        logger.invalid_response(response.url, FORMAT)
        return LookedUp(None, "the response is not valid JSON")


def next_page_url(response: Response) -> str | None:
    """Return the next-page URL from a paginated response's `Link` header (RFC 5988), or None if there is none.

    `requests` parses the `Link` header into `response.links`; the next link may be relative (registries commonly
    return a bare path), so it is resolved against the response's own URL.
    """
    if next_link := response.links.get("next"):
        return urljoin(response.url, next_link["url"])
    return None

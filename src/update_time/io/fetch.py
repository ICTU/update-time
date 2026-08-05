"""Shared HTTP helper: the one place the tool reaches the network.

Every source talks to a flaky external registry, so a single slow or unreachable host must not abort the whole run
with a traceback. `fetch` turns transport-level failures (timeouts, connection errors) into a logged `None`, so the
caller can leave the reference unchanged and the run continues. By default a non-OK status is treated as a failure
too, logged and reported as `None`. Callers that inspect the status themselves — such as the OCI auth probe, which
expects a `401` — pass `require_ok=False` to get the response back whatever its status. It lives in `io` (next to the
file and process I/O) so that network access is centralized: sources and updaters go through it rather than calling
`requests` directly, which the architecture tests enforce.
"""

from typing import TYPE_CHECKING
from urllib.parse import urljoin

import requests

if TYPE_CHECKING:
    from update_time.io.log import Logger

_TIMEOUT = 10  # Seconds to wait for a registry before giving up on the request.


def fetch(
    url: str, logger: Logger, *, method: str = "get", require_ok: bool = True, **kwargs: object
) -> requests.Response | None:
    """Make an HTTP request and return the response, or None (logged) on a network error or non-OK status.

    `method` selects the requests function (`get`, `head`, `post`); `kwargs` (headers, params, auth, json, ...) are
    forwarded to it verbatim. The requests function is looked up on the module at call time so test patches on
    `requests.get`/`.head`/`.post` still apply. Pass `require_ok=False` to receive the response regardless of status.
    """
    try:
        response = getattr(requests, method)(url, timeout=_TIMEOUT, **kwargs)
    except requests.exceptions.Timeout:
        logger.timeout(url)
        return None
    except requests.exceptions.RequestException as error:
        logger.request_error(url, error)
        return None
    if require_ok and not response.ok:
        logger.response(response)
        return None
    return response


def next_page_url(response: requests.Response) -> str | None:
    """Return the next-page URL from a paginated response's `Link` header (RFC 5988), or None if there is none.

    `requests` parses the `Link` header into `response.links`; the next link may be relative (registries commonly
    return a bare path), so it is resolved against the response's own URL.
    """
    if next_link := response.links.get("next"):
        return urljoin(response.url, next_link["url"])
    return None

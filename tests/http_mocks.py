"""Mocks of the HTTP requests Update-time makes through `requests.Session`, and of the responses they get."""

import contextlib
from typing import TYPE_CHECKING, Protocol
from unittest.mock import DEFAULT, Mock, patch

import requests

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping
    from unittest.mock import _patch  # simplint: ignore[private-import-path] # no public patcher type

    class RequestStub(Protocol):
        """A stand-in for `Session.get`, `head`, or `post`, answering a request as a source does."""

        def __call__(self, url: str, **request: object) -> Mock:
            """Return the response to the request for the URL."""
            ...


def unparsable_response(**kwargs: object) -> Mock:
    """Return a mock requests Response whose body is not JSON, with the attributes the keyword arguments set."""
    response = mock_response()
    response.configure_mock(**kwargs)
    response.json.side_effect = requests.JSONDecodeError("Expecting value", "Too Many Requests", 0)
    return response


def mock_response(json: Mapping | list | str | None = None, **kwargs: object) -> Mock:
    """Return a mock requests Response whose .json() returns the given value."""
    # A real response always carries a mapping of headers; default to none, override per test.
    # requests parses the Link header into `.links`; default to none, override per test.
    response = Mock(json=Mock(return_value=json), header={}, links={})
    response.configure_mock(**kwargs)
    return response


def patch_get_response(json: Mapping | list | None = None, **kwargs: object) -> _patch:
    """Patch Session.get to return a mock requests Response whose .json() returns the given value."""
    return patch_get(Mock(return_value=mock_response(json, **kwargs)))


def patch_get(
    new: object = DEFAULT,  # simplint: ignore[imprecise-annotation] # `patch` takes any value
    *,
    return_value: object = DEFAULT,  # simplint: ignore[imprecise-annotation] # `patch` takes any value
) -> _patch:
    """Patch Session.get with `new`, or, as `patch` does, with a mock the decorated test receives."""
    # Where the tests intercept the HTTP requests Update-time makes.
    session_get = "requests.Session.get"
    if new is DEFAULT:
        return patch(session_get, return_value=return_value)
    return patch(session_get, new)


def patch_post_response(json: Mapping | list | None = None, **kwargs: object) -> _patch:
    """Patch Session.post to return a mock requests Response whose .json() returns the given value."""
    return patch_post(Mock(return_value=mock_response(json, **kwargs)))


def patch_post(new: object = DEFAULT) -> _patch:  # simplint: ignore[imprecise-annotation] # `patch` takes any value
    """Patch Session.post with `new`, or, as `patch` does, with a mock the decorated test receives."""
    return patch("requests.Session.post", new)


def requested_urls(mock_get: Mock) -> list[str]:
    """Return the URLs the mock Session.get was asked for, in the order they were asked for."""
    return [call.args[0] for call in mock_get.call_args_list]


def respond_per_url[URL: str | None](mock_get: Mock, responses: Mapping[URL, Mock]) -> None:
    """Point the mock Session.get at the response each URL maps to.

    A URL the mapping doesn't cover raises a KeyError, so a request the test did not expect fails loudly rather
    than being answered.
    """
    mock_get.side_effect = lambda url, **_kwargs: responses[url]


def json_response(json: Mapping | list | None, url: str) -> Mock:
    """Return the response serving the JSON at the URL, or the 404 answering a request for JSON given as None."""
    return mock_response(json, ok=json is not None, status_code=200 if json is not None else 404, url=url)


def patching_get[**P](api: Callable[P, Mock]) -> Callable[P, contextlib._GeneratorContextManager[Mock]]:
    """Return a patch of Session.get with the mock the API builds.

    A test decorated with the patch gets a fresh mock on every run, so each run sees only its own requests.
    """

    @contextlib.contextmanager
    def patched(*args: P.args, **kwargs: P.kwargs) -> Generator[Mock]:
        with patch_get(api(*args, **kwargs)) as mock_get:
            yield mock_get

    return patched

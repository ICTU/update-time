"""Unit tests for the Docker Hub specifics."""

from http import HTTPStatus
from typing import TYPE_CHECKING, Final
from unittest.mock import Mock

import requests

from mutation_time.mutation import kills
from update_time.domain.dependency import Archival, ArchivedSubject
from update_time.primitives.lookup import LookedUp
from update_time.sources import docker_hub
from update_time.sources.docker_hub import _api_headers, _listing_page, _TagListing, archival, last_pushed, tag_listing

from tests.environment import patch_environ
from tests.http_mocks import (
    mock_response,
    patch_get,
    patch_get_response,
    patch_post,
    patch_post_response,
    requested_urls,
    unparsable_response,
)
from tests.update_time.base_test_cases import LoggingTestCase

if TYPE_CHECKING:
    from update_time.formats.json import JSON

_CREDENTIALS: Final[dict[str, str]] = {"DOCKER_HUB_USERNAME": "joe_doe", "DOCKER_HUB_TOKEN": "pat123"}  # nosec
# The endpoint Docker Hub exchanges the credentials for a token at.
_AUTH_URL: Final[str] = "https://hub.docker.com/v2/auth/token"


class ApiHeadersTest(LoggingTestCase):
    """Unit tests for the Docker Hub API authorization headers."""

    def test_no_headers_without_credentials(self):
        """Test that no authorization header is built when no credentials are configured."""
        with patch_environ():
            self.assertEqual(_api_headers(), {})

    @patch_post()
    def test_no_headers_with_incomplete_credentials(self, mock_post: Mock):
        """Test that no header is built, and no token requested, when only one of the two credentials is set."""
        with patch_environ({"DOCKER_HUB_USERNAME": "joe_doe"}, clear=True):  # nosec
            self.assertEqual(_api_headers(), {})
        mock_post.assert_not_called()  # Both credentials are required, so the token endpoint is never called.

    @patch_environ(_CREDENTIALS)
    @patch_post()
    def test_bearer_token_header_when_credentials_are_configured(self, mock_post: Mock):
        """Test that a bearer token is fetched with the credentials and returned as the Authorization header."""
        mock_post.return_value = mock_response({"access_token": "token"})  # nosec
        self.assertEqual(_api_headers(), {"Authorization": "Bearer token"})
        mock_post.assert_called_once_with(
            _AUTH_URL,
            timeout=10,
            json={"identifier": "joe_doe", "secret": "pat123"},  # nosec
        )

    @patch_environ(_CREDENTIALS)
    @patch_post_response(ok=False)
    def test_no_headers_when_token_request_fails(self):
        """Test that a failed token request degrades to anonymous access rather than crashing."""
        self.assertEqual(_api_headers(), {})

    @patch_environ(_CREDENTIALS)
    @patch_post(Mock(return_value=unparsable_response(ok=True, url=_AUTH_URL)))
    def test_no_headers_when_token_response_is_not_json(self):
        """Test that a token response that is not JSON degrades to anonymous access, and is warned about."""
        self.assertEqual(_api_headers(), {})
        self.assert_invalid_json_logged(_AUTH_URL)

    @patch_environ(_CREDENTIALS)
    @kills.replace(
        docker_hub._api_headers,
        'answer.get("access_token")',
        'answer["access_token"]',
        "a Docker Hub token response carrying no token ends the run with a traceback",
        raises="KeyError: 'access_token'",
    )
    @patch_post_response({})
    def test_no_headers_when_token_response_carries_no_token(self):
        """Test that a token response carrying no token degrades to anonymous access rather than crashing."""
        self.assertEqual(_api_headers(), {})


class DockerHubHeadersTest(LoggingTestCase):
    """Unit tests for the headers the Docker Hub API requests carry."""

    @patch_environ(_CREDENTIALS)
    @kills.replace(
        last_pushed,
        "headers=_api_headers())",
        "params=_api_headers())",
        "a push-date request spends Docker Hub's anonymous rate limit although credentials are set",
        case="push date",
    )
    @kills.delete(
        _listing_page,
        "headers=_api_headers(), ",
        "a listing request spends Docker Hub's anonymous rate limit although credentials are set",
        case="listing",
    )
    @kills.delete(
        archival,
        "headers=_api_headers(),",
        "an archival request spends Docker Hub's anonymous rate limit although credentials are set",
        case="archival",
    )
    @patch_post_response({"access_token": "token"})  # nosec
    @patch_get()
    def test_a_request_carries_the_bearer_token_the_credentials_are_exchanged_for(self, mock_get: Mock):
        """Test that each Docker Hub API request carries the bearer token the credentials are exchanged for."""
        requests_made = {
            "push date": lambda: last_pushed("library/python", "3.14"),
            "listing": lambda: tag_listing("library/python", "latest"),
            "archival": lambda: archival("library/python"),
        }
        for request, make_request in requests_made.items():
            with self.subTest(request=request):
                mock_get.return_value = mock_response({"results": [], "tag_last_pushed": "2026-01-01T00:00:00Z"})
                make_request()
                self.assertEqual(mock_get.call_args.kwargs.get("headers"), {"Authorization": "Bearer token"})


class LastPushedTest(LoggingTestCase):
    """Unit tests for reading a Docker Hub tag's push date."""

    @patch_get(Mock(side_effect=requests.exceptions.Timeout))
    def test_a_push_date_request_that_fails_gives_its_failure_as_the_reason(self):
        """Test that a push-date request failing at the transport level returns that failure as the reason."""
        self.assertEqual(last_pushed("library/python", "3.14"), LookedUp(None, "the request failed"))

    @patch_get_response({}, ok=False, status_code=HTTPStatus.TOO_MANY_REQUESTS)
    def test_a_push_date_request_docker_hub_refuses_is_logged_and_gives_its_status_as_the_reason(self):
        """Test that a push-date request Docker Hub refuses is logged, and returns its status as the reason."""
        self.assertEqual(last_pushed("library/python", "3.14"), LookedUp(None, "HTTP 429"))
        self.assert_could_not_fetch_logged(status=HTTPStatus.TOO_MANY_REQUESTS)

    @patch_get_response({"name": "3.14"})
    def test_an_answer_without_a_push_date_gives_a_reason(self):
        """Test that an answer that lacks a push date returns a reason, so the cooldown holds the tag back."""
        self.assertEqual(last_pushed("library/python", "3.14"), LookedUp(None, "Docker Hub did not report a push date"))


# What Docker Hub's repository endpoint answers about an archived repository, and the archival read from it.
_ARCHIVED_REPOSITORY_JSON: Final[dict[str, JSON]] = {"status": 6, "status_description": "archived"}
_ARCHIVED: Final[Archival] = Archival(archived=True, subject=ArchivedSubject.REPOSITORY)


class ArchivalTest(LoggingTestCase):
    """Unit tests for the archival state Docker Hub declares for a repository."""

    @kills.delete(
        archival,
        "HTTPStatus.NOT_FOUND,",
        "a repository Docker Hub does not serve is warned about as a failed request",
        case="unserved",
    )
    @kills.replace(
        archival,
        "statuses_meaning_absent=(HTTPStatus.NOT_FOUND,)",
        "statuses_meaning_absent=tuple(HTTPStatus)",
        "a request Docker Hub fails reads as a repository it does not serve, without a warning",
        case="unreachable",
    )
    @patch_get()
    def test_a_repository_docker_hub_does_not_answer_for_leaves_the_archival_unknown(self, mock_get: Mock):
        """Test that a repository Docker Hub does not serve, or fails to answer for, leaves the archival unknown.

        The repository Docker Hub does not serve is not warned about, and the request Docker Hub fails is.
        """
        cases = {  # The response, and whether it warns
            "unserved": (mock_response({}, ok=False, status_code=HTTPStatus.NOT_FOUND), False),
            "unreachable": (mock_response(None, ok=False, status_code=HTTPStatus.SERVICE_UNAVAILABLE), True),
        }
        for repository, (response, warns) in cases.items():
            with self.subTest(repository=repository):
                mock_get.return_value = response
                self.assertIsNone(archival(f"nginxinc/{repository}"))
                if warns:
                    self.assert_could_not_fetch_logged()
                else:
                    self.assert_no_warnings_logged()
        repository_url = "https://registry.hub.docker.com/v2/namespaces/nginxinc/repositories/{}"
        self.assertEqual(requested_urls(mock_get), [repository_url.format(name) for name in cases])

    @kills.uncache(archival, "every reference to a repository asks Docker Hub about its archival again")
    @patch_get()
    def test_a_repository_asked_about_twice_costs_one_request(self, mock_get: Mock):
        """Test that the second question about a repository's archival is answered without asking Docker Hub again."""
        mock_get.return_value = mock_response(_ARCHIVED_REPOSITORY_JSON)
        self.assertEqual([archival("nginxinc/nginx-unprivileged") for _ in range(2)], [_ARCHIVED, _ARCHIVED])
        self.assertEqual(len(requested_urls(mock_get)), 1)


class TagListingTest(LoggingTestCase):
    """Unit tests for reading a Docker Hub repository's tag listing."""

    @kills.replace(
        _listing_page,
        "if found.absent:",
        "if found.value is None:",
        "a listing request that fails in transport reads as a repository Docker Hub does not serve",
    )
    @patch_get(Mock(side_effect=requests.exceptions.Timeout))
    def test_a_listing_request_that_fails_takes_the_repository_as_served(self):
        """Test that a listing request failing in transport leaves the listing empty and the repository served."""
        self.assertEqual(
            tag_listing("library/python", "latest"), LookedUp(_TagListing(digests={}, pushes={}, examined_all=True))
        )

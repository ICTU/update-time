"""Unit tests for the Docker Hub specifics."""

from http import HTTPStatus
from unittest.mock import Mock, patch

import requests

from update_time.primitives.lookup import LookedUp
from update_time.sources import docker_hub
from update_time.sources.docker_hub import api_headers

from tests.helpers import mock_response, patch_environ
from tests.mutation import Mutation, kills
from tests.update_time.helpers import LoggingTestCase


class ApiHeadersTest(LoggingTestCase):
    """Unit tests for the Docker Hub API authorization headers."""

    def test_no_headers_without_credentials(self):
        """Test that no authorization header is built when no credentials are configured."""
        with patch_environ():
            self.assertEqual(api_headers(), {})

    @patch("requests.post")
    def test_no_headers_with_incomplete_credentials(self, mock_post: Mock):
        """Test that no header is built, and no token requested, when only one of the two credentials is set."""
        with patch_environ({"DOCKER_HUB_USERNAME": "joe_doe"}, clear=True):  # nosec
            self.assertEqual(api_headers(), {})
        mock_post.assert_not_called()  # Both credentials are required, so the token endpoint is never called.

    @patch_environ({"DOCKER_HUB_USERNAME": "joe_doe", "DOCKER_HUB_TOKEN": "pat123"})  # nosec
    @patch("requests.post")
    def test_bearer_token_header_when_credentials_are_configured(self, mock_post: Mock):
        """Test that a bearer token is fetched with the credentials and returned as the Authorization header."""
        mock_post.return_value = mock_response({"access_token": "token"})  # nosec
        self.assertEqual(api_headers(), {"Authorization": "Bearer token"})
        mock_post.assert_called_once_with(
            "https://hub.docker.com/v2/auth/token",
            timeout=10,
            json={"identifier": "joe_doe", "secret": "pat123"},  # nosec
        )

    @patch_environ({"DOCKER_HUB_USERNAME": "joe_doe", "DOCKER_HUB_TOKEN": "pat123"})  # nosec
    @patch("requests.post", Mock(return_value=mock_response(ok=False)))
    def test_no_headers_when_token_request_fails(self):
        """Test that a failed token request degrades to anonymous access rather than crashing."""
        self.assertEqual(api_headers(), {})

    @patch_environ({"DOCKER_HUB_USERNAME": "joe_doe", "DOCKER_HUB_TOKEN": "pat123"})  # nosec
    @kills(
        Mutation(
            docker_hub.api_headers,
            'response.json().get("access_token")',
            'response.json()["access_token"]',
            "a Docker Hub token response carrying no token ends the run with a traceback",
            raises="KeyError: 'access_token'",
        ),
    )
    @patch("requests.post", Mock(return_value=mock_response({})))
    def test_no_headers_when_token_response_carries_no_token(self):
        """Test that a token response carrying no token degrades to anonymous access rather than crashing."""
        self.assertEqual(api_headers(), {})


class LastPushedTest(LoggingTestCase):
    """Unit tests for reading a Docker Hub tag's push date."""

    @patch("requests.get", Mock(side_effect=requests.exceptions.Timeout))
    def test_a_push_date_request_that_fails_gives_its_failure_as_the_reason(self):
        """Test that a push-date request failing at the transport level returns that failure as the reason."""
        self.assertEqual(docker_hub.last_pushed("library/python", "3.14"), LookedUp(None, "the request failed"))

    @patch("requests.get", Mock(return_value=mock_response({}, ok=False, status_code=HTTPStatus.TOO_MANY_REQUESTS)))
    def test_a_push_date_request_docker_hub_refuses_is_logged_and_gives_its_status_as_the_reason(self):
        """Test that a push-date request Docker Hub refuses is logged, and returns its status as the reason."""
        self.assertEqual(docker_hub.last_pushed("library/python", "3.14"), LookedUp(None, "HTTP 429"))
        self.assert_could_not_fetch_logged(status=HTTPStatus.TOO_MANY_REQUESTS)

    @patch("requests.get", Mock(return_value=mock_response({"name": "3.14"})))
    def test_an_answer_without_a_push_date_gives_a_reason(self):
        """Test that an answer that lacks a push date returns a reason, so the cooldown holds the tag back."""
        no_push_date = LookedUp(None, "Docker Hub did not report a push date")
        self.assertEqual(docker_hub.last_pushed("library/python", "3.14"), no_push_date)

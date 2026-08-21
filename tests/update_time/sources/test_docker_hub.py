"""Unit tests for the Docker Hub specifics."""

from unittest.mock import Mock, patch

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
            docker_hub,
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

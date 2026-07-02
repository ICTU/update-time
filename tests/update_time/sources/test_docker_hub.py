"""Unit tests for the Docker Hub specifics."""

from unittest.mock import Mock, patch

from update_time.sources.docker_hub import api_headers

from tests.update_time.helpers import CacheClearingTestCase, mock_response


class ApiHeadersTest(CacheClearingTestCase):
    """Unit tests for the Docker Hub API authorization headers."""

    def test_no_headers_without_credentials(self):
        """Test that no authorization header is built when no credentials are configured."""
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual({}, api_headers())

    @patch.dict("os.environ", {"DOCKER_HUB_USERNAME": "joe_doe", "DOCKER_HUB_TOKEN": "pat123"})  # nosec
    @patch("logging.Logger.warning", Mock())
    @patch("requests.post", Mock(return_value=mock_response(ok=False)))
    def test_no_headers_when_token_request_fails(self):
        """Test that a failed token request degrades to anonymous access rather than crashing."""
        self.assertEqual({}, api_headers())

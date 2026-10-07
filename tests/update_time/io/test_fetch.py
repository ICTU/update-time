"""Unit tests for the shared HTTP fetch helper."""

import unittest
from unittest.mock import Mock, patch

import requests

from update_time.io.fetch import failure_reason, fetch, next_page_url

from tests.helpers import mock_response
from tests.mutation import Mutation, kills


class FetchTest(unittest.TestCase):
    """Unit tests for the fetch function."""

    def setUp(self) -> None:
        """Use a mock logger so the failure branches can be asserted without real logging."""
        super().setUp()
        self.logger = Mock()

    @patch("requests.get")
    def test_ok_response_is_returned(self, mock_get: Mock):
        """Test that an OK response is returned and the timeout is applied."""
        response = mock_response({"key": "value"}, ok=True)
        mock_get.return_value = response
        self.assertIs(response, fetch("https://example.org", self.logger))
        mock_get.assert_called_once_with("https://example.org", timeout=10)

    @patch("requests.head")
    def test_method_and_kwargs_are_forwarded(self, mock_head: Mock):
        """Test that the method selects the requests function and extra kwargs are forwarded verbatim."""
        response = mock_response({}, ok=True)
        mock_head.return_value = response
        self.assertIs(response, fetch("https://example.org", self.logger, method="head", headers={"A": "b"}))
        mock_head.assert_called_once_with("https://example.org", timeout=10, headers={"A": "b"})

    @patch("requests.get")
    def test_non_ok_response_is_logged_and_dropped(self, mock_get: Mock):
        """Test that a non-OK response is logged and reported as None by default."""
        response = mock_response({}, ok=False)
        mock_get.return_value = response
        self.assertIsNone(fetch("https://example.org", self.logger))
        self.logger.response.assert_called_once_with(response)

    @patch("requests.get")
    def test_non_ok_response_is_returned_when_status_is_not_required(self, mock_get: Mock):
        """Test that a non-OK response is returned unlogged when the caller inspects the status itself."""
        response = mock_response({}, ok=False)
        mock_get.return_value = response
        self.assertIs(response, fetch("https://example.org", self.logger, require_ok=False))
        self.logger.response.assert_not_called()

    @patch("requests.get")
    def test_timeout_is_logged_and_dropped(self, mock_get: Mock):
        """Test that a timeout is logged and reported as None rather than raised."""
        mock_get.side_effect = requests.exceptions.Timeout
        self.assertIsNone(fetch("https://example.org", self.logger))
        self.logger.timeout.assert_called_once_with("https://example.org")

    @patch("requests.get")
    def test_network_error_is_logged_and_dropped(self, mock_get: Mock):
        """Test that a connection error (or any other network error) is logged and reported as None."""
        error = requests.exceptions.ConnectionError("connection refused")
        mock_get.side_effect = error
        self.assertIsNone(fetch("https://example.org", self.logger))
        self.logger.request_error.assert_called_once_with("https://example.org", error)


class FailureReasonTest(unittest.TestCase):
    """Unit tests for the reason a failed request is reported with."""

    @kills(
        Mutation(
            failure_reason,
            'message = body.get("message", "") if isinstance(body, dict) else ""',
            'message = (body or {}).get("message", "")',
            "an error body that is a JSON list or string ends the run",
            raises="AttributeError: 'list' object has no attribute 'get'",
        )
    )
    def test_a_json_body_other_than_an_object_gives_the_status_alone(self):
        """Test that a JSON body other than an object gives the status code alone, since it does not hold a message."""
        for body in (["x"], "Too Many Requests"):
            with self.subTest(body=body):
                self.assertEqual(failure_reason(mock_response(body, ok=False, status_code=429)), "HTTP 429")

    @kills(
        Mutation(
            failure_reason,
            "with suppress(ValueError):",
            "with suppress():",
            "an error body that is not JSON ends the run",
            raises="requests.exceptions.JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
        )
    )
    def test_a_body_that_is_not_json_gives_the_status_alone(self):
        """Test that a body that is not JSON, such as plain text, gives the status code alone."""
        response = mock_response(ok=False, status_code=429)
        response.json.side_effect = requests.JSONDecodeError("Expecting value", "Too Many Requests", 0)
        self.assertEqual(failure_reason(response), "HTTP 429")


class NextPageUrlTest(unittest.TestCase):
    """Unit tests for reading the next-page URL from a paginated response's Link header."""

    @staticmethod
    def _response(url: str, link: str = "") -> requests.Response:
        """Return a real requests Response with the given URL and (optional) Link header."""
        response = requests.Response()
        response.url = url
        if link:
            response.headers["Link"] = link
        return response

    def test_no_link_header(self):
        """Test that a response without a Link header has no next page."""
        self.assertIsNone(next_page_url(self._response("https://reg.example/v2/lib/tags/list?n=1000")))

    def test_relative_next_link_is_resolved_against_the_response_url(self):
        """Test that a relative next link (as registries return) is resolved to an absolute URL."""
        response = self._response(
            "https://reg.example/v2/lib/tags/list?n=1000", '</v2/lib/tags/list?last=x>; rel="next"'
        )
        self.assertEqual(next_page_url(response), "https://reg.example/v2/lib/tags/list?last=x")

    def test_only_the_next_relation_is_followed(self):
        """Test that a Link header without a `next` relation (e.g. only `last`) yields no next page."""
        response = self._response("https://reg.example/v2/lib/tags/list", '</v2/lib/tags/list?last=z>; rel="last"')
        self.assertIsNone(next_page_url(response))

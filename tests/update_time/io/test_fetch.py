"""Unit tests for the shared HTTP helpers."""

import unittest
from http import HTTPStatus
from unittest.mock import Mock, patch

import requests

from update_time.io.fetch import failure_reason, fetch, fetch_json, json_body, look_up, next_page_url
from update_time.io.log import Logger, get_logger
from update_time.primitives.lookup import LookedUp

from tests.helpers import mock_response, patch_get, unparsable_response
from tests.mutation import Mutation, kills
from tests.update_time.helpers import LoggingTestCase, message_of

_LOG = get_logger("fetch")


class FetchTest(LoggingTestCase):
    """Unit tests for the fetch function."""

    @patch("requests.Session.get")
    def test_ok_response_is_returned(self, mock_get: Mock):
        """Test that an OK response is returned and the timeout is applied."""
        response = mock_response({"key": "value"}, ok=True)
        mock_get.return_value = response
        self.assertIs(response, fetch("https://example.org", _LOG))
        mock_get.assert_called_once_with("https://example.org", timeout=10)

    @patch("requests.Session.head")
    def test_method_and_kwargs_are_forwarded(self, mock_head: Mock):
        """Test that the method selects the session method and extra kwargs are forwarded verbatim."""
        response = mock_response({}, ok=True)
        mock_head.return_value = response
        self.assertIs(response, fetch("https://example.org", _LOG, method="head", headers={"A": "b"}))
        mock_head.assert_called_once_with("https://example.org", timeout=10, headers={"A": "b"})

    @patch("requests.Session.get")
    def test_non_ok_response_is_logged_and_dropped(self, mock_get: Mock):
        """Test that a non-OK response is logged and reported as None by default."""
        mock_get.return_value = mock_response(
            {}, ok=False, status_code=503, url="https://example.org", reason="Service Unavailable"
        )
        self.assertIsNone(fetch("https://example.org", _LOG))
        self.assert_logged(
            message_of(Logger.response), url="https://example.org", status=503, reason="Service Unavailable"
        )

    @patch("requests.Session.get")
    def test_non_ok_response_is_returned_when_status_is_not_required(self, mock_get: Mock):
        """Test that a non-OK response is returned unlogged when the caller inspects the status itself."""
        response = mock_response({}, ok=False)
        mock_get.return_value = response
        self.assertIs(response, fetch("https://example.org", _LOG, require_ok=False))
        self.mock_log.assert_not_called()

    @patch("requests.Session.get")
    def test_timeout_is_logged_and_dropped(self, mock_get: Mock):
        """Test that a timeout is logged and reported as None rather than raised."""
        mock_get.side_effect = requests.exceptions.Timeout
        self.assertIsNone(fetch("https://example.org", _LOG))
        self.assert_logged(message_of(Logger.timeout), url="https://example.org")

    @patch("requests.Session.get")
    def test_network_error_is_logged_and_dropped(self, mock_get: Mock):
        """Test that a connection error (or any other network error) is logged and reported as None."""
        error = requests.exceptions.ConnectionError("connection refused")
        mock_get.side_effect = error
        self.assertIsNone(fetch("https://example.org", _LOG))
        self.assert_logged(message_of(Logger.request_error), url="https://example.org", error=error)


class FetchJsonTest(LoggingTestCase):
    """Unit tests for the fetch_json function."""

    @kills(
        Mutation(
            json_body,
            "return LookedUp(response.json())",
            "return LookedUp(response)",
            "a source reads the response as if it were the JSON body",
        )
    )
    @patch_get({"key": "value"}, ok=True)
    def test_ok_response_gives_its_json_body(self):
        """Test that an OK response gives its parsed JSON body, and logs nothing."""
        self.assertEqual(fetch_json("https://example.org", _LOG), LookedUp({"key": "value"}))
        self.mock_log.assert_not_called()

    @kills(
        Mutation(
            look_up,
            "on_response=on_response, **request)",
            "on_response=on_response)",
            "a source's requests go out without the authorization it sets",
        )
    )
    @patch("requests.Session.get")
    def test_the_headers_are_sent_with_the_request(self, mock_get: Mock):
        """Test that the headers are sent with the request."""
        mock_get.return_value = mock_response({}, ok=True)
        fetch_json("https://example.org", _LOG, headers={"Authorization": "Bearer token"})
        mock_get.assert_called_once_with("https://example.org", timeout=10, headers={"Authorization": "Bearer token"})

    @kills(
        Mutation(
            look_up,
            "return LookedUp(None, failure_reason(None))",
            "return LookedUp(None)",
            "a source reports a failed request without saying why it failed",
        )
    )
    @patch("requests.Session.get", Mock(side_effect=requests.exceptions.Timeout))
    def test_a_failed_request_gives_its_reason_and_is_logged_once(self):
        """Test that a request failing at the transport level gives the reason it failed, and logs one warning."""
        self.assertEqual(fetch_json("https://example.org", _LOG), LookedUp(None, "the request failed"))
        self.assert_logged(message_of(Logger.timeout), url="https://example.org")

    @kills(
        Mutation(
            look_up,
            "logger.response(response)",
            "pass",
            "a refused request goes unreported unless the source reports it itself",
        ),
        Mutation(
            look_up,
            "return LookedUp(None, failure_reason(response))",
            "return LookedUp(None, failure_reason(None))",
            "a source reports a refused request without its status",
        ),
        Mutation(
            look_up,
            "if response.status_code in statuses_meaning_absent:",
            "if statuses_meaning_absent:",
            "a refusal reads as absent because the caller names another status as absent",
        ),
    )
    @patch_get(ok=False, status_code=503, url="https://example.org", reason="Service Unavailable")
    def test_a_refused_request_gives_its_status_and_is_logged_once(self):
        """Test that a refusal with a status other than one meaning absent gives the status, and logs one warning."""
        self.assertEqual(
            fetch_json("https://example.org", _LOG, statuses_meaning_absent=(HTTPStatus.NOT_FOUND,)),
            LookedUp(None, "HTTP 503"),
        )
        self.assert_logged(
            message_of(Logger.response), url="https://example.org", status=503, reason="Service Unavailable"
        )

    @kills(
        Mutation(
            look_up,
            "return LookedUp(None, failure_reason(response), absent=True)",
            "return LookedUp(None, failure_reason(response))",
            "a source reads what it does not serve as a failed request",
        )
    )
    @patch_get(ok=False, status_code=404)
    def test_a_status_meaning_absent_gives_an_absent_lookup_and_logs_nothing(self):
        """Test that a refusal with a status the caller says means absent gives an absent lookup, and logs nothing."""
        self.assertEqual(
            fetch_json("https://example.org", _LOG, statuses_meaning_absent=(HTTPStatus.NOT_FOUND,)),
            LookedUp(None, "HTTP 404", absent=True),
        )
        self.mock_log.assert_not_called()

    @kills(
        Mutation(
            json_body,
            "logger.invalid_json(response.url)",
            "pass",
            "a body that is not JSON goes unreported unless the source reports it itself",
        ),
        Mutation(
            json_body,
            'return LookedUp(None, "the response is not valid JSON")',
            "return LookedUp(None)",
            "a source reports a body that is not JSON without saying why it failed",
        ),
    )
    @patch("requests.Session.get", Mock(return_value=unparsable_response(ok=True, url="https://example.org")))
    def test_a_body_that_is_not_json_gives_its_reason_and_is_logged_once(self):
        """Test that an OK response whose body is not JSON gives the reason rather than raising, and logs a warning."""
        self.assertEqual(fetch_json("https://example.org", _LOG), LookedUp(None, "the response is not valid JSON"))
        self.assert_invalid_json_logged("https://example.org")


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
        self.assertEqual(failure_reason(unparsable_response(ok=False, status_code=429)), "HTTP 429")


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

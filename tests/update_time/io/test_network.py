"""Unit tests for the network settings a run inherits."""

import unittest
from unittest.mock import Mock

from update_time.io import network as network_module
from update_time.io.network import report_network_settings

from tests.helpers import patch_environ
from tests.mutation import Mutation, kills


class ReportNetworkSettingsTest(unittest.TestCase):
    """Unit tests for logging the network settings."""

    @kills(
        Mutation(
            network_module._redacted_url,
            '":".join(_REDACTED for part',
            '":".join(part for part',
            "a proxy's user and password are written to the log",
        )
    )
    def test_a_proxy_is_logged_with_its_user_and_password_redacted(self):
        """Test that a proxy is logged as its URL with a user and a password each redacted, or as unreadable."""
        for case, url, logged in (
            (
                "user and password",
                "http://joe:s3cret@proxy.acme.com:3128",
                "http://<redacted>:<redacted>@proxy.acme.com:3128",
            ),
            ("user only", "http://joe@proxy.acme.com:3128", "http://<redacted>@proxy.acme.com:3128"),
            ("neither", "http://proxy.acme.com:3128", "http://proxy.acme.com:3128"),
            ("another scheme", "socks5://proxy.acme.com:1080", "socks5://proxy.acme.com:1080"),
            ("no scheme", "proxy.acme.com:3128", "http://proxy.acme.com:3128"),
            (
                "no scheme, user and password",
                "joe:s3cret@proxy.acme.com:3128",
                "http://<redacted>:<redacted>@proxy.acme.com:3128",
            ),
            ("port that is not a number", "http://proxy.acme.com:abc", "http://proxy.acme.com:abc"),
            ("URL that does not split", "http://joe:s3cret@[::1", "<unreadable>"),
        ):
            with self.subTest(case), patch_environ({"HTTPS_PROXY": url}, clear=True):
                logger = Mock()
                report_network_settings(logger)
                logger.network_settings.assert_called_once_with({"https": logged}, "", "")

    @kills(
        Mutation(
            network_module.report_network_settings,
            'or os.environ.get("CURL_CA_BUNDLE") or',
            "or",
            "a CA bundle CURL_CA_BUNDLE names is logged as the default one, though requests uses it",
        ),
        Mutation(
            network_module.report_network_settings,
            'os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")',
            'os.environ.get("CURL_CA_BUNDLE") or os.environ.get("REQUESTS_CA_BUNDLE")',
            "the CA bundle CURL_CA_BUNDLE names is logged, though requests uses the one REQUESTS_CA_BUNDLE names",
        ),
    )
    def test_the_ca_bundle_is_read_from_requests_ca_bundle_first_and_curl_ca_bundle_second(self):
        """Test that REQUESTS_CA_BUNDLE wins over CURL_CA_BUNDLE, and that CURL_CA_BUNDLE is read without it."""
        for case, environment, ca_bundle in (
            (
                "both",
                {"REQUESTS_CA_BUNDLE": "/etc/ssl/acme.pem", "CURL_CA_BUNDLE": "/etc/ssl/curl.pem"},
                "/etc/ssl/acme.pem",
            ),
            ("curl only", {"CURL_CA_BUNDLE": "/etc/ssl/curl.pem"}, "/etc/ssl/curl.pem"),
        ):
            with self.subTest(case), patch_environ(environment, clear=True):
                logger = Mock()
                report_network_settings(logger)
                self.assertEqual(logger.network_settings.call_args.args[2], ca_bundle)

    def test_nothing_is_logged_below_debug(self):
        """Test that the network settings are not logged when the log does not show debug messages."""
        logger = Mock()
        logger.logs_debug.return_value = False
        with patch_environ({"HTTPS_PROXY": "http://proxy.acme.com:3128"}, clear=True):
            report_network_settings(logger)
        logger.network_settings.assert_not_called()

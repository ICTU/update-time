"""Unit tests for the guard that refuses the network."""

import socket
import unittest
from unittest.mock import Mock

from update_time.io.fetch import fetch


class NetworkGuardTest(unittest.TestCase):
    """Unit tests for the refusal of network access."""

    def test_connecting_a_socket_is_refused(self):
        """Test that connecting a socket raises an error naming the address it tried to reach."""
        with socket.socket() as sock, self.assertRaises(RuntimeError) as raised:
            sock.connect(("127.0.0.1", 9))
        self.assertEqual(str(raised.exception), "test tried to reach the network at ('127.0.0.1', 9)")

    def test_resolving_a_host_name_is_refused(self):
        """Test that resolving a host name raises an error naming the host it tried to reach."""
        with self.assertRaises(RuntimeError) as raised:
            socket.getaddrinfo("update-time.invalid", 443)
        self.assertEqual(str(raised.exception), "test tried to reach the network at update-time.invalid")

    def test_an_unmocked_request_fails_the_test(self):
        """Test that a request raises the refusal instead of fetch logging it as a network error and returning None."""
        logger = Mock()
        with self.assertRaises(RuntimeError) as raised:
            fetch("https://update-time.invalid/", logger)
        self.assertEqual(str(raised.exception), "test tried to reach the network at update-time.invalid")
        self.assertEqual(logger.mock_calls, [])

"""Unit tests for the command-line interface."""

import contextlib
import io
import unittest
from importlib.metadata import version
from unittest.mock import patch

from update_time.domain.cooldown import COOLDOWN_DAYS
from update_time.io.cli import parse_args


class CommandLineInterfaceTest(unittest.TestCase):
    """Unit tests for the command-line interface."""

    def parse_args(self, option: str) -> str:
        """Parse the given option, assert that it exits cleanly, and return the captured standard output."""
        stdout = io.StringIO()
        with (
            patch("sys.argv", ["update-time", option]),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as cm,
        ):
            parse_args()
        self.assertEqual(0, cm.exception.code)
        return stdout.getvalue()

    def test_version(self):
        """Test that the --version option shows the version and exits."""
        self.assertEqual(f"v{version('update-time')}", self.parse_args("--version").strip())

    def test_help(self):
        """Test that the --help option shows the help text and exits."""
        self.assertIn("usage: update-time", self.parse_args("--help"))

    def test_default_cooldown(self):
        """Test that the cooldown defaults to the default cooldown period."""
        with patch("sys.argv", ["update-time"]):
            self.assertEqual(COOLDOWN_DAYS, parse_args().cooldown)

    def test_cooldown(self):
        """Test that the --cooldown option sets the cooldown period."""
        with patch("sys.argv", ["update-time", "--cooldown", "14"]):
            self.assertEqual(14, parse_args().cooldown)

    def test_negative_cooldown(self):
        """Test that a negative --cooldown is rejected."""
        stderr = io.StringIO()
        with (
            patch("sys.argv", ["update-time", "--cooldown", "-1"]),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as cm,
        ):
            parse_args()
        self.assertEqual(2, cm.exception.code)
        self.assertIn("-1 is not a non-negative integer", stderr.getvalue())

"""Unit tests for the command-line interface."""

import contextlib
import io
import unittest
from importlib.metadata import version
from pathlib import Path
from unittest.mock import Mock, patch

from update_time.domain.cooldown import COOLDOWN_DAYS
from update_time.io.cli import parse_args
from update_time.io.log import DEFAULT_LOG_LEVEL


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

    def test_non_integer_cooldown(self):
        """Test that a non-integer --cooldown is rejected with a user-facing message."""
        stderr = io.StringIO()
        with (
            patch("sys.argv", ["update-time", "--cooldown", "abc"]),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as cm,
        ):
            parse_args()
        self.assertEqual(2, cm.exception.code)
        self.assertIn("invalid days value: 'abc'", stderr.getvalue())

    def test_default_path(self):
        """Test that the path defaults to the current directory."""
        with patch("sys.argv", ["update-time"]):
            self.assertEqual(Path(), parse_args().path)

    @patch("pathlib.Path.is_dir", Mock(return_value=True))
    def test_path(self):
        """Test that a positional path argument is parsed as a directory."""
        with patch("sys.argv", ["update-time", "some-directory"]):
            self.assertEqual(Path("some-directory"), parse_args().path)

    @patch("pathlib.Path.is_dir", Mock(return_value=False))
    def test_path_that_is_not_a_directory(self):
        """Test that a path that is not an existing directory is rejected with a user-facing message."""
        stderr = io.StringIO()
        with (
            patch("sys.argv", ["update-time", "no-such-directory"]),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as cm,
        ):
            parse_args()
        self.assertEqual(2, cm.exception.code)
        self.assertIn("no-such-directory is not an existing directory", stderr.getvalue())

    def test_default_log_level(self):
        """Test that the log level defaults to the default log level."""
        with patch("sys.argv", ["update-time"]):
            self.assertEqual(DEFAULT_LOG_LEVEL, parse_args().log_level)

    def test_log_level(self):
        """Test that the --log-level option sets the log level, upper-casing the value."""
        with patch("sys.argv", ["update-time", "--log-level", "debug"]):
            self.assertEqual("DEBUG", parse_args().log_level)

    def test_invalid_log_level(self):
        """Test that an invalid --log-level is rejected."""
        stderr = io.StringIO()
        with (
            patch("sys.argv", ["update-time", "--log-level", "verbose"]),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as cm,
        ):
            parse_args()
        self.assertEqual(2, cm.exception.code)
        self.assertIn("invalid choice", stderr.getvalue())

    def test_default_exclude_path(self):
        """Test that no paths are excluded by default."""
        with patch("sys.argv", ["update-time"]):
            self.assertEqual([], parse_args().exclude_path)

    def test_exclude_path(self):
        """Test that a comma-separated --exclude-path is parsed into a list of relative paths."""
        with patch("sys.argv", ["update-time", "--exclude-path", "vendor,packages/legacy"]):
            self.assertEqual([Path("vendor"), Path("packages/legacy")], parse_args().exclude_path)

    def test_exclude_path_normalises_entries(self):
        """Test that --exclude-path entries are stripped of surrounding whitespace and trailing separators."""
        with patch("sys.argv", ["update-time", "--exclude-path", " vendor/ , packages/legacy/ "]):
            self.assertEqual([Path("vendor"), Path("packages/legacy")], parse_args().exclude_path)

    def test_exclude_path_ignores_empty_entries(self):
        """Test that empty entries in --exclude-path (e.g. from a trailing comma) are dropped."""
        with patch("sys.argv", ["update-time", "--exclude-path", "vendor,"]):
            self.assertEqual([Path("vendor")], parse_args().exclude_path)

    def assert_rejected_exclude_path(self, value: str, expected_message: str) -> None:
        """Assert that the given --exclude-path value is rejected with exit status 2 and the expected message."""
        stderr = io.StringIO()
        with (
            patch("sys.argv", ["update-time", "--exclude-path", value]),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as cm,
        ):
            parse_args()
        self.assertEqual(2, cm.exception.code)
        self.assertIn(expected_message, stderr.getvalue())

    def test_absolute_exclude_path_is_rejected(self):
        """Test that an absolute --exclude-path is rejected: the option narrows the tree, it can't redirect it."""
        self.assert_rejected_exclude_path("/etc", "/etc is not a relative path")

    def test_escaping_exclude_path_is_rejected(self):
        """Test that a --exclude-path escaping the scan root (../…) is rejected."""
        self.assert_rejected_exclude_path("../sibling", "../sibling is outside the scan root")

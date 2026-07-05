"""Unit tests for the command-line interface."""

import contextlib
import io
import unittest
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from update_time.domain.cooldown import COOLDOWN_DAYS
from update_time.io.cli import parse_args
from update_time.io.log import DEFAULT_LOG_LEVEL

if TYPE_CHECKING:
    import argparse


class CommandLineInterfaceTest(unittest.TestCase):
    """Unit tests for the command-line interface."""

    def parsed(self, *argv: str) -> argparse.Namespace:
        """Parse the given command-line arguments and return the resulting namespace."""
        with patch("sys.argv", ["update-time", *argv]):
            return parse_args()

    def stdout_of(self, *argv: str) -> str:
        """Parse the given arguments, assert they exit cleanly (status 0), and return the captured standard output."""
        stdout = io.StringIO()
        with (
            patch("sys.argv", ["update-time", *argv]),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as cm,
        ):
            parse_args()
        self.assertEqual(0, cm.exception.code)
        return stdout.getvalue()

    def assert_rejected(self, argv: list[str], expected_message: str) -> None:
        """Assert that parsing the given arguments exits with status 2 and the expected message on standard error."""
        stderr = io.StringIO()
        with (
            patch("sys.argv", ["update-time", *argv]),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as cm,
        ):
            parse_args()
        self.assertEqual(2, cm.exception.code)
        self.assertIn(expected_message, stderr.getvalue())

    def test_version(self):
        """Test that the --version option shows the version and exits."""
        self.assertEqual(f"v{version('update-time')}", self.stdout_of("--version").strip())

    def test_help(self):
        """Test that the --help option shows the help text and exits."""
        self.assertIn("usage: update-time", self.stdout_of("--help"))

    def test_default_cooldown(self):
        """Test that the cooldown defaults to the default cooldown period."""
        self.assertEqual(COOLDOWN_DAYS, self.parsed().cooldown)

    def test_cooldown(self):
        """Test that the --cooldown option sets the cooldown period."""
        self.assertEqual(14, self.parsed("--cooldown", "14").cooldown)

    def test_negative_cooldown(self):
        """Test that a negative --cooldown is rejected."""
        self.assert_rejected(["--cooldown", "-1"], "-1 is not a non-negative integer")

    def test_non_integer_cooldown(self):
        """Test that a non-integer --cooldown is rejected with a user-facing message."""
        self.assert_rejected(["--cooldown", "abc"], "invalid days value: 'abc'")

    def test_default_path(self):
        """Test that the path defaults to the current directory."""
        self.assertEqual(Path(), self.parsed().path)

    @patch("pathlib.Path.is_dir", Mock(return_value=True))
    def test_path(self):
        """Test that a positional path argument is parsed as a directory."""
        self.assertEqual(Path("some-directory"), self.parsed("some-directory").path)

    @patch("pathlib.Path.is_dir", Mock(return_value=False))
    def test_path_that_is_not_a_directory(self):
        """Test that a path that is not an existing directory is rejected with a user-facing message."""
        self.assert_rejected(["no-such-directory"], "no-such-directory is not an existing directory")

    def test_default_log_level(self):
        """Test that the log level defaults to the default log level."""
        self.assertEqual(DEFAULT_LOG_LEVEL, self.parsed().log_level)

    def test_log_level(self):
        """Test that the --log-level option sets the log level, upper-casing the value."""
        self.assertEqual("DEBUG", self.parsed("--log-level", "debug").log_level)

    def test_invalid_log_level(self):
        """Test that an invalid --log-level is rejected."""
        self.assert_rejected(["--log-level", "verbose"], "invalid choice")

    def test_default_exclude_path(self):
        """Test that no paths are excluded by default."""
        self.assertEqual([], self.parsed().exclude_path)

    def test_exclude_path(self):
        """Test that a comma-separated --exclude-path is parsed into a list of relative paths."""
        excluded = self.parsed("--exclude-path", "vendor,packages/legacy").exclude_path
        self.assertEqual([Path("vendor"), Path("packages/legacy")], excluded)

    def test_exclude_path_normalises_entries(self):
        """Test that --exclude-path entries are stripped of surrounding whitespace and trailing separators."""
        excluded = self.parsed("--exclude-path", " vendor/ , packages/legacy/ ").exclude_path
        self.assertEqual([Path("vendor"), Path("packages/legacy")], excluded)

    def test_exclude_path_collapses_interior_parent_segments(self):
        """Test that a `..` that stays inside the scan root is collapsed (normalised, not just stripped)."""
        excluded = self.parsed("--exclude-path", "packages/old/../legacy").exclude_path
        self.assertEqual([Path("packages/legacy")], excluded)

    def test_exclude_path_ignores_empty_entries(self):
        """Test that empty entries in --exclude-path (e.g. from a trailing comma) are dropped."""
        self.assertEqual([Path("vendor")], self.parsed("--exclude-path", "vendor,").exclude_path)

    def test_absolute_exclude_path_is_rejected(self):
        """Test that an absolute --exclude-path is rejected: the option narrows the tree, it can't redirect it."""
        self.assert_rejected(["--exclude-path", "/etc"], "/etc is not a relative path")

    def test_escaping_exclude_path_is_rejected(self):
        """Test that a --exclude-path escaping the scan root (../…) is rejected."""
        self.assert_rejected(["--exclude-path", "../sibling"], "../sibling is outside the scan root")

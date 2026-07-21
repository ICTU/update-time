"""Unit tests for the command-line interface."""

import contextlib
import io
import unittest
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from update_time.domain.cooldown import COOLDOWN
from update_time.io.cli import parse_args
from update_time.io.log import LOG_LEVEL

from tests.update_time.helpers import patch_pathlib_path

if TYPE_CHECKING:
    import argparse


class CommandLineInterfaceTest(unittest.TestCase):
    """Unit tests for the command-line interface."""

    def setUp(self) -> None:
        """Treat the PATH as inside a git repository by default, so parsing succeeds unless a test says otherwise."""
        self.enterContext(patch("update_time.io.cli.inside_git_repository", Mock(return_value=True)))

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
        self.assertEqual(cm.exception.code, 0)
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
        self.assertEqual(cm.exception.code, 2)
        self.assertIn(expected_message, stderr.getvalue())

    def test_version(self):
        """Test that the --version option shows the version and exits."""
        self.assertEqual(self.stdout_of("--version").strip(), f"v{version('update-time')}")

    def test_help(self):
        """Test that the --help option shows the help text and exits."""
        self.assertIn("usage: update-time", self.stdout_of("--help"))

    def test_default_cooldown(self):
        """Test that the cooldown defaults to the default cooldown period."""
        self.assertEqual(COOLDOWN.default, self.parsed().cooldown)

    def test_cooldown(self):
        """Test that the --cooldown option sets the cooldown period."""
        self.assertEqual(self.parsed("--cooldown", "14").cooldown, 14)

    def test_negative_cooldown(self):
        """Test that a negative --cooldown is rejected."""
        self.assert_rejected(["--cooldown", "-1"], "-1 is not a non-negative integer")

    def test_non_integer_cooldown(self):
        """Test that a non-integer --cooldown is rejected with a user-facing message."""
        self.assert_rejected(["--cooldown", "abc"], "invalid days value: 'abc'")

    def test_default_path(self):
        """Test that the path defaults to the current directory."""
        self.assertEqual(Path(), self.parsed().path)

    @patch_pathlib_path(is_dir=True)
    def test_path(self):
        """Test that a positional path argument is parsed as a directory."""
        self.assertEqual(Path("some-directory"), self.parsed("some-directory").path)

    @patch_pathlib_path(is_dir=False)
    def test_path_that_is_not_a_directory(self):
        """Test that a path that is not an existing directory is rejected with a user-facing message."""
        self.assert_rejected(["no-such-directory"], "no-such-directory is not an existing directory")

    def test_default_force(self):
        """Test that --force is off by default."""
        self.assertFalse(self.parsed().force)

    def test_force(self):
        """Test that --force turns on forcing the run outside a git repository."""
        self.assertTrue(self.parsed("--force").force)

    @patch_pathlib_path(is_dir=True)
    @patch("update_time.io.cli.inside_git_repository", Mock(return_value=False))
    def test_path_not_in_git_repository_is_rejected(self):
        """Test that a PATH that is not inside a git repository is rejected like any other invalid argument."""
        self.assert_rejected(["some-directory"], "some-directory is not inside a git repository")

    @patch_pathlib_path(is_dir=True)
    @patch("update_time.io.cli.inside_git_repository", Mock(return_value=False))
    def test_force_allows_a_path_not_in_a_git_repository(self):
        """Test that --force lets a PATH that is not inside a git repository through instead of rejecting it."""
        self.assertTrue(self.parsed("--force", "some-directory").force)

    def test_default_log_level(self):
        """Test that the log level defaults to the default log level."""
        self.assertEqual(LOG_LEVEL.default, self.parsed().log_level)

    def test_log_level(self):
        """Test that the --log-level option sets the log level, upper-casing the value."""
        self.assertEqual(self.parsed("--log-level", "debug").log_level, "DEBUG")

    def test_invalid_log_level(self):
        """Test that an invalid --log-level is rejected."""
        self.assert_rejected(["--log-level", "verbose"], "invalid choice")

    def test_default_exclude_path(self):
        """Test that no paths are excluded by default."""
        self.assertEqual(self.parsed().exclude_path, [])

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

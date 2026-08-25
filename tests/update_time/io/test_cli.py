"""Unit tests for the command-line interface."""

import contextlib
import io
import unittest
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency_type import DEPENDENCY_TYPES
from update_time.domain.vulnerability import NO_RISK_LEVEL, RISK_LEVELS, VULNERABILITY_LEVEL
from update_time.io import cli as cli_module
from update_time.io.cli import parse_args
from update_time.io.log import LOG_LEVEL

from tests.helpers import patch_environ, patch_pathlib_path
from tests.mutation import Mutation, kills

if TYPE_CHECKING:
    import argparse


class CommandLineInterfaceTest(unittest.TestCase):
    """Unit tests for the command-line interface."""

    def setUp(self) -> None:
        """Treat the PATH as inside a git repository, and keep argparse's help colourless whatever the run sets."""
        self.enterContext(patch("update_time.io.cli.inside_git_repository", Mock(return_value=True)))
        self.enterContext(patch_environ({"NO_COLOR": "1"}, clear=False))  # Make argparse write colourless help

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

    def declared_file_types(self) -> list[str]:
        """Return the file types the dependency types declare, in declaration order, repetitions included.

        An empty declaration would leave every assertion about the file types passing without examining anything, so
        it fails here rather than in each test.
        """
        file_types = [
            file_type.name for dependency_type in DEPENDENCY_TYPES for file_type in dependency_type.file_types
        ]
        self.assertNotEqual(file_types, [])
        return file_types

    def description_of_help(self) -> str:
        """Return the help's description: the paragraph between the usage and the positional arguments.

        Argparse breaks a wrapped file on its hyphens, and rejoining the lines does not put the file back
        together, so it is given a terminal wide enough to wrap nothing.
        """
        with patch_environ({"COLUMNS": "999"}, clear=False):
            help_text = self.stdout_of("--help")
        return help_text.partition("\n\npositional arguments:")[0].rpartition("\n\n")[2]

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

    @kills(
        Mutation(
            cli_module,
            "            file_type.name for dependency_type in DEPENDENCY_TYPES "
            "for file_type in dependency_type.file_types",
            "            file_type.name for dependency_type in list(DEPENDENCY_TYPES)[:-1] "
            "for file_type in dependency_type.file_types",
            "a file type the dependency types declare goes unnamed in the help",
        )
    )
    def test_help_names_the_file_types_of_every_dependency_type(self):
        """Test that the help names every file type the dependency types declare."""
        file_types = self.declared_file_types()
        description = self.description_of_help()
        self.assertEqual([file_type for file_type in file_types if file_type not in description], [])

    @kills(
        Mutation(
            cli_module,
            "        dict.fromkeys(\n",
            "        (\n",
            "the help names a file type twice when two dependency types declare it",
        )
    )
    def test_help_names_a_file_type_two_dependency_types_declare_once(self):
        """Test that the help names a file type more than one dependency type declares once."""
        declared = Counter(self.declared_file_types())
        shared = [file_type for file_type, times in declared.items() if times > 1]
        self.assertNotEqual(shared, [])  # Without a file type two types declare there is no repetition to collapse
        description = self.description_of_help()
        self.assertEqual([file_type for file_type in shared if description.count(file_type) != 1], [])

    @kills(
        Mutation(
            cli_module,
            "    file_types = list(\n",
            "    file_types = sorted(\n",
            "the help names the file types in an order of its own rather than the one they are declared in",
        )
    )
    def test_help_names_the_file_types_in_declaration_order(self):
        """Test that the help names the file types in the order the dependency types declare them."""
        file_types = list(dict.fromkeys(self.declared_file_types()))
        description = self.description_of_help()
        self.assertEqual(sorted(file_types, key=description.index), file_types)

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

    def test_default_vulnerability_level(self):
        """Test that the risk level to warn from defaults to the default vulnerability level."""
        self.assertEqual(VULNERABILITY_LEVEL.default, self.parsed().vulnerability_level)

    def test_vulnerability_level(self):
        """Test that the --vulnerability-level option sets the risk level to warn from, lower-casing the value."""
        self.assertEqual(self.parsed("--vulnerability-level", "HIGH").vulnerability_level, "high")

    def test_invalid_vulnerability_level(self):
        """Test that an invalid --vulnerability-level is rejected."""
        self.assert_rejected(["--vulnerability-level", "hgih"], "invalid choice")

    def test_help_enumerates_the_vulnerability_levels(self):
        """Test that the help names every value --vulnerability-level accepts."""
        levels = ",".join([*RISK_LEVELS, NO_RISK_LEVEL])
        self.assertIn(f"--vulnerability-level {{{levels}}}", self.stdout_of("--help"))

    def test_default_exclude_path(self):
        """Test that no paths are excluded by default."""
        self.assertEqual(self.parsed().exclude_path, [])

    def test_exclude_path(self):
        """Test that a comma-separated --exclude-path is parsed into a list of relative paths."""
        excluded = self.parsed("--exclude-path", "vendor,packages/legacy").exclude_path
        self.assertEqual(excluded, [Path("vendor"), Path("packages/legacy")])

    def test_exclude_path_normalises_entries(self):
        """Test that --exclude-path entries are stripped of surrounding whitespace and trailing separators."""
        excluded = self.parsed("--exclude-path", " vendor/ , packages/legacy/ ").exclude_path
        self.assertEqual(excluded, [Path("vendor"), Path("packages/legacy")])

    def test_exclude_path_collapses_interior_parent_segments(self):
        """Test that a `..` that stays inside the scan root is collapsed (normalised, not just stripped)."""
        excluded = self.parsed("--exclude-path", "packages/old/../legacy").exclude_path
        self.assertEqual(excluded, [Path("packages/legacy")])

    def test_exclude_path_ignores_empty_entries(self):
        """Test that empty entries in --exclude-path (e.g. from a trailing comma) are dropped."""
        self.assertEqual(self.parsed("--exclude-path", "vendor,").exclude_path, [Path("vendor")])

    def test_absolute_exclude_path_is_rejected(self):
        """Test that an absolute --exclude-path is rejected: the option narrows the tree, it can't redirect it."""
        self.assert_rejected(["--exclude-path", "/etc"], "/etc is not a relative path")

    def test_escaping_exclude_path_is_rejected(self):
        """Test that a --exclude-path escaping the scan root (../…) is rejected."""
        self.assert_rejected(["--exclude-path", "../sibling"], "../sibling is outside the scan root")

"""Unit tests for the command-line interface."""

import contextlib
import io
import unittest
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from mutation_time.mutation import kills
from update_time.domain.cooldown import COOLDOWN
from update_time.domain.dependency_type import DEPENDENCY_TYPES
from update_time.domain.vulnerability import RISK_LEVELS, VULNERABILITY_LEVEL
from update_time.io.cli import _scanned_file_types, parse_args
from update_time.io.log import LOG_LEVEL

from tests.environment import patch_environ
from tests.file_mocks import patch_pathlib_path

if TYPE_CHECKING:
    import argparse


class CommandLineInterfaceTest(unittest.TestCase):
    """Unit tests for the command-line interface."""

    def setUp(self) -> None:
        """Treat the PATH as inside a git repository, and keep argparse's help colourless whatever the run sets."""
        self.enterContext(patch("update_time.io.cli.inside_git_repository", Mock(return_value=True)))
        self.enterContext(patch_environ({"NO_COLOR": "1"}, clear=False))  # Make argparse write colourless help

    def test_version(self):
        """Test that the --version option shows the version and exits."""
        self.assertEqual(self.stdout_of("--version").strip(), f"v{version('update-time')}")

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

    def test_help(self):
        """Test that the --help option shows the help text and exits."""
        self.assertIn("usage: update-time", self.stdout_of("--help"))

    @kills.replace(
        _scanned_file_types,
        "file_type.name for dependency_type in DEPENDENCY_TYPES for file_type in dependency_type.file_types",
        "file_type.name for dependency_type in list(DEPENDENCY_TYPES)[:-1] for file_type in dependency_type.file_types",
        "a file type the dependency types declare goes unnamed in the help",
    )
    def test_help_names_the_file_types_of_every_dependency_type(self):
        """Test that the help names every file type the dependency types declare."""
        file_types = self.declared_file_types()
        description = self.description_of_help()
        self.assertEqual([file_type for file_type in file_types if file_type not in description], [])

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

    @kills.replace(
        _scanned_file_types,
        "unique(names)",
        "list(names)",
        "the help names a file type twice when two dependency types declare it",
    )
    def test_help_names_a_file_type_two_dependency_types_declare_once(self):
        """Test that the help names a file type more than one dependency type declares once."""
        declared = Counter(self.declared_file_types())
        shared = [file_type for file_type, times in declared.items() if times > 1]
        self.assertNotEqual(shared, [])  # Without a file type two types declare there is no repetition to collapse
        description = self.description_of_help()
        self.assertEqual([file_type for file_type in shared if description.count(file_type) != 1], [])

    @kills.replace(
        _scanned_file_types,
        "unique(names)",
        "sorted(set(names))",
        "the help names the file types in an order of its own rather than the one they are declared in",
    )
    def test_help_names_the_file_types_in_declaration_order(self):
        """Test that the help names the file types in the order the dependency types declare them."""
        file_types = list(dict.fromkeys(self.declared_file_types()))
        self.assertEqual(sorted(file_types, key=self.description_of_help().index), file_types)

    def test_an_option_sets_its_value_and_takes_its_default_when_left_out(self):
        """Test that each option sets the value it names, and takes its default where the command line leaves it out."""
        vendor_and_legacy = [Path("vendor"), Path("packages/legacy")]
        for case, (argv, attribute, expected) in {
            "the default cooldown": ((), "cooldown", COOLDOWN.default),
            "a cooldown": (("--cooldown", "14"), "cooldown", 14),
            "the default path, the current directory": ((), "path", Path()),
            "force off by default": ((), "force", False),
            "force": (("--force",), "force", True),
            "the default log level": ((), "log_level", LOG_LEVEL.default),
            "a log level, upper-cased": (("--log-level", "debug"), "log_level", "DEBUG"),
            "the default risk level": ((), "vulnerability_level", VULNERABILITY_LEVEL.default),
            "a risk level, lower-cased": (("--vulnerability-level", "HIGH"), "vulnerability_level", "high"),
            "no path excluded by default": ((), "exclude_path", []),
            "comma-separated paths": (("--exclude-path", "vendor,packages/legacy"), "exclude_path", vendor_and_legacy),
            "paths stripped of whitespace and trailing separators": (
                ("--exclude-path", " vendor/ , packages/legacy/ "),
                "exclude_path",
                vendor_and_legacy,
            ),
            "a parent segment inside the scan root": (
                ("--exclude-path", "packages/old/../legacy"),
                "exclude_path",
                [Path("packages/legacy")],
            ),
            "an empty entry": (("--exclude-path", "vendor,"), "exclude_path", [Path("vendor")]),
        }.items():
            with self.subTest(case=case):
                self.assertEqual(getattr(self.parsed(*argv), attribute), expected)

    def parsed(self, *argv: str) -> argparse.Namespace:
        """Parse the given command-line arguments and return the resulting namespace."""
        with patch("sys.argv", ["update-time", *argv]):
            return parse_args()

    def test_an_invalid_value_is_rejected(self):
        """Test that an invalid option value is rejected with a message saying what is wrong with it."""
        for case, (argv, message) in {
            "a negative cooldown": (["--cooldown", "-1"], "-1 is not a non-negative integer"),
            "a cooldown that is not a whole number": (["--cooldown", "abc"], "invalid days value: 'abc'"),
            "a log level it does not know": (["--log-level", "verbose"], "invalid choice"),
            "a risk level it does not know": (["--vulnerability-level", "hgih"], "invalid choice"),
            "an absolute excluded path": (["--exclude-path", "/etc"], "/etc is not a relative path"),
            "an excluded path outside the scan root": (
                ["--exclude-path", "../sibling"],
                "../sibling is outside the scan root",
            ),
        }.items():
            with self.subTest(case=case):
                self.assert_rejected(argv, message)

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

    @patch_pathlib_path(is_dir=True)
    def test_path(self):
        """Test that a positional path argument is parsed as a directory."""
        self.assertEqual(Path("some-directory"), self.parsed("some-directory").path)

    @patch_pathlib_path(is_dir=False)
    def test_path_that_is_not_a_directory(self):
        """Test that a path that is not an existing directory is rejected with a user-facing message."""
        self.assert_rejected(["no-such-directory"], "no-such-directory is not an existing directory")

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

    def test_help_enumerates_the_vulnerability_levels(self):
        """Test that the help names every value --vulnerability-level accepts."""
        levels = ",".join(RISK_LEVELS)
        self.assertIn(f"--vulnerability-level {{{levels}}}", self.stdout_of("--help"))

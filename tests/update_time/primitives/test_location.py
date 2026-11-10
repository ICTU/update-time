"""Unit tests for the Location value object."""

import unittest
from pathlib import Path

from update_time.primitives.location import Location

from tests.helpers import patch_pathlib_path
from tests.mutation import Mutation, kills


class LocationTest(unittest.TestCase):
    """Unit tests for rendering a location."""

    def test_str_appends_the_line_to_the_relative_path(self):
        """Test that a location with a line renders as the working-directory-relative `path:line`."""
        self.assertEqual(str(Location(Path.cwd() / "docs" / "requirements.txt", 42)), "docs/requirements.txt:42")

    def test_str_without_a_line_is_the_relative_path(self):
        """Test that a location without a line renders as the relative path alone, with no trailing colon."""
        self.assertEqual(str(Location(Path.cwd() / "docs" / "requirements.txt")), "docs/requirements.txt")

    @kills(
        Mutation(
            Location.relative,
            "self.path.parts[:2] == cwd.parts[:2]",
            "True",
            "a path sharing only the root with the working directory is logged with a `..` for every directory",
        )
    )
    @patch_pathlib_path(cwd=Path("/repository/sub"))
    def test_relative_keeps_a_path_that_shares_only_the_root_with_the_working_directory(self):
        """Test that a path that shares only the root with the working directory stays absolute."""
        self.assertEqual(Location(Path("/elsewhere/pyproject.toml")).relative(), Path("/elsewhere/pyproject.toml"))

    def test_relative_keeps_a_relative_path(self):
        """Test that a path that is relative already stays as it is."""
        self.assertEqual(Location(Path("docs/requirements.txt")).relative(), Path("docs/requirements.txt"))

    @kills(
        Mutation(
            Location.relative,
            "self.path.parts[:2] == cwd.parts[:2]",
            "self.path.is_relative_to(cwd)",
            "a path above the working directory is logged as its absolute path",
        )
    )
    @patch_pathlib_path(cwd=Path("/repository/sub"))
    def test_relative_walks_up_to_a_path_outside_the_working_directory(self):
        """Test that a path above the working directory is made relative to it with `..`."""
        location = Location(Path("/repository/pyproject.toml"), 7)
        self.assertEqual(location.relative(), Path("../pyproject.toml"))
        self.assertEqual(str(location), "../pyproject.toml:7")

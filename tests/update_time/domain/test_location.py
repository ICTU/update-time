"""Unit tests for the Location value object."""

import unittest
from pathlib import Path

from update_time.domain.location import Location


class LocationTest(unittest.TestCase):
    """Unit tests for rendering a location."""

    def test_str_appends_the_line_to_the_relative_path(self):
        """Test that a location with a line renders as the working-directory-relative `path:line`."""
        self.assertEqual(str(Location(Path.cwd() / "docs" / "requirements.txt", 42)), "docs/requirements.txt:42")

    def test_str_without_a_line_is_the_relative_path(self):
        """Test that a location without a line renders as the relative path alone, with no trailing colon."""
        self.assertEqual(str(Location(Path.cwd() / "docs" / "requirements.txt")), "docs/requirements.txt")

    def test_relative_falls_back_to_the_absolute_path_outside_the_working_directory(self):
        """Test that a path that can't be made relative to the working directory is kept as its absolute self."""
        outside = Path("/elsewhere/pyproject.toml")
        self.assertEqual(Location(outside, 7).relative(), outside)
        self.assertEqual(str(Location(outside, 7)), "/elsewhere/pyproject.toml:7")

"""Unit tests for the TOML file format, with file I/O mocked."""

import unittest
from unittest.mock import Mock

from update_time.file_formats import toml

from tests.helpers import mock_path


class ReadTest(unittest.TestCase):
    """Unit tests for parsing a TOML file."""

    def test_valid(self):
        """Test that a valid TOML file is parsed into a dict."""
        self.assertEqual(toml.read(mock_path('[project]\nname = "x"\n')), {"project": {"name": "x"}})

    def test_malformed(self):
        """Test that a malformed TOML file reads back as None instead of raising."""
        self.assertIsNone(toml.read(mock_path("this is not = valid = toml")))

    def test_missing(self):
        """Test that a missing/unreadable TOML file reads back as None instead of raising."""
        self.assertIsNone(toml.read(Mock(read_text=Mock(side_effect=OSError))))

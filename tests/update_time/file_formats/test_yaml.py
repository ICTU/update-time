"""Unit tests for the YAML file format, with file I/O mocked."""

import unittest

from update_time.file_formats import yaml

from tests.update_time.helpers import mock_path


class ReadTest(unittest.TestCase):
    """Unit tests for parsing a YAML file."""

    def test_mapping(self):
        """Test that a YAML mapping is parsed into a dict."""
        self.assertEqual(
            yaml.read(mock_path("machine:\n  image: ubuntu-2204:1\n")), {"machine": {"image": "ubuntu-2204:1"}}
        )

    def test_sequence(self):
        """Test that a YAML sequence is parsed into a list."""
        self.assertEqual(yaml.read(mock_path("- a\n- b\n")), ["a", "b"])

    def test_empty(self):
        """Test that an empty YAML file is parsed as None."""
        self.assertIsNone(yaml.read(mock_path("")))

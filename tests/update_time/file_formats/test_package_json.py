"""Unit tests for the package.json file format, with file I/O mocked."""

import unittest

from update_time.file_formats import package_json

from tests.update_time.helpers import mock_path


class ReadTest(unittest.TestCase):
    """Unit tests for parsing a package.json."""

    def test_read(self):
        """Test that a package.json is parsed into a dict."""
        self.assertEqual(
            {"name": "x", "version": "1.0"}, package_json.read(mock_path('{"name": "x", "version": "1.0"}'))
        )

"""Unit tests for reading and rewriting files, with file I/O mocked."""

import unittest
from typing import TYPE_CHECKING
from unittest.mock import Mock

from update_time.io.file import rewrite_file

from tests.helpers import mock_path

if TYPE_CHECKING:
    from update_time.primitives.line import Line


def _rewrite_second_line(lines: list[Line]) -> list[str]:
    """Return the lines with `second` replaced by `third`, standing in for a transform that rewrites a reference."""
    return [line.text.replace("second", "third") for line in lines]


class RewriteFileTest(unittest.TestCase):
    """Unit tests for `rewrite_file`'s read/transform/write cycle."""

    def test_crlf_line_endings_preserved(self):
        """Test that a file's CRLF line endings survive a rewrite."""
        mock_file = mock_path("first\r\nsecond\r\n")
        rewrite_file(mock_file, _rewrite_second_line, Mock())
        mock_file.write_text.assert_called_once_with("first\r\nthird\r\n")

    def test_missing_final_newline_preserved(self):
        """Test that a file without a final newline does not gain one in a rewrite."""
        mock_file = mock_path("first\nsecond")
        rewrite_file(mock_file, _rewrite_second_line, Mock())
        mock_file.write_text.assert_called_once_with("first\nthird")

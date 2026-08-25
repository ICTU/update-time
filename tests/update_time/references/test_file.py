"""Unit tests for the file-rewrite orchestration."""

import unittest
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from update_time.domain.dependency import DependencyVersion
from update_time.domain.file_type import FileType
from update_time.domain.reference import Reference
from update_time.primitives.location import Location
from update_time.references.file import rewrite_file, update_file, update_files

from tests.helpers import mock_path
from tests.update_time.helpers import new_version_getter

if TYPE_CHECKING:
    from update_time.domain.line import Line

_REGEXP = r"image: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)"


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


class UpdateFileTest(unittest.TestCase):
    """Unit tests for `update_file`'s single-pass read/rewrite/write of one file."""

    def test_multiple_regexps_applied_in_one_pass(self):
        """Test that several regexps are applied to the same content, reading and writing the file once."""
        mount_regexp = r"mount: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)"
        mock_file = mock_path("image: python:3.14\nmount: redis:1.0\n")
        update_file(mock_file, _REGEXP, mount_regexp, get_new_version=new_version_getter("9.9"), logger=Mock())
        mock_file.read_text.assert_called_once_with()
        mock_file.write_text.assert_called_once_with("image: python:9.9\nmount: redis:9.9\n")


@patch("pathlib.Path.glob")
class UpdateFilesTest(unittest.TestCase):
    """Unit tests for the update file function."""

    def test_no_changes(self, mock_glob: Mock):
        """Test that files are unchanged if there is no new version."""
        mock_file = mock_path("line1\nline2\n")
        mock_glob.return_value = [mock_file]
        mock_logger = Mock()
        file_type = FileType("Dockerfiles", ("Dockerfile",))
        update_files(file_type, regexp=_REGEXP, get_new_version=new_version_getter("1.1"), logger=mock_logger)
        mock_file.write_text.assert_not_called()
        mock_logger.new_version.assert_not_called()

    def test_new_version(self, mock_glob: Mock):
        """Test that files are updated with the new version."""
        mock_file = mock_path("line1\nimage: python:3.14\n")
        mock_glob.return_value = [mock_file]
        mock_logger = Mock()
        file_type = FileType("configs", ("config.yml",))
        update_files(file_type, regexp=_REGEXP, get_new_version=new_version_getter("3.15"), logger=mock_logger)
        mock_file.write_text.assert_called_with("line1\nimage: python:3.15\n")
        # "line1" then the reference, so the reference is on line 2.
        reference = Reference("python", "3.14", Location(mock_file, 2))
        mock_logger.new_version.assert_called_with(reference, DependencyVersion(version="3.15"))

    def test_multiple_patterns(self, mock_glob: Mock):
        """Test that files matching any of multiple glob patterns are updated."""
        yml_file = mock_path("image: python:3.14\n")
        yaml_file = mock_path("image: python:3.14\n")
        mock_glob.side_effect = [[yml_file], [yaml_file]]
        mock_logger = Mock()
        file_type = FileType("YAML files", ("*.yml", "*.yaml"))
        update_files(file_type, regexp=_REGEXP, get_new_version=new_version_getter("3.15"), logger=mock_logger)
        yml_file.write_text.assert_called_with("image: python:3.15\n")
        yaml_file.write_text.assert_called_with("image: python:3.15\n")

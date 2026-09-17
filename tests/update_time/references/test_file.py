"""Unit tests for the file-rewrite orchestration."""

import unittest
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from update_time.domain.file_type import FileType
from update_time.references import file as file_module
from update_time.references.file import rewrite_file, update_file, update_yaml_files

from tests.helpers import mock_path
from tests.mutation import Mutation, kills
from tests.update_time.fixtures import IMAGE_REGEXP
from tests.update_time.references.helpers import new_version_getter

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.line import Line


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
        update_file(mock_file, IMAGE_REGEXP, mount_regexp, get_new_version=new_version_getter("9.9"), logger=Mock())
        mock_file.read_text.assert_called_once_with()
        mock_file.write_text.assert_called_once_with("image: python:9.9\nmount: redis:9.9\n")


@patch("pathlib.Path.glob")
class UpdateYamlFilesTest(unittest.TestCase):
    """Unit tests for the update YAML files function."""

    FILE_TYPE = FileType("YAML files", ("*.yml",))
    # A reference the regexp matches, so skipping is what leaves it alone, followed by an unclosed flow sequence.
    UNPARSABLE = "image: python:3.14\nbroken: [\n"

    def update(self, mock_logger: Mock, get_new_version_for: Callable[[object], NewVersionGetter]) -> None:
        """Update the discovered files, building each file's getter with the given factory."""
        update_yaml_files(
            self.FILE_TYPE, regexp=IMAGE_REGEXP, get_new_version_for=get_new_version_for, logger=mock_logger
        )

    @kills(
        Mutation(
            file_module,
            "        else:\n",
            "        if True:\n",
            "a file that does not parse is rewritten anyway, having been reported",
        ),
        Mutation(
            file_module,
            "            logger.invalid_file(path, yaml_format.FORMAT)\n",
            "            logger.invalid_file(path, yaml_format.FORMAT)\n            return\n",
            "a file that does not parse ends the walk, so the files after it are never updated",
        ),
    )
    def test_file_that_does_not_parse_is_reported_and_skipped(self, mock_glob: Mock):
        """Test that a file whose YAML does not parse is reported and left as it is, and the walk goes on."""
        unparsable_file = mock_path(self.UNPARSABLE)
        next_file = mock_path("image: python:3.14\n")
        mock_glob.return_value = [unparsable_file, next_file]
        mock_logger = Mock()
        self.update(mock_logger, lambda _document: new_version_getter("3.15"))
        unparsable_file.write_text.assert_not_called()
        next_file.write_text.assert_called_once_with("image: python:3.15\n")
        mock_logger.invalid_file.assert_called_once_with(unparsable_file, "YAML")

    @kills(
        Mutation(
            file_module,
            "        if document is yaml_format.UNPARSABLE:\n",
            "        if document is yaml_format.UNPARSABLE or document is None:\n",
            "a file that parses to nothing is reported as invalid, the empty document reading as unparsable",
        )
    )
    def test_file_that_parses_to_nothing_is_not_reported(self, mock_glob: Mock):
        """Test that a file holding only comments is taken through the update rather than reported as invalid."""
        mock_glob.return_value = [mock_path("# image: python:3.14\n")]
        mock_logger = Mock()
        get_new_version_for = Mock(return_value=new_version_getter("3.15"))
        self.update(mock_logger, get_new_version_for)
        get_new_version_for.assert_called_once_with(None)
        mock_logger.invalid_file.assert_not_called()

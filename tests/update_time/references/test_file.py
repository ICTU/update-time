"""Unit tests for the file-rewrite orchestration."""

import unittest
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from mutation_time.mutation import kills
from update_time.domain.dependency import ImageNotLookedUp
from update_time.domain.file_type import FileType
from update_time.domain.source import Source
from update_time.references import file as file_module
from update_time.references.file import update_file, update_yaml_files

from tests.helpers import mock_path
from tests.update_time.fixtures import IMAGE_REGEXP
from tests.update_time.references.helpers import mock_logger, new_version_getter

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter


class UpdateFileTest(unittest.TestCase):
    """Unit tests for `update_file`'s single-pass read/rewrite/write of one file."""

    def test_multiple_regexps_applied_in_one_pass(self):
        """Test that several regexps are applied to the same content, reading and writing the file once."""
        mount_regexp = r"mount: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)"
        mock_file = mock_path("image: python:3.14\nmount: redis:1.0\n")
        update_file(
            mock_file,
            IMAGE_REGEXP,
            mount_regexp,
            source=Source(new_version=new_version_getter("9.9")),
            logger=mock_logger(),
        )
        mock_file.read_text.assert_called_once_with()
        mock_file.write_text.assert_called_once_with("image: python:9.9\nmount: redis:9.9\n")


@patch("pathlib.Path.glob")
class UpdateYamlFilesTest(unittest.TestCase):
    """Unit tests for the update YAML files function."""

    FILE_TYPE = FileType("YAML files", ("*.yml",))
    # A reference the regexp matches, so skipping is what leaves it alone, followed by an unclosed flow sequence.
    UNPARSABLE = "image: python:3.14\nbroken: [\n"

    def update(self, logger: Mock, get_new_version: NewVersionGetter) -> None:
        """Update the discovered files, each with the given getter."""
        tag_getter = Mock(source=Source(new_version=get_new_version))
        with patch.object(file_module, "TagGetter", Mock(return_value=tag_getter)):
            update_yaml_files(
                self.FILE_TYPE,
                regexp=IMAGE_REGEXP,
                find_images=lambda _document: set(),
                reason=ImageNotLookedUp.BUILT_IMAGE,
                logger=logger,
            )

    @kills.replace(
        file_module.update_yaml_files,
        "else:\n",
        "if True:\n",
        "a file that does not parse is rewritten anyway, having been reported",
    )
    @kills.append(
        file_module.update_yaml_files,
        "logger.invalid_file(path, yaml_format.FORMAT)",
        "; return",
        "a file that does not parse ends the walk, so the files after it are never updated",
    )
    def test_file_that_does_not_parse_is_reported_and_skipped(self, mock_glob: Mock):
        """Test that a file whose YAML does not parse is reported and left as it is, and the walk goes on."""
        unparsable_file = mock_path(self.UNPARSABLE)
        next_file = mock_path("image: python:3.14\n")
        mock_glob.return_value = [unparsable_file, next_file]
        logger = mock_logger()
        self.update(logger, new_version_getter("3.15"))
        unparsable_file.write_text.assert_not_called()
        next_file.write_text.assert_called_once_with("image: python:3.15\n")
        logger.invalid_file.assert_called_once_with(unparsable_file, "YAML")

"""Unit tests for the file system module."""

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from update_time.domain.version import DependencyVersion
from update_time.io.filesystem import glob, update_file, update_files

from tests.update_time.assertions import assert_success
from tests.update_time.helpers import mock_path, new_version_getter


@patch("pathlib.Path.cwd", Mock(return_value=Path("/")))
@patch("pathlib.Path.glob")
class GlobTest(unittest.TestCase):
    """Unit tests for the glob function."""

    def test_one_file(self, mock_glob: Mock):
        """Test that a file is returned."""
        mock_glob.return_value = [Path("/file.txt")]
        self.assertEqual([Path("/file.txt")], list(glob("*.txt")))

    def test_multiple_files(self, mock_glob: Mock):
        """Test that multiple files are returned."""
        mock_glob.return_value = [Path("/file.txt"), Path("/folder/another_file.txt")]
        self.assertEqual([Path("/file.txt"), Path("/folder/another_file.txt")], list(glob("*.txt")))

    def test_start_folder(self, mock_glob: Mock):
        """Test that a different start folder can be passed."""
        mock_glob.return_value = [Path("/example/file.txt")]
        self.assertEqual([Path("/example/file.txt")], list(glob("*.txt", start=Path("/example"))))

    def test_multiple_patterns(self, mock_glob: Mock):
        """Test that multiple glob patterns can be passed."""
        mock_glob.side_effect = [[Path("/file.yml")], [Path("/file.yaml")]]
        self.assertEqual([Path("/file.yml"), Path("/file.yaml")], list(glob("*.yml", "*.yaml")))

    def test_ignore_folders(self, mock_glob: Mock):
        """Test that some folders are ignored."""
        folders_that_should_be_ignored = ["/project/build", "/example/node_modules", "/src/__pycache__", "/.git"]
        mock_glob.return_value = [Path(folder) / "file.txt" for folder in folders_that_should_be_ignored]
        self.assertEqual([], list(glob("*.txt")))

    def test_hidden_folder_named_in_pattern_is_visited(self, mock_glob: Mock):
        """Test that a hidden folder named literally in the pattern is visited, not skipped as a hidden folder."""
        files = [Path("/.devcontainer/devcontainer.json"), Path("/pkg/.devcontainer/devcontainer.json")]
        mock_glob.return_value = files
        self.assertEqual(files, list(glob(".devcontainer/devcontainer.json")))

    def test_hidden_file_named_in_pattern_is_visited(self, mock_glob: Mock):
        """Test that a top-level hidden file named literally in the pattern is visited."""
        mock_glob.return_value = [Path("/.devcontainer.json")]
        self.assertEqual([Path("/.devcontainer.json")], list(glob(".devcontainer.json")))

    def test_hidden_folder_not_named_in_pattern_is_still_skipped(self, mock_glob: Mock):
        """Test that hidden folders the pattern does not name are still skipped, even next to one it does."""
        mock_glob.return_value = [Path("/.git/.devcontainer/devcontainer.json")]
        self.assertEqual([], list(glob(".devcontainer/devcontainer.json")))


REGEXP = r"image: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)"


class UpdateFileTest(unittest.TestCase):
    """Unit tests for reading, rewriting, and writing back a single file.

    The rewriting itself is covered by the reference-rewriting engine's own tests (test_rewrite); these check that
    `update_file` reads the file, joins the rewritten lines back with a trailing newline, and only writes on change.
    """

    def test_writes_the_updated_file_when_a_reference_changed(self):
        """Test that the file is written back, with a trailing newline, when a reference was updated."""
        mock_file = mock_path("line1\nimage: python:3.14\n")
        assert_success(update_file(mock_file, REGEXP, get_new_version=new_version_getter("3.15"), logger=Mock()))
        mock_file.write_text.assert_called_once_with("line1\nimage: python:3.15\n")

    def test_does_not_write_when_nothing_changed(self):
        """Test that the file is not written when no reference was updated."""
        mock_file = mock_path("line1\nimage: python:3.14\n")
        assert_success(update_file(mock_file, REGEXP, get_new_version=new_version_getter("3.14"), logger=Mock()))
        mock_file.write_text.assert_not_called()

    def test_multiple_regexps_applied_in_one_pass(self):
        """Test that several regexps are applied to the same content, reading and writing the file once."""
        mount_regexp = r"mount: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)"
        mock_file = mock_path("image: python:3.14\nmount: redis:1.0\n")
        assert_success(
            update_file(mock_file, REGEXP, mount_regexp, get_new_version=new_version_getter("9.9"), logger=Mock())
        )
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
        assert_success(
            update_files("Dockerfile", regexp=REGEXP, get_new_version=new_version_getter("1.1"), logger=mock_logger),
        )
        mock_file.write_text.assert_not_called()
        mock_logger.new_version.assert_not_called()

    def test_new_version(self, mock_glob: Mock):
        """Test that files are updated with the new version."""
        mock_file = mock_path("line1\nimage: python:3.14\n")
        mock_glob.return_value = [mock_file]
        mock_logger = Mock()
        assert_success(
            update_files("config.yml", regexp=REGEXP, get_new_version=new_version_getter("3.15"), logger=mock_logger),
        )
        mock_file.write_text.assert_called_with("line1\nimage: python:3.15\n")
        mock_logger.new_version.assert_called_with("python", DependencyVersion(version="3.15"), mock_file)

    def test_multiple_patterns(self, mock_glob: Mock):
        """Test that files matching any of multiple glob patterns are updated."""
        yml_file = mock_path("image: python:3.14\n")
        yaml_file = mock_path("image: python:3.14\n")
        mock_glob.side_effect = [[yml_file], [yaml_file]]
        mock_logger = Mock()
        patterns = "*.yml", "*.yaml"
        assert_success(
            update_files(*patterns, regexp=REGEXP, get_new_version=new_version_getter("3.15"), logger=mock_logger),
        )
        yml_file.write_text.assert_called_with("image: python:3.15\n")
        yaml_file.write_text.assert_called_with("image: python:3.15\n")

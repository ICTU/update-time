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


REGEXP = r"image: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)"
SHA_REGEXP = r"image: (?P<dependency>[\w\d\./-]+):(?P<version>[\d\w\.\-]+)(?:@(?P<sha>sha256:[a-f0-9]{64}))?"


class UpdateFileTest(unittest.TestCase):
    """Unit tests for the update file function."""

    def test_no_changes(self):
        """Test no changes."""
        mock_file = mock_path("line1\nline2\n")
        mock_logger = Mock()
        assert_success(update_file(mock_file, "regexp", new_version_getter("1.1"), mock_logger))
        mock_file.write_text.assert_not_called()
        mock_logger.new_version.assert_not_called()

    def test_new_version(self):
        """Test a new version."""
        mock_file = mock_path("line1\nimage: python:3.14\n")
        mock_logger = Mock()
        assert_success(update_file(mock_file, REGEXP, new_version_getter("3.15"), mock_logger))
        mock_file.write_text.assert_called_with("line1\nimage: python:3.15\n")
        mock_logger.new_version.assert_called_with("python", DependencyVersion(version="3.15"), mock_file)

    def test_new_version_with_sha(self):
        """Test a new version with a sha."""
        old_sha = "a" * 40
        regexp = r"uses: (?P<dependency>[\w\d\./-]+)@(?P<sha>[a-f0-9]{40}) # v?(?P<version>[\d\w\.\-]+)"
        mock_file = mock_path(f"line1\nuses: action/action@{old_sha} # v3.14\n")
        mock_logger = Mock()
        new_sha = "b" * 40
        assert_success(update_file(mock_file, regexp, new_version_getter("3.15", new_sha), mock_logger))
        mock_file.write_text.assert_called_with(f"line1\nuses: action/action@{new_sha} # v3.15\n")
        mock_logger.new_version.assert_called_with(
            "action/action", DependencyVersion(version="3.15", sha=new_sha), mock_file
        )

    def test_unchanged_version(self):
        """Test that the file is not changed when the latest version equals the current version."""
        mock_file = mock_path("line1\nimage: python:3.14\n")
        mock_logger = Mock()
        assert_success(update_file(mock_file, REGEXP, new_version_getter("3.14"), mock_logger))
        mock_file.write_text.assert_not_called()
        mock_logger.new_version.assert_not_called()

    def test_pin_unpinned_image_at_latest_version(self):
        """Test that an unpinned image at the latest version is pinned, logging a pin rather than a new version."""
        sha = f"sha256:{'a' * 64}"
        mock_file = mock_path("line1\nimage: python:3.14\n")
        mock_logger = Mock()
        assert_success(update_file(mock_file, SHA_REGEXP, new_version_getter("3.14", sha), mock_logger))
        mock_file.write_text.assert_called_with(f"line1\nimage: python:3.14@{sha}\n")
        mock_logger.pinned.assert_called_with("python", DependencyVersion(version="3.14", sha=sha), mock_file)
        mock_logger.new_version.assert_not_called()

    def test_pin_unpinned_image_with_new_version(self):
        """Test that an unpinned image is pinned and bumped to the latest version at the same time."""
        sha = f"sha256:{'a' * 64}"
        mock_file = mock_path("line1\nimage: python:3.14\n")
        mock_logger = Mock()
        assert_success(update_file(mock_file, SHA_REGEXP, new_version_getter("3.15", sha), mock_logger))
        mock_file.write_text.assert_called_with(f"line1\nimage: python:3.15@{sha}\n")
        mock_logger.new_version.assert_called_with("python", DependencyVersion(version="3.15", sha=sha), mock_file)

    def test_unpinned_image_left_alone_without_digest(self):
        """Test that an unpinned image is not pinned when no digest is available."""
        mock_file = mock_path("line1\nimage: python:3.14\n")
        mock_logger = Mock()
        assert_success(update_file(mock_file, SHA_REGEXP, new_version_getter("3.14"), mock_logger))
        mock_file.write_text.assert_not_called()
        mock_logger.new_version.assert_not_called()

    def test_new_version_sorting_lower_than_current(self):
        """Test the regression where a newer version sorts lexicographically lower than the current one.

        get_new_version returns the highest version (compared as a packaging.Version), so e.g. "3.10" must be
        applied over "3.9" even though the string "3.10" < "3.9".
        """
        mock_file = mock_path("line1\nimage: python:3.9\n")
        mock_logger = Mock()
        assert_success(update_file(mock_file, REGEXP, new_version_getter("3.10"), mock_logger))
        mock_file.write_text.assert_called_with("line1\nimage: python:3.10\n")
        mock_logger.new_version.assert_called_with("python", DependencyVersion(version="3.10"), mock_file)

    def test_version_from_source_is_applied_even_when_lower(self):
        """Test that _update_line applies any differing version get_new_version returns, trusting the source.

        _update_line no longer guards against downgrades itself; the source functions decide the target version
        (the real ones return the maximum). This lets, for example, update_node_engine sync the package.json Node
        version down to a downgraded Docker base image.
        """
        mock_file = mock_path("line1\nimage: python:3.14\n")
        mock_logger = Mock()
        assert_success(update_file(mock_file, REGEXP, new_version_getter("3.13"), mock_logger))
        mock_file.write_text.assert_called_with("line1\nimage: python:3.13\n")
        mock_logger.new_version.assert_called_with("python", DependencyVersion(version="3.13"), mock_file)

    def test_inline_ignore_marker_pins_line(self):
        """Test that an inline `# update-time: ignore` comment leaves the line untouched, looking up no version."""
        get_new_version = Mock()
        mock_logger = Mock()
        mock_file = mock_path("image: python:3.14  # update-time: ignore\n")
        assert_success(update_file(mock_file, REGEXP, get_new_version, mock_logger))
        mock_file.write_text.assert_not_called()
        get_new_version.assert_not_called()
        mock_logger.ignored.assert_called_once_with("python", mock_file)

    def test_preceding_ignore_marker_pins_next_line(self):
        """Test that a standalone `# update-time: ignore` comment pins the reference on the line below it.

        The marker comment itself carries no reference, so only the pinned reference below it is logged as ignored.
        """
        get_new_version = Mock()
        mock_logger = Mock()
        mock_file = mock_path("# update-time: ignore\nimage: python:3.14\n")
        assert_success(update_file(mock_file, REGEXP, get_new_version, mock_logger))
        mock_file.write_text.assert_not_called()
        get_new_version.assert_not_called()
        mock_logger.ignored.assert_called_once_with("python", mock_file)

    def test_inline_marker_does_not_pin_following_line(self):
        """Test that an inline marker pins only its own line, not the reference on the line below it."""
        mock_logger = Mock()
        mock_file = mock_path("image: a:3.14  # update-time: ignore\nimage: b:3.14\n")
        assert_success(update_file(mock_file, REGEXP, new_version_getter("3.15"), mock_logger))
        mock_file.write_text.assert_called_with("image: a:3.14  # update-time: ignore\nimage: b:3.15\n")
        mock_logger.ignored.assert_called_once_with("a", mock_file)


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

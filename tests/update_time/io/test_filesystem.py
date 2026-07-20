"""Unit tests for the file system module."""

import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

if TYPE_CHECKING:
    from unittest.mock import _patch

from update_time.io.filesystem import EXCLUDE_PATHS_ENV_VAR, excluded_paths, glob, inside_git_repository

from tests.update_time.helpers import patch_environ


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
        self.assertEqual(list(glob("*.txt")), [])

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
        self.assertEqual(list(glob(".devcontainer/devcontainer.json")), [])

    @patch_environ({EXCLUDE_PATHS_ENV_VAR: "vendor"})
    def test_excluded_folder_is_skipped(self, mock_glob: Mock):
        """Test that files under a directory passed to --exclude-path are skipped."""
        mock_glob.return_value = [Path("/vendor/file.txt"), Path("/src/file.txt")]
        self.assertEqual([Path("/src/file.txt")], list(glob("*.txt")))

    @patch_environ({EXCLUDE_PATHS_ENV_VAR: "vendor"})
    def test_excluded_folder_matches_by_prefix_not_by_name(self, mock_glob: Mock):
        """Test that --exclude-path matches a relative path prefix, not a folder name anywhere in the tree."""
        mock_glob.return_value = [Path("/vendor/file.txt"), Path("/src/vendor/file.txt")]
        self.assertEqual([Path("/src/vendor/file.txt")], list(glob("*.txt")))

    @patch_environ({EXCLUDE_PATHS_ENV_VAR: "vendor,packages/legacy"})
    def test_multiple_excluded_folders_are_skipped(self, mock_glob: Mock):
        """Test that every directory in a comma-separated --exclude-path list is skipped."""
        mock_glob.return_value = [Path("/vendor/a.txt"), Path("/packages/legacy/b.txt"), Path("/packages/kept/c.txt")]
        self.assertEqual([Path("/packages/kept/c.txt")], list(glob("*.txt")))


class InsideGitRepositoryTest(unittest.TestCase):
    """Unit tests for detecting whether a directory sits inside a git repository."""

    @staticmethod
    def patch_git_entry(*git_dirs: Path) -> _patch:
        """Patch `Path.exists` so that only a `.git` entry in one of the given directories exists."""
        git_entries = {git_dir / ".git" for git_dir in git_dirs}
        return patch("pathlib.Path.exists", autospec=True, side_effect=lambda self: self in git_entries)

    def test_no_git_entry_found(self):
        """Test that a directory with no `.git` up to the filesystem root is not inside a repository."""
        with self.patch_git_entry():
            self.assertFalse(inside_git_repository(Path("/home/user/project")))

    def test_git_directory_in_the_directory_itself(self):
        """Test that a directory containing a `.git` entry is inside a repository."""
        with self.patch_git_entry(Path("/home/user/project")):
            self.assertTrue(inside_git_repository(Path("/home/user/project")))

    def test_git_entry_is_a_file(self):
        """Test that a `.git` *file* (a worktree or submodule pointer) counts, so `.is_dir()` is not required."""
        with self.patch_git_entry(Path("/home/user/project")), patch("pathlib.Path.is_dir", Mock(return_value=False)):
            self.assertTrue(inside_git_repository(Path("/home/user/project")))

    def test_git_directory_in_a_parent(self):
        """Test that a `.git` entry in a parent directory means a subdirectory is inside the repository."""
        with self.patch_git_entry(Path("/home/user/project")):
            self.assertTrue(inside_git_repository(Path("/home/user/project/src/pkg")))

    def test_defaults_to_the_working_directory(self):
        """Test that, without an explicit start, the check walks up from the working directory."""
        with (
            patch("pathlib.Path.cwd", Mock(return_value=Path("/home/user/project"))),
            self.patch_git_entry(Path("/home/user/project")),
        ):
            self.assertTrue(inside_git_repository())


class ExcludedPathsTest(unittest.TestCase):
    """Unit tests for reading the excluded paths from the environment."""

    def test_no_excluded_paths(self):
        """Test that no excluded paths are returned when the environment variable is not set."""
        with patch_environ():
            self.assertEqual(excluded_paths(), [])

    def test_empty_excluded_paths(self):
        """Test that an empty environment variable yields no excluded paths."""
        with patch_environ({EXCLUDE_PATHS_ENV_VAR: ""}):
            self.assertEqual(excluded_paths(), [])

    def test_excluded_paths(self):
        """Test that the comma-separated excluded paths are parsed into a list of paths."""
        with patch_environ({EXCLUDE_PATHS_ENV_VAR: "vendor,packages/legacy"}):
            self.assertEqual([Path("vendor"), Path("packages/legacy")], excluded_paths())

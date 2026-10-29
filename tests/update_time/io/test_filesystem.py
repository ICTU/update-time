"""Unit tests for the file system module."""

import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

if TYPE_CHECKING:
    from unittest.mock import _patch

from update_time.domain.file_type import FileType
from update_time.io.filesystem import _EXCLUDE_PATHS, configure_excluded_paths, glob_for, inside_git_repository

from tests.helpers import patch_environ, patch_pathlib_path

# The file types the walk is exercised with: the patterns and the declaration each test needs, and nothing else.
_TEXT_FILES = FileType("text files", ("*.txt",))
_TEXT_FILES_IN_EXAMPLE = FileType("text files", ("*.txt",), start="example")
_HIDDEN_TEXT_FILES = FileType("hidden text files", (".*.txt",), case_sensitive=False)
_YAML_FILES = FileType("YAML files", ("*.yml", "*.yaml"))
_DEVCONTAINER_IN_FOLDER = FileType("devcontainer configs", (".devcontainer/devcontainer.json",))
_DEVCONTAINER_AT_ROOT = FileType("devcontainer configs", (".devcontainer.json",))
_CASE_INSENSITIVE_FILES = FileType("Dockerfiles", ("Dockerfile",), case_sensitive=False)
_TEXT_FILES_AT_ROOT = FileType("text files", ("*.txt",), recursive=False)


@patch_pathlib_path(cwd=Path("/"))
@patch("pathlib.Path.glob", autospec=True)
class GlobForTest(unittest.TestCase):
    """Unit tests for walking the files a file type declares."""

    def test_one_file(self, mock_glob: Mock):
        """Test that a file is returned."""
        mock_glob.return_value = [Path("/file.txt")]
        self.assertEqual(list(glob_for(_TEXT_FILES)), [Path("/file.txt")])

    def test_multiple_files(self, mock_glob: Mock):
        """Test that multiple files are returned."""
        mock_glob.return_value = [Path("/file.txt"), Path("/folder/another_file.txt")]
        self.assertEqual(list(glob_for(_TEXT_FILES)), [Path("/file.txt"), Path("/folder/another_file.txt")])

    def test_case_sensitivity_is_taken_from_the_declaration(self, mock_glob: Mock):
        """Test that the walk asks for the case sensitivity the file type declares, so a `dockerfile` counts."""
        mock_glob.return_value = [Path("/dockerfile")]
        self.assertEqual(list(glob_for(_CASE_INSENSITIVE_FILES)), [Path("/dockerfile")])
        self.assertEqual([call.kwargs["case_sensitive"] for call in mock_glob.call_args_list], [False])

    def test_start_folder(self, mock_glob: Mock):
        """Test that the walk starts at the folder the file type declares."""
        mock_glob.return_value = [Path("/example/file.txt")]
        self.assertEqual(list(glob_for(_TEXT_FILES_IN_EXAMPLE)), [Path("/example/file.txt")])
        self.assertEqual(mock_glob.call_args.args[0], Path("/example"))

    def test_multiple_patterns(self, mock_glob: Mock):
        """Test that every glob pattern the file type declares is walked."""
        mock_glob.side_effect = [[Path("/file.yml")], [Path("/file.yaml")]]
        self.assertEqual(list(glob_for(_YAML_FILES)), [Path("/file.yml"), Path("/file.yaml")])

    def test_the_walk_recurses_only_where_the_declaration_says_so(self, mock_glob: Mock):
        """Test that a recursive file type walks the tree below its start folder, and a non-recursive one does not.

        `Path.rglob` delegates to `Path.glob` with the pattern prefixed, so recursion shows up as that prefix.
        """
        for file_type, expected in ((_TEXT_FILES, "**/*.txt"), (_TEXT_FILES_AT_ROOT, "*.txt")):
            with self.subTest(recursive=file_type.recursive):
                mock_glob.return_value = [Path("/file.txt")]
                self.assertEqual(list(glob_for(file_type)), [Path("/file.txt")])
                self.assertEqual(mock_glob.call_args.args[1], expected)

    def test_ignore_folders(self, mock_glob: Mock):
        """Test that some folders are ignored, while a file beside them is still returned."""
        folders_that_should_be_ignored = [
            "/project/build",
            "/project/target",
            "/example/node_modules",
            "/src/__pycache__",
            "/.git",
        ]
        ignored = [Path(folder) / "file.txt" for folder in folders_that_should_be_ignored]
        mock_glob.return_value = [*ignored, Path("/src/file.txt")]
        self.assertEqual(list(glob_for(_TEXT_FILES)), [Path("/src/file.txt")])

    def test_hidden_folder_named_in_pattern_is_visited(self, mock_glob: Mock):
        """Test that a hidden folder named literally in the pattern is visited, not skipped as a hidden folder."""
        files = [Path("/.devcontainer/devcontainer.json"), Path("/pkg/.devcontainer/devcontainer.json")]
        mock_glob.return_value = files
        self.assertEqual(list(glob_for(_DEVCONTAINER_IN_FOLDER)), files)

    def test_hidden_file_named_in_pattern_is_visited(self, mock_glob: Mock):
        """Test that a top-level hidden file named literally in the pattern is visited."""
        mock_glob.return_value = [Path("/.devcontainer.json")]
        self.assertEqual(list(glob_for(_DEVCONTAINER_AT_ROOT)), [Path("/.devcontainer.json")])

    def test_hidden_file_not_matched_by_a_hidden_segment_is_skipped(self, mock_glob: Mock):
        """Test that a hidden file a broad pattern matches is skipped, while a file beside it is returned."""
        mock_glob.return_value = [Path("/.env.txt"), Path("/file.txt")]
        self.assertEqual(list(glob_for(_TEXT_FILES)), [Path("/file.txt")])

    def test_hidden_file_matched_by_a_wildcard_in_pattern_is_visited_in_any_case(self, mock_glob: Mock):
        """Test that a hidden file a wildcard segment of the pattern matches is visited, whatever its case."""
        mock_glob.return_value = [Path("/.NOTES.TXT")]
        self.assertEqual(list(glob_for(_HIDDEN_TEXT_FILES)), [Path("/.NOTES.TXT")])

    def test_hidden_folder_not_named_in_pattern_is_still_skipped(self, mock_glob: Mock):
        """Test that hidden folders the pattern does not name are still skipped, even next to one it does."""
        visited = Path("/.devcontainer/devcontainer.json")
        mock_glob.return_value = [Path("/.git/.devcontainer/devcontainer.json"), visited]
        self.assertEqual(list(glob_for(_DEVCONTAINER_IN_FOLDER)), [visited])

    @patch_environ({_EXCLUDE_PATHS.name: "vendor"})
    def test_excluded_folder_is_skipped(self, mock_glob: Mock):
        """Test that files under a directory passed to --exclude-path are skipped."""
        mock_glob.return_value = [Path("/vendor/file.txt"), Path("/src/file.txt")]
        self.assertEqual(list(glob_for(_TEXT_FILES)), [Path("/src/file.txt")])

    @patch_environ({_EXCLUDE_PATHS.name: "vendor"})
    def test_excluded_folder_matches_by_prefix_not_by_name(self, mock_glob: Mock):
        """Test that --exclude-path matches a relative path prefix, not a folder name anywhere in the tree."""
        mock_glob.return_value = [Path("/vendor/file.txt"), Path("/src/vendor/file.txt")]
        self.assertEqual(list(glob_for(_TEXT_FILES)), [Path("/src/vendor/file.txt")])

    @patch_environ({_EXCLUDE_PATHS.name: "vendor,packages/legacy"})
    def test_multiple_excluded_folders_are_skipped(self, mock_glob: Mock):
        """Test that every directory in a comma-separated --exclude-path list is skipped."""
        mock_glob.return_value = [Path("/vendor/a.txt"), Path("/packages/legacy/b.txt"), Path("/packages/kept/c.txt")]
        self.assertEqual(list(glob_for(_TEXT_FILES)), [Path("/packages/kept/c.txt")])


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
            self.assertEqual(_EXCLUDE_PATHS.get(), [])

    def test_empty_excluded_paths(self):
        """Test that an empty environment variable yields no excluded paths."""
        with patch_environ({_EXCLUDE_PATHS.name: ""}):
            self.assertEqual(_EXCLUDE_PATHS.get(), [])

    def test_excluded_paths(self):
        """Test that the comma-separated excluded paths are parsed into a list of paths."""
        with patch_environ({_EXCLUDE_PATHS.name: "vendor,packages/legacy"}):
            self.assertEqual(_EXCLUDE_PATHS.get(), [Path("vendor"), Path("packages/legacy")])


@patch_environ()
class ConfigureExcludedPathsTest(unittest.TestCase):
    """Unit tests for setting the excluded directories that exist, and logging each directory given."""

    def test_a_directory_that_does_not_exist_is_left_out(self):
        """Test that a directory that does not exist is warned about and left out, and one that exists is kept."""
        logger = Mock()
        with patch("pathlib.Path.exists", autospec=True, side_effect=lambda self: self == Path("vendor")):
            configure_excluded_paths([Path("vendor"), Path("missing")], logger)
        self.assertEqual(_EXCLUDE_PATHS.get(), [Path("vendor")])
        logger.missing_excluded_path.assert_called_once_with(Path("missing"))

    @patch_pathlib_path(exists=True)
    def test_an_existing_directory_is_logged(self):
        """Test that an existing directory is logged once at DEBUG, and not warned about."""
        logger = Mock()
        configure_excluded_paths([Path("vendor")], logger)
        logger.excluded_path.assert_called_once_with(Path("vendor"))
        logger.missing_excluded_path.assert_not_called()

    @patch_pathlib_path(exists=False)
    def test_a_missing_directory_is_warned_about(self):
        """Test that a directory that does not exist is warned about rather than failing the run."""
        logger = Mock()
        configure_excluded_paths([Path("vendor")], logger)
        logger.missing_excluded_path.assert_called_once_with(Path("vendor"))
        logger.excluded_path.assert_not_called()

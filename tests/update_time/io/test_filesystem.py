"""Unit tests for the file system module."""

import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

if TYPE_CHECKING:
    from unittest.mock import _patch

from update_time.domain.file_type import FileType
from update_time.io import filesystem as filesystem_module
from update_time.io.filesystem import _EXCLUDE_PATHS, configure_excluded_paths, glob_for, inside_git_repository

from tests.helpers import patch_environ, patch_pathlib_path
from tests.mutation import Mutation, kills
from tests.update_time.helpers import SCAN_ROOT

# The file types the walk is exercised with: the patterns and the declaration each test needs, and nothing else.
_TEXT_FILES = FileType("text files", ("*.txt",))
_TEXT_FILES_IN_EXAMPLE = FileType("text files", ("*.txt",), start="example")
_TEXT_FILES_IN_HIDDEN_EXAMPLE = FileType("text files", ("*.txt",), start=".example")
_HIDDEN_TEXT_FILES = FileType("hidden text files", (".*.txt",), case_sensitive=False)
_YAML_FILES = FileType("YAML files", ("*.yml", "*.yaml"))
_DEVCONTAINER_IN_FOLDER = FileType("devcontainer configs", (".devcontainer/devcontainer.json",))
_DEVCONTAINER_AT_ROOT = FileType("devcontainer configs", (".devcontainer.json",))
_CASE_INSENSITIVE_FILES = FileType("Dockerfiles", ("Dockerfile",), case_sensitive=False)
_TEXT_FILES_AT_ROOT = FileType("text files", ("*.txt",), recursive=False)

# The git checks start from the project, and a run excludes the vendor directory.
_PROJECT = Path("/home/user/project")
_VENDOR = Path("vendor")


@patch_pathlib_path(cwd=SCAN_ROOT)
@patch("pathlib.Path.glob", autospec=True)
class GlobForTest(unittest.TestCase):
    """Unit tests for walking the files a file type declares."""

    def assert_kept(
        self, mock_glob: Mock, file_type: FileType, skipped_files: list[str], kept_files: list[str]
    ) -> None:
        """Assert that the walk over the skipped and the kept files returns the kept files alone."""
        mock_glob.return_value = [SCAN_ROOT / path for path in skipped_files + kept_files]
        self.assertEqual(list(glob_for(file_type)), [SCAN_ROOT / path for path in kept_files])

    def test_the_walk_returns_every_file_it_finds(self, mock_glob: Mock):
        """Test that the walk returns every file the glob finds."""
        for case, kept_files in (("one file", ["file.txt"]), ("several files", ["file.txt", "folder/b.txt"])):
            with self.subTest(case):
                self.assert_kept(mock_glob, _TEXT_FILES, [], kept_files)

    def test_case_sensitivity_is_taken_from_the_declaration(self, mock_glob: Mock):
        """Test that the walk asks for the case sensitivity the file type declares, so a `dockerfile` counts."""
        mock_glob.return_value = [SCAN_ROOT / "dockerfile"]
        self.assertEqual(list(glob_for(_CASE_INSENSITIVE_FILES)), [SCAN_ROOT / "dockerfile"])
        self.assertEqual([call.kwargs["case_sensitive"] for call in mock_glob.call_args_list], [False])

    def test_the_walk_starts_at_the_declared_folder(self, mock_glob: Mock):
        """Test that the walk starts at the folder the file type declares."""
        mock_glob.return_value = [SCAN_ROOT / "example/file.txt"]
        self.assertEqual(list(glob_for(_TEXT_FILES_IN_EXAMPLE)), [SCAN_ROOT / "example/file.txt"])
        self.assertEqual(mock_glob.call_args.args[0], SCAN_ROOT / "example")

    def test_every_declared_pattern_is_walked(self, mock_glob: Mock):
        """Test that every glob pattern the file type declares is walked."""
        mock_glob.side_effect = [[SCAN_ROOT / "file.yml"], [SCAN_ROOT / "file.yaml"]]
        self.assertEqual(list(glob_for(_YAML_FILES)), [SCAN_ROOT / "file.yml", SCAN_ROOT / "file.yaml"])

    def test_the_walk_recurses_only_where_the_declaration_says_so(self, mock_glob: Mock):
        """Test that a recursive file type walks the tree below its start folder, and a non-recursive one does not.

        `Path.rglob` delegates to `Path.glob` with the pattern prefixed, so recursion shows up as that prefix.
        """
        for file_type, expected in ((_TEXT_FILES, "**/*.txt"), (_TEXT_FILES_AT_ROOT, "*.txt")):
            with self.subTest(recursive=file_type.recursive):
                mock_glob.return_value = [SCAN_ROOT / "file.txt"]
                self.assertEqual(list(glob_for(file_type)), [SCAN_ROOT / "file.txt"])
                self.assertEqual(mock_glob.call_args.args[1], expected)

    def test_always_ignored_folders_are_skipped(self, mock_glob: Mock):
        """Test that the walk skips a file in an always-ignored folder, and returns a file beside it."""
        ignored_folders = ["project/build", "project/target", "example/node_modules", "src/__pycache__"]
        skipped_files = [f"{folder}/file.txt" for folder in ignored_folders]
        self.assert_kept(mock_glob, _TEXT_FILES, skipped_files, ["src/file.txt"])

    @kills(
        Mutation(
            filesystem_module.glob_for,
            "_is_skipped(path.relative_to(start)",
            "_is_skipped(path.relative_to(root)",
            "a file under a hidden start folder, such as `.github`, is skipped as hidden",
        )
    )
    def test_hidden_folders_and_files_are_skipped_unless_named(self, mock_glob: Mock):
        """Test that the walk skips a hidden folder or file, except one the pattern or the start folder names."""
        cases: tuple[tuple[str, FileType, list[str], list[str]], ...] = (
            (
                "a hidden folder the pattern names",
                _DEVCONTAINER_IN_FOLDER,
                [],
                [".devcontainer/devcontainer.json", "pkg/.devcontainer/devcontainer.json"],
            ),
            ("a hidden file the pattern names", _DEVCONTAINER_AT_ROOT, [], [".devcontainer.json"]),
            ("a hidden file outside the pattern", _TEXT_FILES, [".env.txt"], ["file.txt"]),
            ("a hidden file a wildcard matches, in any case", _HIDDEN_TEXT_FILES, [], [".NOTES.TXT"]),
            (
                "a hidden folder outside the pattern",
                _DEVCONTAINER_IN_FOLDER,
                [".git/.devcontainer/devcontainer.json"],
                [".devcontainer/devcontainer.json"],
            ),
            ("a hidden start folder, such as .github", _TEXT_FILES_IN_HIDDEN_EXAMPLE, [], [".example/file.txt"]),
        )
        for case, file_type, skipped_files, kept_files in cases:
            with self.subTest(case):
                self.assert_kept(mock_glob, file_type, skipped_files, kept_files)

    def test_exclude_path_skips_the_directories_it_names_from_the_scan_root(self, mock_glob: Mock):
        """Test that the walk skips each directory that --exclude-path names, relative to the scan root."""
        cases: tuple[tuple[str, str, FileType, list[str], list[str]], ...] = (
            ("a directory", "vendor", _TEXT_FILES, ["vendor/file.txt"], ["src/file.txt"]),
            ("a prefix, not a name", "vendor", _TEXT_FILES, ["vendor/file.txt"], ["src/vendor/file.txt"]),
            (
                "several directories",
                "vendor,packages/legacy",
                _TEXT_FILES,
                ["vendor/a.txt", "packages/legacy/b.txt"],
                ["packages/kept/c.txt"],
            ),
            (
                "a directory under the start folder",
                "example/legacy",
                _TEXT_FILES_IN_EXAMPLE,
                ["example/legacy/file.txt"],
                ["example/current/file.txt"],
            ),
            ("a namesake under the start folder", "legacy", _TEXT_FILES_IN_EXAMPLE, [], ["example/legacy/file.txt"]),
        )
        for case, exclude_path, file_type, skipped_files, kept_files in cases:
            with self.subTest(case), patch_environ({_EXCLUDE_PATHS.name: exclude_path}):
                self.assert_kept(mock_glob, file_type, skipped_files, kept_files)


class InsideGitRepositoryTest(unittest.TestCase):
    """Unit tests for detecting whether a directory sits inside a git repository."""

    @staticmethod
    def patch_git_entry(*git_dirs: Path) -> _patch:
        """Patch `Path.exists` so that only a `.git` entry in one of the given directories exists."""
        git_entries = {git_dir / ".git" for git_dir in git_dirs}
        return patch("pathlib.Path.exists", autospec=True, side_effect=lambda self: self in git_entries)

    def test_a_directory_is_inside_a_repository_where_it_or_a_parent_holds_a_git_entry(self):
        """Test that the check walks up from the directory to the filesystem root looking for a `.git` entry."""
        cases = (
            ("no .git entry up to the root", [], _PROJECT, False),
            (".git in the directory itself", [_PROJECT], _PROJECT, True),
            (".git in a parent", [_PROJECT], _PROJECT / "src" / "pkg", True),
        )
        for case, git_dirs, directory, inside in cases:
            with self.subTest(case), self.patch_git_entry(*git_dirs):
                self.assertEqual(inside_git_repository(directory), inside)

    def test_git_entry_is_a_file(self):
        """Test that a `.git` *file* (a worktree or submodule pointer) counts, so `.is_dir()` is not required."""
        with self.patch_git_entry(_PROJECT), patch("pathlib.Path.is_dir", Mock(return_value=False)):
            self.assertTrue(inside_git_repository(_PROJECT))

    def test_defaults_to_the_working_directory(self):
        """Test that, without an explicit start, the check walks up from the working directory."""
        with (
            patch("pathlib.Path.cwd", Mock(return_value=_PROJECT)),
            self.patch_git_entry(_PROJECT),
        ):
            self.assertTrue(inside_git_repository())


class ExcludedPathsTest(unittest.TestCase):
    """Unit tests for reading the excluded paths from the environment."""

    def test_the_comma_separated_excluded_paths_are_parsed_into_a_list_of_paths(self):
        """Test that the environment variable yields a path per comma-separated entry, and none when unset or empty."""
        cases: tuple[tuple[str, dict[str, str], list[Path]], ...] = (
            ("not set", {}, []),
            ("empty", {_EXCLUDE_PATHS.name: ""}, []),
            ("two paths", {_EXCLUDE_PATHS.name: "vendor,packages/legacy"}, [_VENDOR, Path("packages/legacy")]),
        )
        for case, environment, excluded_paths in cases:
            with self.subTest(case), patch_environ(environment):
                self.assertEqual(_EXCLUDE_PATHS.get(), excluded_paths)


@patch_environ()
class ConfigureExcludedPathsTest(unittest.TestCase):
    """Unit tests for setting the excluded directories that exist, and logging each directory given."""

    def test_a_directory_that_does_not_exist_is_left_out(self):
        """Test that a directory that does not exist is warned about and left out, and one that exists is kept."""
        logger = Mock()
        with patch("pathlib.Path.exists", autospec=True, side_effect=lambda self: self == _VENDOR):
            configure_excluded_paths([_VENDOR, Path("missing")], logger)
        self.assertEqual(_EXCLUDE_PATHS.get(), [_VENDOR])
        logger.missing_excluded_path.assert_called_once_with(Path("missing"))

    @patch_pathlib_path(exists=True)
    def test_an_existing_directory_is_logged(self):
        """Test that an existing directory is logged once at DEBUG, and not warned about."""
        logger = Mock()
        configure_excluded_paths([_VENDOR], logger)
        logger.excluded_path.assert_called_once_with(_VENDOR)
        logger.missing_excluded_path.assert_not_called()

    @patch_pathlib_path(exists=False)
    def test_a_missing_directory_is_warned_about(self):
        """Test that a directory that does not exist is warned about rather than failing the run."""
        logger = Mock()
        configure_excluded_paths([_VENDOR], logger)
        logger.missing_excluded_path.assert_called_once_with(_VENDOR)
        logger.excluded_path.assert_not_called()

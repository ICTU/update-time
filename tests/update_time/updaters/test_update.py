"""Unit tests for the update script that runs all updater scripts."""

import os
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

from update_time.domain.cooldown import COOLDOWN
from update_time.domain.drift import ALLOW_HASH_DRIFT
from update_time.domain.floating import ALLOW_FLOATING_PIN
from update_time.domain.vulnerability import IGNORE_VULNERABILITIES
from update_time.io.filesystem import EXCLUDE_PATHS
from update_time.io.log import LOG_LEVEL
from update_time.updaters.update import main, run_script, update_dependencies

from tests.helpers import patch_environ, patch_pathlib_path

# The updaters that must run, spelled out so this pins which ones rather than echoing the registry back at itself.
# `update.py` fails on import when these names and the `update_*.py` scripts on disk disagree.
_PARALLEL_SCRIPT_NAMES = (
    "dockerfile_base_image",
    "pyproject_toml",
    "requirements_txt",
    "github_action",
    "circle_ci_config",
    "gitlab_ci_config",
    "manifest_images",
    "devcontainer",
    "jsdelivr",
    "python_inline_script_metadata",
    "pre_commit_config",
)
_SEQUENTIAL_SCRIPT_NAMES = ("node_engine", "package_json", "python_version_file")


@patch("subprocess.run")
class RunScriptTest(unittest.TestCase):
    """Unit tests for the run_script function."""

    def test_run_script(self, mock_run: Mock):
        """Test that a script is run and its exit code is returned."""
        mock_run.return_value = Mock(returncode=0)
        self.assertEqual(run_script("dockerfile_base_image"), 0)
        args = mock_run.call_args.args[0]
        self.assertEqual(args[-1].split("/")[-1], "update_dockerfile_base_image.py")


@patch("subprocess.run")
class UpdateDependenciesTest(unittest.TestCase):
    """Unit tests for the update_dependencies function."""

    def test_all_scripts_are_run(self, mock_run: Mock):
        """Test that all updater scripts are run, the parallel ones before the sequential ones."""
        mock_run.return_value = Mock(returncode=0)
        self.assertEqual(update_dependencies(), 0)
        scripts_run = [run_call.args[0][-1].split("/")[-1] for run_call in mock_run.call_args_list]
        expected = [f"update_{name}.py" for name in (*_PARALLEL_SCRIPT_NAMES, *_SEQUENTIAL_SCRIPT_NAMES)]
        self.assertEqual(sorted(expected), sorted(scripts_run))
        sequential = [f"update_{name}.py" for name in _SEQUENTIAL_SCRIPT_NAMES]
        self.assertEqual(sequential, scripts_run[-len(_SEQUENTIAL_SCRIPT_NAMES) :])

    def test_sequential_scripts_run_in_order(self, mock_run: Mock):
        """Test that node_engine runs before package_json (they share the package.json they both rewrite)."""
        mock_run.return_value = Mock(returncode=0)
        update_dependencies()
        scripts_run = [run_call.args[0][-1].split("/")[-1] for run_call in mock_run.call_args_list]
        self.assertLess(scripts_run.index("update_node_engine.py"), scripts_run.index("update_package_json.py"))

    def test_highest_exit_code_is_returned(self, mock_run: Mock):
        """Test that the highest exit code of all scripts is returned."""
        mock_run.return_value = Mock(returncode=1)
        self.assertEqual(update_dependencies(), 1)


@patch("os.chdir", Mock())
@patch("subprocess.run")
class UpdateMainTest(unittest.TestCase):
    """Unit tests for the main function."""

    def run_main(self, mock_run: Mock, *argv: str, inside_git: bool = True) -> tuple[int, dict[str, str]]:
        """Run `main()` with the given CLI arguments and return the environment it exported for the subprocesses.

        The returned snapshot is taken while main's exports (cooldown, log level, excluded paths) are still in place,
        so a caller can assert on them even though `patch.dict` restores the real environment afterwards.
        """
        mock_run.return_value = Mock(returncode=0)
        with (
            patch_environ(),
            patch("sys.argv", ["update-time", *argv]),
            # parse_args (cli) gates on the repository check and main warns on it, so keep both patches in sync.
            patch("update_time.io.cli.inside_git_repository", Mock(return_value=inside_git)),
            patch("update_time.updaters.update.inside_git_repository", Mock(return_value=inside_git)),
        ):
            try:
                return main(), dict(os.environ)
            except SystemExit as system_exit:
                return cast(int, system_exit.code), dict(os.environ)

    def test_main_updates_dependencies(self, mock_run: Mock):
        """Test that main parses the arguments and then updates the dependencies."""
        mock_run.return_value = Mock(returncode=0)
        exit_code, environment = self.run_main(mock_run)
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[COOLDOWN.name], "7")

    def test_main_passes_cooldown_to_subprocesses(self, mock_run: Mock):
        """Test that main exports the cooldown in the environment so the updater subprocesses inherit it."""
        exit_code, environment = self.run_main(mock_run, "--cooldown", "14")
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[COOLDOWN.name], "14")

    def test_main_passes_log_level_to_subprocesses(self, mock_run: Mock):
        """Test that main exports the log level in the environment so the updater subprocesses inherit it."""
        exit_code, environment = self.run_main(mock_run, "--log-level", "debug")
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[LOG_LEVEL.name], "DEBUG")

    def test_main_passes_allow_hash_drift_off_by_default(self, mock_run: Mock):
        """Test that main exports the drift opt-in as off when --allow-hash-drift is not given."""
        exit_code, environment = self.run_main(mock_run)
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[ALLOW_HASH_DRIFT.name], "0")

    def test_main_passes_allow_hash_drift_when_set(self, mock_run: Mock):
        """Test that main exports the drift opt-in as on when --allow-hash-drift is given."""
        exit_code, environment = self.run_main(mock_run, "--allow-hash-drift")
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[ALLOW_HASH_DRIFT.name], "1")

    def test_main_passes_allow_floating_pin_off_by_default(self, mock_run: Mock):
        """Test that main exports the floating-pin opt-in as off when --allow-floating-pin is not given."""
        exit_code, environment = self.run_main(mock_run)
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[ALLOW_FLOATING_PIN.name], "0")

    def test_main_passes_allow_floating_pin_when_set(self, mock_run: Mock):
        """Test that main exports the floating-pin opt-in as on when --allow-floating-pin is given."""
        exit_code, environment = self.run_main(mock_run, "--allow-floating-pin")
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[ALLOW_FLOATING_PIN.name], "1")

    def test_main_passes_ignored_vulnerabilities_to_subprocesses(self, mock_run: Mock):
        """Test that main exports the advisories to ignore so the updater subprocesses inherit them."""
        advisories = "GHSA-2gwj-7jmv-h26r,CVE-2022-28346"
        exit_code, environment = self.run_main(mock_run, "--ignore-vulnerability", advisories)
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[IGNORE_VULNERABILITIES.name], "CVE-2022-28346,GHSA-2gwj-7jmv-h26r")

    def test_main_passes_no_ignored_vulnerabilities_by_default(self, mock_run: Mock):
        """Test that main exports an empty variable when --ignore-vulnerability is not given."""
        exit_code, environment = self.run_main(mock_run)
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[IGNORE_VULNERABILITIES.name], "")

    @patch_pathlib_path(exists=True)
    def test_main_passes_excluded_paths_to_subprocesses(self, mock_run: Mock):
        """Test that main exports the excluded paths in the environment so the updater subprocesses inherit them."""
        exit_code, environment = self.run_main(mock_run, "--exclude-path", "vendor,packages/legacy")
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[EXCLUDE_PATHS.name], "vendor,packages/legacy")

    def test_main_passes_no_excluded_paths_by_default(self, mock_run: Mock):
        """Test that main exports an empty excluded-paths variable when --exclude-path is not given."""
        exit_code, environment = self.run_main(mock_run)
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[EXCLUDE_PATHS.name], "")

    def test_main_leaves_non_existing_excluded_paths_out_of_the_environment(self, mock_run: Mock):
        """Test that a non-existing excluded path is warned about but not passed down to the subprocesses."""
        mock_logger = Mock()
        with (
            patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)),
            patch("pathlib.Path.exists", autospec=True, side_effect=lambda self: self == Path("vendor")),
        ):
            exit_code, environment = self.run_main(mock_run, "--exclude-path", "vendor,missing")
        self.assertEqual(exit_code, 0)
        self.assertEqual(environment[EXCLUDE_PATHS.name], "vendor")
        mock_logger.missing_excluded_path.assert_called_once_with(Path("missing"))

    @patch_pathlib_path(exists=True)
    def test_main_logs_existing_excluded_path(self, mock_run: Mock):
        """Test that main logs an existing excluded path once at DEBUG."""
        mock_logger = Mock()
        with patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)):
            self.run_main(mock_run, "--exclude-path", "vendor")
        mock_logger.excluded_path.assert_called_once_with(Path("vendor"))
        mock_logger.missing_excluded_path.assert_not_called()

    @patch_pathlib_path(exists=False)
    def test_main_warns_about_missing_excluded_path(self, mock_run: Mock):
        """Test that main warns about a non-existing excluded path instead of failing the run."""
        mock_logger = Mock()
        with patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)):
            self.run_main(mock_run, "--exclude-path", "vendor")
        mock_logger.missing_excluded_path.assert_called_once_with(Path("vendor"))
        mock_logger.excluded_path.assert_not_called()

    def test_main_changes_to_the_default_directory(self, mock_run: Mock):
        """Test that main changes to the current directory when no path is given."""
        with patch("os.chdir") as mock_chdir:
            self.run_main(mock_run)
        mock_chdir.assert_called_once_with(Path())

    @patch_pathlib_path(is_dir=True)
    def test_main_changes_to_the_given_directory(self, mock_run: Mock):
        """Test that main changes to the given path before spawning the updater subprocesses."""
        with patch("os.chdir") as mock_chdir:
            self.run_main(mock_run, "some-directory")
        mock_chdir.assert_called_once_with(Path("some-directory"))

    def test_main_runs_without_warning_inside_git_repository(self, mock_run: Mock):
        """Test that main updates the dependencies without warning when inside a git repository."""
        mock_run.return_value = Mock(returncode=0)
        mock_logger = Mock()
        with patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)):
            exit_code, _ = self.run_main(mock_run)
        self.assertEqual(exit_code, 0)
        mock_run.assert_called()
        mock_logger.forced_outside_git_repository.assert_not_called()

    def test_main_refuses_to_run_outside_git_repository(self, mock_run: Mock):
        """Test that main refuses to run outside a git repository."""
        with patch("sys.stderr.write") as mock_write:
            exit_code, _ = self.run_main(mock_run, inside_git=False)
        self.assertEqual(exit_code, 2)
        mock_write.assert_called_with(
            "update-time: error: . is not inside a git repository; rerun inside a repository so changes can be "
            "reverted, or pass --force to run anyway\n"
        )
        mock_run.assert_not_called()

    def test_main_warns_when_forced_to_run_outside_git_repository(self, mock_run: Mock):
        """Test that main still updates the dependencies but warns when the scan root is outside a repository."""
        mock_run.return_value = Mock(returncode=0)
        mock_logger = Mock()
        with patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)):
            exit_code, _ = self.run_main(mock_run, "--force", inside_git=False)
        self.assertEqual(exit_code, 0)
        mock_run.assert_called()
        mock_logger.forced_outside_git_repository.assert_called_once_with(Path.cwd())

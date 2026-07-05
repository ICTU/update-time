"""Unit tests for the update script that runs all updater scripts."""

import os
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from update_time.domain.cooldown import COOLDOWN_DAYS_ENV_VAR
from update_time.io.filesystem import EXCLUDE_PATHS_ENV_VAR
from update_time.io.log import LOG_LEVEL_ENV_VAR
from update_time.updaters.update import PARALLEL_SCRIPTS, SEQUENTIAL_SCRIPTS, main, run_script, update_dependencies


@patch("subprocess.run")
class UpdateTest(unittest.TestCase):
    """Unit tests for the update_dependencies function."""

    def run_main(self, mock_run: Mock, *argv: str) -> dict[str, str]:
        """Run `main()` with the given CLI arguments and return the environment it exported for the subprocesses.

        A successful subprocess run is mocked, and the environment is cleared and restored around the call. The
        returned snapshot is taken while main's exports (cooldown, log level, excluded paths) are still in place,
        so a caller can assert on them even though `patch.dict` restores the real environment afterwards. Tests
        supply the `os.chdir`, `Path.exists`, and `get_logger` patches they need via decorators or a `with` block.
        """
        mock_run.return_value = Mock(returncode=0)
        with patch.dict("os.environ", clear=True), patch("sys.argv", ["update-time", *argv]):
            main()
            return dict(os.environ)

    def test_run_script(self, mock_run: Mock):
        """Test that a script is run and its exit code is returned."""
        mock_run.return_value = Mock(returncode=0)
        self.assertEqual(0, run_script("dockerfile_base_image"))
        args = mock_run.call_args.args[0]
        self.assertEqual("update_dockerfile_base_image.py", args[-1].split("/")[-1])

    def test_all_scripts_are_run(self, mock_run: Mock):
        """Test that all updater scripts are run, the parallel ones before the sequential ones."""
        mock_run.return_value = Mock(returncode=0)
        self.assertEqual(0, update_dependencies())
        scripts_run = [run_call.args[0][-1].split("/")[-1] for run_call in mock_run.call_args_list]
        expected = [f"update_{name}.py" for name in (*PARALLEL_SCRIPTS, *SEQUENTIAL_SCRIPTS)]
        self.assertEqual(sorted(expected), sorted(scripts_run))
        self.assertEqual([f"update_{name}.py" for name in SEQUENTIAL_SCRIPTS], scripts_run[-len(SEQUENTIAL_SCRIPTS) :])

    def test_sequential_scripts_run_in_order(self, mock_run: Mock):
        """Test that the sequential scripts run after the parallel ones, node_engine before package_json."""
        mock_run.return_value = Mock(returncode=0)
        update_dependencies()
        self.assertEqual(
            [call("update_node_engine.py"), call("update_package_json.py")],
            [call(run_call.args[0][-1].split("/")[-1]) for run_call in mock_run.call_args_list[-2:]],
        )

    def test_highest_exit_code_is_returned(self, mock_run: Mock):
        """Test that the highest exit code of all scripts is returned."""
        mock_run.return_value = Mock(returncode=1)
        self.assertEqual(1, update_dependencies())

    @patch("os.chdir", Mock())
    def test_main_updates_dependencies(self, mock_run: Mock):
        """Test that main parses the arguments and then updates the dependencies."""
        mock_run.return_value = Mock(returncode=0)
        with patch("sys.argv", ["update-time"]):
            self.assertEqual(0, main())

    @patch("os.chdir", Mock())
    def test_main_passes_cooldown_to_subprocesses(self, mock_run: Mock):
        """Test that main exports the cooldown in the environment so the updater subprocesses inherit it."""
        environment = self.run_main(mock_run, "--cooldown", "14")
        self.assertEqual("14", environment[COOLDOWN_DAYS_ENV_VAR])

    @patch("os.chdir", Mock())
    def test_main_passes_log_level_to_subprocesses(self, mock_run: Mock):
        """Test that main exports the log level in the environment so the updater subprocesses inherit it."""
        environment = self.run_main(mock_run, "--log-level", "debug")
        self.assertEqual("DEBUG", environment[LOG_LEVEL_ENV_VAR])

    @patch("os.chdir", Mock())
    @patch("pathlib.Path.exists", Mock(return_value=True))
    def test_main_passes_excluded_paths_to_subprocesses(self, mock_run: Mock):
        """Test that main exports the excluded paths in the environment so the updater subprocesses inherit them."""
        environment = self.run_main(mock_run, "--exclude-path", "vendor,packages/legacy")
        self.assertEqual("vendor,packages/legacy", environment[EXCLUDE_PATHS_ENV_VAR])

    @patch("os.chdir", Mock())
    def test_main_passes_no_excluded_paths_by_default(self, mock_run: Mock):
        """Test that main exports an empty excluded-paths variable when --exclude-path is not given."""
        environment = self.run_main(mock_run)
        self.assertEqual("", environment[EXCLUDE_PATHS_ENV_VAR])

    @patch("os.chdir", Mock())
    def test_main_leaves_non_existing_excluded_paths_out_of_the_environment(self, mock_run: Mock):
        """Test that a non-existing excluded path is warned about but not passed down to the subprocesses."""
        with patch("pathlib.Path.exists", autospec=True, side_effect=lambda self: self == Path("vendor")):
            environment = self.run_main(mock_run, "--exclude-path", "vendor,missing")
        self.assertEqual("vendor", environment[EXCLUDE_PATHS_ENV_VAR])

    @patch("os.chdir", Mock())
    @patch("pathlib.Path.exists", Mock(return_value=True))
    def test_main_logs_existing_excluded_path(self, mock_run: Mock):
        """Test that main logs an existing excluded path once at DEBUG."""
        mock_logger = Mock()
        with patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)):
            self.run_main(mock_run, "--exclude-path", "vendor")
        mock_logger.excluded_path.assert_called_once_with(Path("vendor"))
        mock_logger.missing_excluded_path.assert_not_called()

    @patch("os.chdir", Mock())
    @patch("pathlib.Path.exists", Mock(return_value=False))
    def test_main_warns_about_missing_excluded_path(self, mock_run: Mock):
        """Test that main warns about a non-existing excluded path instead of failing the run."""
        mock_logger = Mock()
        with patch("update_time.updaters.update.get_logger", Mock(return_value=mock_logger)):
            self.run_main(mock_run, "--exclude-path", "vendor")
        mock_logger.missing_excluded_path.assert_called_once_with(Path("vendor"))
        mock_logger.excluded_path.assert_not_called()

    @patch("os.chdir")
    def test_main_changes_to_the_default_directory(self, mock_chdir: Mock, mock_run: Mock):
        """Test that main changes to the current directory when no path is given."""
        self.run_main(mock_run)
        mock_chdir.assert_called_once_with(Path())

    @patch("pathlib.Path.is_dir", Mock(return_value=True))
    @patch("os.chdir")
    def test_main_changes_to_the_given_directory(self, mock_chdir: Mock, mock_run: Mock):
        """Test that main changes to the given path before spawning the updater subprocesses."""
        self.run_main(mock_run, "some-directory")
        mock_chdir.assert_called_once_with(Path("some-directory"))

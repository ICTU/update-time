"""Unit tests for the update script that runs all updater scripts."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from update_time.domain.cooldown import COOLDOWN_DAYS_ENV_VAR
from update_time.io.log import LOG_LEVEL_ENV_VAR
from update_time.updaters.update import (
    GIT_REPOSITORY_REFUSAL_MESSAGE,
    PARALLEL_SCRIPTS,
    SEQUENTIAL_SCRIPTS,
    is_inside_git_repository,
    main,
    run_script,
    update_dependencies,
)


def script_name(script: object) -> str:
    """Return the updater script file name from a subprocess argument."""
    return Path(script).name


class GitRepositoryDetectionTest(unittest.TestCase):
    """Unit tests for detecting whether the current path is inside a git repository."""

    def test_detects_git_directory(self):
        """Test that a .git directory marks the current path as inside a repository."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / ".git").mkdir()
            self.assertTrue(is_inside_git_repository(path))

    def test_detects_git_file(self):
        """Test that a .git file marks the current path as inside a worktree or submodule repository."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / ".git").write_text("gitdir: ../.git/worktrees/example\n")
            self.assertTrue(is_inside_git_repository(path))

    def test_detects_parent_git_entry(self):
        """Test that a .git entry in a parent directory is detected."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            child = path / "subdir" / "nested"
            child.mkdir(parents=True)
            (path / ".git").mkdir()
            self.assertTrue(is_inside_git_repository(child))

    def test_returns_false_without_git_entry(self):
        """Test that a path outside a git repository is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(is_inside_git_repository(Path(tmp)))


@patch("subprocess.run")
class UpdateTest(unittest.TestCase):
    """Unit tests for the update_dependencies function."""

    def test_run_script(self, mock_run: Mock):
        """Test that a script is run and its exit code is returned."""
        mock_run.return_value = Mock(returncode=0)
        self.assertEqual(0, run_script("dockerfile_base_image"))
        args = mock_run.call_args.args[0]
        self.assertEqual("update_dockerfile_base_image.py", script_name(args[-1]))

    def test_all_scripts_are_run(self, mock_run: Mock):
        """Test that all updater scripts are run, the parallel ones before the sequential ones."""
        mock_run.return_value = Mock(returncode=0)
        self.assertEqual(0, update_dependencies())
        scripts_run = [script_name(run_call.args[0][-1]) for run_call in mock_run.call_args_list]
        expected = [f"update_{name}.py" for name in (*PARALLEL_SCRIPTS, *SEQUENTIAL_SCRIPTS)]
        self.assertEqual(sorted(expected), sorted(scripts_run))
        self.assertEqual([f"update_{name}.py" for name in SEQUENTIAL_SCRIPTS], scripts_run[-len(SEQUENTIAL_SCRIPTS) :])

    def test_sequential_scripts_run_in_order(self, mock_run: Mock):
        """Test that the sequential scripts run after the parallel ones, node_engine before package_json."""
        mock_run.return_value = Mock(returncode=0)
        update_dependencies()
        self.assertEqual(
            [call("update_node_engine.py"), call("update_package_json.py")],
            [call(script_name(run_call.args[0][-1])) for run_call in mock_run.call_args_list[-2:]],
        )

    def test_highest_exit_code_is_returned(self, mock_run: Mock):
        """Test that the highest exit code of all scripts is returned."""
        mock_run.return_value = Mock(returncode=1)
        self.assertEqual(1, update_dependencies())

    def test_main_updates_dependencies(self, mock_run: Mock):
        """Test that main parses the arguments and then updates the dependencies."""
        mock_run.return_value = Mock(returncode=0)
        with patch("sys.argv", ["update-time"]):
            self.assertEqual(0, main())

    def test_main_passes_cooldown_to_subprocesses(self, mock_run: Mock):
        """Test that main exports the cooldown in the environment so the updater subprocesses inherit it."""
        mock_run.return_value = Mock(returncode=0)
        with patch.dict("os.environ", clear=True), patch("sys.argv", ["update-time", "--cooldown", "14"]):
            main()
            self.assertEqual("14", os.environ[COOLDOWN_DAYS_ENV_VAR])

    def test_main_passes_log_level_to_subprocesses(self, mock_run: Mock):
        """Test that main exports the log level in the environment so the updater subprocesses inherit it."""
        mock_run.return_value = Mock(returncode=0)
        with patch.dict("os.environ", clear=True), patch("sys.argv", ["update-time", "--log-level", "debug"]):
            main()
            self.assertEqual("DEBUG", os.environ[LOG_LEVEL_ENV_VAR])

    def test_main_refuses_outside_git_repository(self, mock_run: Mock):
        """Test that main refuses to run outside a git repository before spawning updater subprocesses."""
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("sys.argv", ["update-time"]),
            patch("update_time.updaters.update.Path.cwd", return_value=Path(tmp)),
            patch("logging.Logger.error") as mock_error,
        ):
            self.assertEqual(1, main())
        mock_run.assert_not_called()
        mock_error.assert_called_once_with(GIT_REPOSITORY_REFUSAL_MESSAGE)

    def test_main_force_overrides_git_repository_refusal(self, mock_run: Mock):
        """Test that --force lets main run outside a git repository."""
        mock_run.return_value = Mock(returncode=0)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("sys.argv", ["update-time", "--force"]),
            patch("update_time.updaters.update.Path.cwd", return_value=Path(tmp)),
        ):
            self.assertEqual(0, main())
        mock_run.assert_called()

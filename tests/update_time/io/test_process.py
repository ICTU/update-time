"""Unit tests for running processes."""

import subprocess  # nosec
from pathlib import Path
from unittest import TestCase
from unittest.mock import ANY, Mock, patch

from update_time.io.process import run


@patch("subprocess.run")
class RunTests(TestCase):
    """Unit tests for the run function."""

    def test_stdout_is_returned(self, mock_run: Mock):
        """Test that the stdout of a successful command is returned."""
        mock_run.return_value = Mock(stdout="output")
        self.assertEqual("output", run(["tool", "--version"]))

    def test_command_and_cwd_are_passed(self, mock_run: Mock):
        """Test that the command and working directory are passed to the subprocess."""
        mock_run.return_value = Mock(stdout="")
        run(["tool", "list"], cwd=Path("/dir"))
        self.assertEqual((["tool", "list"],), mock_run.call_args.args)
        self.assertEqual(Path("/dir"), mock_run.call_args.kwargs["cwd"])

    @patch("logging.Logger.warning")
    def test_non_zero_exit_returns_stdout(self, mock_warning: Mock, mock_run: Mock):
        """Test that a non-zero exit (the normal case for e.g. `npm outdated`) still returns stdout without warning."""
        mock_run.side_effect = subprocess.CalledProcessError(returncode=1, cmd="", output="output", stderr="")
        self.assertEqual("output", run(["npm", "outdated"]))
        mock_warning.assert_not_called()

    @patch("logging.Logger.warning")
    def test_stderr_is_logged_on_failure(self, mock_warning: Mock, mock_run: Mock):
        """Test that stderr is logged when a command fails, so genuine failures aren't swallowed silently."""
        mock_run.side_effect = subprocess.CalledProcessError(returncode=1, cmd="", output="", stderr="boom\n")
        self.assertEqual("", run(["uv", "lock"]))
        mock_warning.assert_called_once_with("Error running %s:\n%s", "uv lock", "boom", stacklevel=ANY)

    @patch("logging.Logger.error")
    def test_missing_executable_is_logged(self, mock_error: Mock, mock_run: Mock):
        """Test that a missing executable is logged and an empty result returned rather than crashing the run."""
        mock_run.side_effect = FileNotFoundError
        self.assertEqual("", run(["npm", "outdated"]))
        mock_error.assert_called_once_with("Could not run %s: is %s installed?", "npm outdated", "npm", stacklevel=ANY)

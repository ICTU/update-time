"""Unit tests for running processes."""

import subprocess  # nosec
from pathlib import Path
from unittest import TestCase
from unittest.mock import ANY, Mock, patch

from update_time.io.process import run, run_json


@patch("subprocess.run")
class RunTests(TestCase):
    """Unit tests for the run function (action commands whose non-zero exit means the action failed)."""

    def test_stdout_is_returned(self, mock_run: Mock):
        """Test that the stdout of a successful command is returned."""
        mock_run.return_value = Mock(stdout="output", stderr="")
        self.assertEqual("output", run(["tool", "--version"]))

    def test_command_and_cwd_are_passed(self, mock_run: Mock):
        """Test that the command and working directory are passed to the subprocess."""
        mock_run.return_value = Mock(stdout="", stderr="")
        run(["tool", "list"], cwd=Path("/dir"))
        self.assertEqual((["tool", "list"],), mock_run.call_args.args)
        self.assertEqual(Path("/dir"), mock_run.call_args.kwargs["cwd"])

    @patch("logging.Logger.warning")
    def test_non_zero_exit_without_stderr_returns_stdout(self, mock_warning: Mock, mock_run: Mock):
        """Test that a non-zero exit with no stderr still returns stdout without warning."""
        mock_run.side_effect = subprocess.CalledProcessError(returncode=1, cmd="", output="output", stderr="")
        self.assertEqual("output", run(["uv", "lock"]))
        mock_warning.assert_not_called()

    @patch("logging.Logger.warning")
    def test_stderr_is_logged_on_failure(self, mock_warning: Mock, mock_run: Mock):
        """Test that stderr is logged when an action command fails, so genuine failures aren't swallowed silently."""
        mock_run.side_effect = subprocess.CalledProcessError(returncode=1, cmd="", output="", stderr="boom\n")
        self.assertEqual("", run(["uv", "lock"]))
        mock_warning.assert_called_once_with("%s wrote to stderr:\n%s", "uv lock", "boom", stacklevel=ANY)

    @patch("logging.Logger.error")
    def test_missing_executable_is_logged(self, mock_error: Mock, mock_run: Mock):
        """Test that a missing executable is logged and an empty result returned rather than crashing the run."""
        mock_run.side_effect = FileNotFoundError
        self.assertEqual("", run(["npm", "outdated"]))
        mock_error.assert_called_once_with("Could not run %s: is %s installed?", "npm outdated", "npm", stacklevel=ANY)


@patch("subprocess.run")
class RunJsonTests(TestCase):
    """Unit tests for run_json (commands whose non-zero exit is a normal 'there is data' signal)."""

    def test_output_is_parsed(self, mock_run: Mock):
        """Test that JSON output from a successful command is parsed."""
        mock_run.return_value = Mock(stdout='{"a": 1}', stderr="")
        self.assertEqual({"a": 1}, run_json(["npm", "list"]))

    @patch("logging.Logger.warning")
    def test_non_zero_exit_with_output_is_parsed_without_warning(self, mock_warning: Mock, mock_run: Mock):
        """Test that a non-zero exit is fine as long as parseable output was produced, even with stderr chatter.

        `pnpm outdated` exits non-zero when packages are outdated and can print a deprecation `[WARN]` to stderr; the
        result is still usable, so it must not be logged as a problem.
        """
        error = subprocess.CalledProcessError(returncode=1, cmd="", output='{"pkg": {}}', stderr="[WARN] deprecated\n")
        mock_run.side_effect = error
        self.assertEqual({"pkg": {}}, run_json(["pnpm", "outdated"]))
        mock_warning.assert_not_called()

    @patch("logging.Logger.warning")
    def test_no_output_with_stderr_is_logged(self, mock_warning: Mock, mock_run: Mock):
        """Test that stderr is logged when the command produced no usable output (a genuine failure)."""
        mock_run.side_effect = subprocess.CalledProcessError(returncode=1, cmd="", output="", stderr="boom\n")
        self.assertEqual({}, run_json(["pnpm", "outdated"]))
        mock_warning.assert_called_once_with("%s wrote to stderr:\n%s", "pnpm outdated", "boom", stacklevel=ANY)

    @patch("logging.Logger.warning")
    def test_empty_output_without_failure_is_not_logged(self, mock_warning: Mock, mock_run: Mock):
        """Test that an empty result from a successful command is treated as no data, without warning."""
        mock_run.return_value = Mock(stdout="", stderr="")
        self.assertEqual({}, run_json(["npm", "outdated"]))
        mock_warning.assert_not_called()

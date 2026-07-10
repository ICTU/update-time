"""Unit tests for running processes."""

import subprocess  # nosec
from pathlib import Path
from unittest import TestCase
from unittest.mock import ANY, Mock, patch

from update_time.io.log import Logger
from update_time.io.process import run


@patch("subprocess.run")
class RunTests(TestCase):
    """Unit tests for the run function and its Result."""

    def test_stdout_is_returned(self, mock_run: Mock):
        """Test that the stdout of a successful command is captured."""
        mock_run.return_value = Mock(stdout="output", stderr="")
        self.assertEqual("output", run(["tool", "--version"]).stdout)

    def test_command_and_cwd_are_passed(self, mock_run: Mock):
        """Test that the command and working directory are passed to the subprocess."""
        mock_run.return_value = Mock(stdout="", stderr="")
        run(["tool", "list"], cwd=Path("/dir"))
        self.assertEqual((["tool", "list"],), mock_run.call_args.args)
        self.assertEqual(Path("/dir"), mock_run.call_args.kwargs["cwd"])

    def test_cwd_defaults_to_none(self, mock_run: Mock):
        """Test that no working directory is forwarded (cwd=None) when the caller doesn't specify one."""
        mock_run.return_value = Mock(stdout="", stderr="")
        run(["tool", "list"])
        self.assertIsNone(mock_run.call_args.kwargs["cwd"])

    def test_json_is_parsed(self, mock_run: Mock):
        """Test that stdout is parsed as JSON on demand."""
        mock_run.return_value = Mock(stdout='{"a": 1}', stderr="")
        self.assertEqual({"a": 1}, run(["npm", "list"]).json)

    def test_empty_output_parses_to_an_empty_dict(self, mock_run: Mock):
        """Test that a command with no output parses to an empty dict rather than crashing."""
        mock_run.return_value = Mock(stdout="", stderr="")
        self.assertEqual({}, run(["npm", "list"]).json)

    def test_clean_exit_is_ok(self, mock_run: Mock):
        """Test that a command that exits cleanly is ok, even with no output."""
        mock_run.return_value = Mock(stdout="", stderr="")
        self.assertTrue(run(["uv", "lock"]).ok)

    @patch("logging.Logger.warning")
    def test_non_zero_exit_with_output_is_ok_without_warning(self, mock_warning: Mock, mock_run: Mock):
        """Test that a non-zero exit is ok as long as output was produced, and isn't logged even with stderr chatter.

        `pnpm outdated` exits non-zero when packages are outdated and can print a deprecation `[WARN]` to stderr; the
        result is still usable, so it must not be logged as a problem.
        """
        error = subprocess.CalledProcessError(returncode=1, cmd="", output='{"pkg": {}}', stderr="[WARN] deprecated\n")
        mock_run.side_effect = error
        result = run(["pnpm", "outdated"])
        self.assertEqual({"pkg": {}}, result.json)
        self.assertTrue(result.ok)
        mock_warning.assert_not_called()

    @patch("logging.Logger.warning")
    def test_failure_without_output_is_not_ok_and_is_logged(self, mock_warning: Mock, mock_run: Mock):
        """Test that a command that both failed and produced nothing is not ok, and its stderr is surfaced."""
        mock_run.side_effect = subprocess.CalledProcessError(returncode=1, cmd="", output="", stderr="boom\n")
        result = run(["uv", "lock"])
        self.assertEqual("", result.stdout)
        self.assertFalse(result.ok)
        mock_warning.assert_called_once_with(Logger._MESSAGE_COMMAND_STDERR, "uv lock", "boom", stacklevel=ANY)

    @patch("logging.Logger.error")
    def test_missing_executable_is_logged(self, mock_error: Mock, mock_run: Mock):
        """Test that a missing executable is logged and a failed, empty result returned rather than crashing."""
        mock_run.side_effect = FileNotFoundError
        result = run(["npm", "outdated"])
        self.assertEqual("", result.stdout)
        self.assertFalse(result.ok)
        mock_error.assert_called_once_with(Logger._MESSAGE_COMMAND_NOT_FOUND, "npm outdated", "npm", stacklevel=ANY)

"""Unit tests for running processes."""

import subprocess  # nosec
from pathlib import Path
from unittest.mock import Mock, patch

from update_time.io.log import Logger
from update_time.io.process import run
from update_time.primitives.command import Command

from tests.update_time.helpers import LoggingTestCase


@patch("subprocess.run")
class RunTests(LoggingTestCase):
    """Unit tests for the run function and its Result."""

    def test_stdout_is_returned(self, mock_run: Mock):
        """Test that the stdout of a successful command is captured."""
        mock_run.return_value = Mock(stdout="output", stderr="")
        self.assertEqual(run(Command("tool", "--version")).stdout, "output")

    def test_captured_stderr_loses_its_trailing_newline(self, mock_run: Mock):
        """Test that a result holds its captured stderr without the newline the command ended it with."""
        mock_run.return_value = Mock(stdout="", stderr="boom\n")
        self.assertEqual(run(Command("uv", "lock")).stderr, "boom")

    def test_command_and_cwd_are_passed(self, mock_run: Mock):
        """Test that the command and working directory are passed to the subprocess."""
        mock_run.return_value = Mock(stdout="", stderr="")
        run(Command("tool", "list"), cwd=Path("/dir"))
        self.assertEqual(mock_run.call_args.args, (Command("tool", "list"),))
        self.assertEqual(Path("/dir"), mock_run.call_args.kwargs["cwd"])

    def test_cwd_defaults_to_none(self, mock_run: Mock):
        """Test that no working directory is forwarded (cwd=None) when the caller doesn't specify one."""
        mock_run.return_value = Mock(stdout="", stderr="")
        run(Command("tool", "list"))
        self.assertIsNone(mock_run.call_args.kwargs["cwd"])

    def test_json_is_parsed(self, mock_run: Mock):
        """Test that stdout is parsed as JSON on demand."""
        mock_run.return_value = Mock(stdout='{"a": 1}', stderr="")
        self.assertEqual(run(Command("npm", "list")).json, {"a": 1})

    def test_empty_output_parses_to_an_empty_dict(self, mock_run: Mock):
        """Test that a command with no output parses to an empty dict rather than crashing."""
        mock_run.return_value = Mock(stdout="", stderr="")
        self.assertEqual(run(Command("npm", "list")).json, {})

    def test_clean_exit_is_ok(self, mock_run: Mock):
        """Test that a command that exits cleanly is ok, even with no output."""
        mock_run.return_value = Mock(stdout="", stderr="")
        self.assertTrue(run(Command("uv", "lock")).ok)

    def test_non_zero_exit_with_output_is_ok_without_warning(self, mock_run: Mock):
        """Test that a non-zero exit is ok as long as output was produced, and isn't logged even with stderr chatter."""
        error = subprocess.CalledProcessError(returncode=1, cmd="", output='{"pkg": {}}', stderr="[WARN] deprecated\n")
        mock_run.side_effect = error
        result = run(Command("pnpm", "outdated"))
        self.assertEqual(result.json, {"pkg": {}})
        self.assertTrue(result.ok)
        self.mock_log.assert_not_called()

    def test_failure_without_output_is_not_ok_and_is_logged(self, mock_run: Mock):
        """Test that a command that both failed and produced nothing is not ok, and its stderr is surfaced."""
        mock_run.side_effect = subprocess.CalledProcessError(returncode=1, cmd="", output="", stderr="boom\n")
        result = run(Command("uv", "lock"))
        self.assertEqual(result.stdout, "")
        self.assertFalse(result.ok)
        self.assert_command_stderr_logged(Command("uv", "lock"), "boom")

    def test_whitespace_only_stderr_is_not_logged(self, mock_run: Mock):
        """Test that a failure whose stderr holds only whitespace logs nothing, rather than an empty warning."""
        mock_run.side_effect = subprocess.CalledProcessError(returncode=1, cmd="", output="", stderr="\n")
        run(Command("uv", "lock"))
        self.mock_log.assert_not_called()

    def test_missing_executable_is_logged(self, mock_run: Mock):
        """Test that a missing executable is logged and a failed, empty result returned rather than crashing."""
        mock_run.side_effect = FileNotFoundError
        result = run(Command("npm", "outdated"))
        self.assertEqual(result.stdout, "")
        self.assertFalse(result.ok)
        self.assert_error_logged(
            Logger._MESSAGE_COMMAND_NOT_FOUND, command=Command("npm", "outdated"), executable="npm"
        )

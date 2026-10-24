"""Unit tests for running processes."""

import logging
import signal
import subprocess  # nosec
import unittest
from functools import partial
from logging import DEBUG, WARNING
from pathlib import Path
from unittest.mock import Mock, call, patch

from update_time.io import process as process_module
from update_time.io.log import Logger
from update_time.io.process import Processes, run
from update_time.primitives.command import Command
from update_time.primitives.location import Location

from tests.mutation import Mutation, kills
from tests.update_time.helpers import LoggingTestCase, message_of


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

    def test_an_executable_that_cannot_be_run_is_logged(self, mock_run: Mock):
        """Test that an executable the system refuses to run is logged, rather than ending the run."""
        refused = PermissionError(13, "Permission denied")
        mock_run.side_effect = refused
        result = run(Command("./mvnw", "--version"))
        self.assertEqual(result.stdout, "")
        self.assertFalse(result.ok)
        self.assert_error_logged(
            message_of(Logger.command_error), command=Command("./mvnw", "--version"), error=refused
        )

    def test_missing_executable_is_logged(self, mock_run: Mock):
        """Test that a missing executable is logged and a failed, empty result returned rather than crashing."""
        mock_run.side_effect = FileNotFoundError
        result = run(Command("npm", "outdated"))
        self.assertEqual(result.stdout, "")
        self.assertFalse(result.ok)
        self.assert_error_logged(
            message_of(Logger.command_not_found), command=Command("npm", "outdated"), executable="npm"
        )


@patch("subprocess.run")
class ToolVersionTests(LoggingTestCase):
    """Unit tests for logging the version of each tool the run invokes."""

    def setUp(self) -> None:
        """Log at debug level, where the tool versions are logged."""
        super().setUp()
        root = logging.getLogger()
        self.addCleanup(root.setLevel, root.level)
        root.setLevel(DEBUG)

    @staticmethod
    def commands_run(mock_run: Mock) -> list[tuple[Command, Path | None]]:
        """Return each command the stub for subprocess.run received, with the directory it ran in."""
        return [(each.args[0], each.kwargs["cwd"]) for each in mock_run.call_args_list]

    def test_version_is_logged_with_the_directory_the_command_runs_in(self, mock_run: Mock):
        """Test that the version of the executable is logged with the directory the command runs in."""
        mock_run.return_value = Mock(stdout="11.19.1\n", stderr="")
        run(Command("npm", "outdated"), cwd=Path("/project"))
        self.assert_logged(
            message_of(Logger.tool_version),
            executable="npm",
            directory=Location(Path("/project")),
            version="11.19.1",
        )

    def test_version_of_a_command_run_in_no_directory_names_the_scan_root(self, mock_run: Mock):
        """Test that the version of an executable run in no particular directory names the scan root it runs in."""
        mock_run.return_value = Mock(stdout="uv 0.13.1\n", stderr="")
        run(Command("uv", "lock"))
        self.assert_logged(
            message_of(Logger.tool_version),
            executable="uv",
            directory=Location(Path.cwd()),
            version="uv 0.13.1",
        )

    def test_version_is_asked_once_per_executable_and_directory(self, mock_run: Mock):
        """Test that an executable is asked for its version before its first command in each directory alone."""
        mock_run.return_value = Mock(stdout="11.19.1\n", stderr="")
        project, frontend = Path("/project"), Path("/project/frontend")
        run(Command("npm", "outdated"), cwd=project)
        run(Command("npm", "update"), cwd=project)
        run(Command("npm", "list"), cwd=frontend)
        run(Command("uv", "lock"))
        self.assertEqual(
            self.commands_run(mock_run),
            [
                (Command("npm", "--version"), project),
                (Command("npm", "outdated"), project),
                (Command("npm", "update"), project),
                (Command("npm", "--version"), frontend),
                (Command("npm", "list"), frontend),
                (Command("uv", "--version"), Path.cwd()),
                (Command("uv", "lock"), None),
            ],
        )

    def test_no_directory_and_the_working_directory_share_one_version_query(self, mock_run: Mock):
        """Test that a command run in no directory and one run in the working directory ask for the version once."""
        mock_run.return_value = Mock(stdout="uv 0.13.1\n", stderr="")
        run(Command("uv", "lock"))
        run(Command("uv", "sync"), cwd=Path.cwd())
        commands = [command for command, _directory in self.commands_run(mock_run)]
        self.assertEqual(commands.count(Command("uv", "--version")), 1)

    def test_a_version_that_cannot_be_determined_is_warned_about(self, mock_run: Mock):
        """Test that a failing version query is warned about with its reason, while the command itself still runs."""
        unknown_option = subprocess.CalledProcessError(1, "", output="", stderr="unknown option: --version\n")
        no_java = subprocess.CalledProcessError(
            1, "", output="The JAVA_HOME environment variable is not set\n", stderr=""
        )
        for case, outcome, reason in (
            ("non-zero exit", unknown_option, "unknown option: --version"),
            ("non-zero exit, error on stdout", no_java, "The JAVA_HOME environment variable is not set"),
            ("nothing on stdout", Mock(stdout="", stderr=""), "it did not print a version"),
            (
                "non-zero exit, no output",
                subprocess.CalledProcessError(1, "", output="", stderr=""),
                "it failed without saying why",
            ),
        ):
            with self.subTest(case):
                mock_run.side_effect = [outcome, Mock(stdout="output", stderr="")]
                self.assertEqual(run(Command("pnpm", "outdated"), cwd=Path("/project")).stdout, "output")
                self.assert_logged(
                    message_of(Logger.tool_version_unknown),
                    executable="pnpm",
                    directory=Location(Path("/project")),
                    reason=reason,
                )
                self.assertEqual(self.records(DEBUG), [])

    def test_a_tool_that_cannot_run_is_reported_once(self, mock_run: Mock):
        """Test that a tool the system cannot run is reported by its command alone, not by its version query too."""
        refused = PermissionError(13, "Permission denied")
        for case, error, message, fields in (
            ("not installed", FileNotFoundError(), message_of(Logger.command_not_found), {"executable": "pnpm"}),
            ("refused", refused, message_of(Logger.command_error), {"error": refused}),
        ):
            with self.subTest(case):
                mock_run.reset_mock()
                mock_run.side_effect = error
                run(Command("pnpm", "outdated"), cwd=Path("/project"))
                self.assertEqual(mock_run.call_count, 2)
                self.assertEqual(self.records(WARNING), [])
                self.assert_error_logged(message, command=Command("pnpm", "outdated"), **fields)


@patch("subprocess.Popen")
class ProcessesTest(unittest.TestCase):
    """Unit tests for the processes a run started."""

    @kills(
        Mutation(
            process_module.Processes._start,
            "if self.stop_signal is not None:\n            return None",
            "if False:\n            return None",
            "a process starts after the run stopped, and outlives the run",
        )
    )
    @patch("os.killpg")
    def test_a_stopped_run_starts_no_further_process(self, _mock_killpg: Mock, mock_popen: Mock):
        """Test that once the run stops, a process about to start does not, and reports the exit code of SIGTERM."""
        processes = Processes()
        processes.stop(signal.SIGINT)
        self.assertEqual(processes.run(Command("tool")), [-signal.SIGTERM])
        mock_popen.assert_not_called()

    @patch("os.killpg")
    def test_a_process_runs_and_stops_as_a_process_group_of_its_own(self, mock_killpg: Mock, mock_popen: Mock):
        """Test that a process runs in a process group of its own, which stopping the run sends a SIGTERM."""
        processes = Processes()
        mock_popen.return_value.wait.side_effect = partial(processes.stop, signal.SIGINT)
        processes.run(Command("tool"))
        mock_popen.assert_called_once_with(Command("tool"), process_group=0)
        mock_killpg.assert_called_once_with(mock_popen.return_value.pid, signal.SIGTERM)

    @patch("os.killpg")
    def test_stopping_passes_over_a_process_whose_process_group_ended(self, mock_killpg: Mock, mock_popen: Mock):
        """Test that stopping the run passes over a process that ended, waited for or not, and stops the others."""
        for case, refusal in (("waited for", ProcessLookupError), ("not waited for", PermissionError)):
            with self.subTest(case):
                mock_killpg.reset_mock()
                processes = Processes()
                ended, running = Mock(pid=1), Mock(pid=2)
                mock_popen.side_effect = [ended, running]
                ended.wait.side_effect = partial(processes.stop, signal.SIGINT)

                def killpg(pid: int, _signal: int, ended: Mock = ended, refusal: type[OSError] = refusal) -> None:
                    if pid == ended.pid:
                        raise refusal

                mock_killpg.side_effect = killpg
                processes.run(Command("ended"), Command("running"))
                self.assertCountEqual(
                    mock_killpg.call_args_list, [call(ended.pid, signal.SIGTERM), call(running.pid, signal.SIGTERM)]
                )

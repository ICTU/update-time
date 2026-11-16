"""Run processes."""

import os
import shutil
import subprocess  # nosec
from contextlib import suppress
from functools import cache
from pathlib import Path
from signal import Signals
from typing import TYPE_CHECKING

from update_time.io.log import get_logger
from update_time.primitives.command import Command, Result

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOG = get_logger("process")


def is_installed(executable: str) -> bool:
    """Return whether the executable is on the path."""
    return shutil.which(executable) is not None


def run(command: Command, cwd: Path | None = None, environment: Mapping[str, str] | None = None) -> Result:
    """Run a command with the variables added to its environment, and return its result.

    Log a failure, and the command and its tool's version at debug level.
    """
    variables = environment or {}
    if _LOG.logs_debug():
        directory = Path.cwd() if cwd is None else cwd
        _log_version(command.executable, directory)
        _LOG.command_run(command, directory, variables)
    outcome = run_unlogged(command, cwd, variables)
    if isinstance(outcome, OSError):
        _report_unrunnable(command, outcome)
        return Result("", "", succeeded=False)
    if not outcome.ok and outcome.stderr:
        _LOG.command_stderr(command, outcome.stderr)
    return outcome


def _report_unrunnable(command: Command, error: OSError) -> None:
    """Report the error that kept the system from running the command."""
    if isinstance(error, FileNotFoundError):
        _LOG.command_not_found(command)
    else:
        _LOG.command_error(command, error)


@cache
def _log_version(executable: str, directory: Path) -> None:
    """Log the version of the executable in the directory, where it reports one.

    Where the system cannot run the tool, the command it runs for reports that.
    """
    if isinstance(version := _tool_version(executable, directory), str):
        _LOG.tool_version(executable, directory, version)


def tool_version(executable: str, directory: Path) -> str | None:
    """Return the version the executable reports in the directory, or report why it is unknown and return None."""
    if isinstance(outcome := _tool_version(executable, directory), OSError):
        _report_unrunnable(Command(executable, "--version"), outcome)
        return None
    return outcome


@cache
def _tool_version(executable: str, directory: Path) -> str | OSError | None:
    """Return the version the executable reports, the error keeping the system from running it, or else None.

    Warn why the version is unknown where the executable runs. The query runs without colour, which a tool such as
    yarn prints into a pipe as well, so the version and the reason why it is unknown are plain text.
    """
    outcome = run_unlogged(Command(executable, "--version"), directory, {"NO_COLOR": "1"})
    if isinstance(outcome, OSError):
        return outcome
    if not outcome.succeeded:
        reason = outcome.error or "it failed without saying why"
    elif version := outcome.stdout.strip():
        return version
    else:
        reason = "it did not print a version"
    _LOG.tool_version_unknown(executable, directory, reason)
    return None


def run_unlogged(command: Command, cwd: Path | None, environment: Mapping[str, str] | None = None) -> Result | OSError:
    """Run a command with the variables added to its environment, and return its result or the error stopping it."""
    env = os.environ | dict(environment) if environment else None
    try:
        completed = subprocess.run(  # noqa: S603 # nosec
            command, capture_output=True, text=True, check=True, cwd=cwd, env=env
        )
    except OSError as error:
        return error
    except subprocess.CalledProcessError as error:
        return Result(error.stdout, error.stderr, succeeded=False)
    return Result(completed.stdout, completed.stderr, succeeded=True)


class Processes:
    """The processes a run started, which a signal can stop all at once."""

    def __init__(self) -> None:
        """Start with no processes."""
        self._running: set[subprocess.Popen] = set()
        self.stop_signal: Signals | None = None

    def run(self, *commands: Command) -> list[int]:
        """Run the commands at the same time and return their exit codes, that of SIGTERM for one kept from starting."""
        started = [self._start(command) for command in commands]
        return [self._wait(process) for process in started]

    def _start(self, command: Command) -> subprocess.Popen | None:
        """Start the command in a process group of its own, unless a signal stopped the run."""
        if self.stop_signal is not None:
            return None
        process = subprocess.Popen(command, process_group=0)  # noqa: S603 # nosec: B603
        self._running.add(process)
        if self.stop_signal is not None:  # The signal arrived while the process started, so stop did not see it
            _stop_group(process)
        return process

    def _wait(self, process: subprocess.Popen | None) -> int:
        """Wait for the process to end and return its exit code, or that of SIGTERM for one that never started."""
        if process is None:
            return -Signals.SIGTERM
        try:
            return process.wait()
        finally:
            self._running.discard(process)

    def stop(self, signal: Signals) -> None:
        """Send SIGTERM to the process group of every process still running, and start none after."""
        self.stop_signal = signal
        for process in self._running:
            _stop_group(process)


def _stop_group(process: subprocess.Popen) -> None:
    """Send SIGTERM to the process group the process leads."""
    # A process group that is gone belongs to a process that ended by itself, which is what stopping wants. macOS
    # refuses to signal the group of a process that ended but was not waited for yet, rather than reporting it gone.
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, Signals.SIGTERM)

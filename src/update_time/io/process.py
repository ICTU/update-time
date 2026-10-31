"""Run processes."""

import os
import subprocess  # nosec
from contextlib import suppress
from functools import cache
from pathlib import Path
from signal import Signals

from update_time.io.log import get_logger
from update_time.primitives.command import Command, Result

_LOG = get_logger("process")


def run(command: Command, cwd: Path | None = None) -> Result:
    """Run a command and return its result, logging a failure, and the executable's version when the log shows debug."""
    if _LOG.logs_debug():
        _log_version(command.executable, Path.cwd() if cwd is None else cwd)
    outcome = run_unlogged(command, cwd)
    if isinstance(outcome, FileNotFoundError):
        _LOG.command_not_found(command)
        return Result("", "", succeeded=False)
    if isinstance(outcome, OSError):
        _LOG.command_error(command, outcome)
        return Result("", "", succeeded=False)
    if not outcome.ok and outcome.stderr:
        _LOG.command_stderr(command, outcome.stderr)
    return outcome


@cache
def _log_version(executable: str, directory: Path) -> None:
    """Log the version of the executable in the directory, or warn why it is unknown."""
    outcome = run_unlogged(Command(executable, "--version"), directory)
    if isinstance(outcome, OSError):
        return  # The command's own run reports a tool the system cannot run
    if not outcome.succeeded:
        reason = outcome.stderr or outcome.stdout.strip() or "it failed without saying why"
    elif version := outcome.stdout.strip():
        _LOG.tool_version(executable, directory, version)
        return
    else:
        reason = "it did not print a version"
    _LOG.tool_version_unknown(executable, directory, reason)


def run_unlogged(command: Command, cwd: Path | None) -> Result | OSError:
    """Run a command and return its result, or the error that kept the system from running it."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True, cwd=cwd)  # noqa: S603 # nosec
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

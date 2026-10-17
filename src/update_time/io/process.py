"""Run processes."""

import os
import subprocess  # nosec
import threading
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
    outcome = _execute(command, cwd)
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
    outcome = _execute(Command(executable, "--version"), directory)
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


def _execute(command: Command, cwd: Path | None) -> Result | OSError:
    """Run a command and return its result, or the error that kept the system from running it."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True, cwd=cwd)  # noqa: S603 # nosec
    except OSError as error:
        return error
    except subprocess.CalledProcessError as error:
        return Result(error.stdout, error.stderr, succeeded=False)
    return Result(completed.stdout, completed.stderr, succeeded=True)


class Processes:
    """The processes a run started, which the run can stop all at once."""

    def __init__(self) -> None:
        """Start with no processes."""
        self._lock = threading.RLock()  # Re-entrant, so stop cannot deadlock a thread that already holds the lock
        self._running: set[subprocess.Popen] = set()
        self._stopping = False

    def run(self, command: Command) -> int:
        """Run the command and return its exit code, or the exit code of SIGTERM once the run stops."""
        with self._lock:
            if self._stopping:
                return -Signals.SIGTERM
            process = subprocess.Popen(command, process_group=0)  # noqa: S603 # nosec: B603
            self._running.add(process)
        try:
            return process.wait()
        finally:
            with self._lock:
                self._running.discard(process)

    @property
    def stopping(self) -> bool:
        """Return whether the run is stopping its processes."""
        return self._stopping

    def stop(self) -> None:
        """Send SIGTERM to the process group of every process still running, and start none after."""
        with self._lock:
            self._stopping = True
            for process in self._running:
                # A process group that is gone belongs to a process that ended by itself, which is what stopping wants
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, Signals.SIGTERM)

"""Run processes."""

import subprocess  # nosec
from dataclasses import dataclass
from json import loads
from typing import TYPE_CHECKING

from update_time.io.log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

LOG = get_logger("process")


@dataclass(frozen=True)
class Result:
    """The outcome of running a command: its captured output and whether it produced something usable."""

    stdout: str
    stderr: str
    succeeded: bool  # Whether the process exited with status 0 (and its executable was found).

    @property
    def json(self) -> dict | list:
        """Return the stdout parsed as JSON, or an empty dict when the command produced no output."""
        return loads(self.stdout) if self.stdout.strip() else {}

    @property
    def ok(self) -> bool:
        """Return whether the command produced a usable result.

        True when it exited cleanly, or produced output despite a non-zero exit — some tools use a non-zero exit as
        a normal signal (e.g. `npm outdated` exits non-zero when packages are outdated). A command that both failed
        and produced nothing (a genuine failure, such as an unreachable registry) is not ok, and `run` surfaces its
        stderr; a caller can check this to skip follow-up work the failure would make pointless.
        """
        return self.succeeded or bool(self.stdout.strip())


def run(command: list[str], cwd: Path | None = None) -> Result:
    """Run a command and return its result, logging stderr as a warning when it produced nothing usable.

    A missing executable is logged and reported as a failed, empty result so the rest of the run continues instead
    of crashing with a traceback. stderr is surfaced only for a genuine failure (see `Result.ok`), which keeps
    routine tool chatter — a non-zero `npm outdated` exit, a pnpm deprecation `[WARN]` alongside real output — out of
    the log.
    """
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True, cwd=cwd)  # noqa: S603 # nosec
    except FileNotFoundError:
        LOG.command_not_found(command)
        return Result("", "", succeeded=False)
    except subprocess.CalledProcessError as error:
        result = Result(error.stdout, error.stderr, succeeded=False)
    else:
        result = Result(completed.stdout, completed.stderr, succeeded=True)
    if not result.ok and result.stderr:
        LOG.command_stderr(command, result.stderr)
    return result

"""Run processes."""

import subprocess  # nosec
from typing import TYPE_CHECKING

from update_time.io.log import get_logger
from update_time.primitives.command import Result

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.primitives.command import Command

_LOG = get_logger("process")


def run(command: Command, cwd: Path | None = None) -> Result:
    """Run a command and return its result, logging stderr as a warning when it produced nothing usable.

    A missing executable is logged and reported as a failed, empty result so the rest of the run continues instead
    of crashing with a traceback. stderr is surfaced only for a genuine failure (see `Result.ok`), which keeps
    routine tool chatter — a non-zero `npm outdated` exit, a pnpm deprecation `[WARN]` alongside real output — out of
    the log.
    """
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True, cwd=cwd)  # noqa: S603 # nosec
    except FileNotFoundError:
        _LOG.command_not_found(command)
        return Result("", "", succeeded=False)
    except subprocess.CalledProcessError as error:
        result = Result(error.stdout, error.stderr, succeeded=False)
    else:
        result = Result(completed.stdout, completed.stderr, succeeded=True)
    if not result.ok and result.stderr:
        _LOG.command_stderr(command, result.stderr)
    return result

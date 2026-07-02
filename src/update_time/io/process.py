"""Run processes."""

import json
import subprocess  # nosec
from typing import TYPE_CHECKING

from update_time.io.log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

LOG = get_logger("process")


def _run(command: list[str], cwd: Path | None) -> tuple[str, str, bool]:
    """Run the command and return its (stdout, stderr, succeeded).

    A missing executable (the tool isn't installed) is logged and reported as a failure with empty output, so the
    rest of the run continues instead of crashing with a traceback.
    """
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True, cwd=cwd)  # noqa: S603 # nosec
    except FileNotFoundError:
        LOG.command_not_found(command)
        return "", "", False
    except subprocess.CalledProcessError as error:
        return error.stdout, error.stderr, False
    return completed.stdout, completed.stderr, True


def run(command: list[str], cwd: Path | None = None) -> str:
    """Run the process and return its stdout, logging any stderr as a warning when the command fails.

    For action commands (e.g. `npm update`, `uv lock`) whose non-zero exit means the action didn't complete: their
    stderr is logged (at warning level) rather than swallowed, while the stdout is still returned so the caller can
    decide. Commands whose non-zero exit is a normal signal rather than a failure (e.g. `npm outdated`, which exits
    non-zero when packages are outdated) should use `run_json` instead.
    """
    stdout, stderr, succeeded = _run(command, cwd)
    if not succeeded and stderr:
        LOG.command_stderr(command, stderr)
    return stdout


def run_json(command: list[str], cwd: Path | None = None) -> dict | list:
    """Run a command that emits JSON and return the parsed value, or an empty dict when it produced no output.

    These commands (e.g. `npm outdated`, `pnpm outdated`) use a non-zero exit code as a normal signal (packages are
    outdated), so a non-zero exit isn't treated as a failure as long as the command still produced parseable output.
    The stderr is logged as a warning only when the command produced nothing usable (a genuine failure); this keeps
    routine tool chatter — such as a pnpm deprecation `[WARN]` printed alongside an ordinary `outdated` result — out
    of the log.
    """
    stdout, stderr, succeeded = _run(command, cwd)
    if stdout.strip():
        return json.loads(stdout)
    if not succeeded and stderr:
        LOG.command_stderr(command, stderr)
    return {}

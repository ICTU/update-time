"""Run processes."""

import subprocess  # nosec
from typing import TYPE_CHECKING

from update_time.io.log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

LOG = get_logger("process")


def run(command: list[str], cwd: Path | None = None) -> str:
    """Run the process and return stdout.

    The updater commands (npm, uv) use a non-zero exit code for normal outcomes (e.g. `npm outdated` exits 1 when
    packages are outdated), so a non-zero exit isn't treated as a failure and its stdout is still returned. They are
    run with `--silent`/`--quiet` though, so anything written to stderr signals a genuine problem and is logged
    rather than silently swallowed. A missing executable (the tool isn't installed) is logged and an empty result
    returned, so the rest of the run continues instead of crashing with a traceback.
    """
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True, cwd=cwd)  # noqa: S603 # nosec
    except FileNotFoundError:
        LOG.command_not_found(command)
        return ""
    except subprocess.CalledProcessError as error:
        if error.stderr:
            LOG.command_failed(command, error.stderr)
        return error.stdout
    return completed.stdout

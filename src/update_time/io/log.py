"""Log helpers."""

import logging
import os
import sys
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

from rich.logging import RichHandler

# The log levels that can be selected on the command line, and the default. Reporting an available new version is
# logged at INFO (it is the tool's regular output, not a problem), while genuinely unexpected situations stay at
# WARNING and failures at ERROR. The per-file "checking ..." progress is logged at DEBUG.
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_LOG_LEVEL = "INFO"
# Private channel that passes the log level from the CLI to the updater subprocesses; not a user-facing setting
# (use --log-level instead). The leading underscore marks it internal.
LOG_LEVEL_ENV_VAR = "_UPDATE_TIME_LOG_LEVEL"


def log_level() -> str:
    """Return the configured log level."""
    return os.environ.get(LOG_LEVEL_ENV_VAR, DEFAULT_LOG_LEVEL)


if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

    from requests import Response

    from update_time.domain.version import DependencyVersion

# Files that wrap or dispatch logging on behalf of the updaters. Frames in these files are skipped when
# determining a log record's origin, so the reported origin is the updater that triggered the log rather
# than this wrapper or the generic file-update engine in filesystem.py.
_HELPER_FILES = frozenset(str(path.resolve()) for path in (Path(__file__), Path(__file__).with_name("filesystem.py")))


def _caller_stacklevel() -> int:
    """Return the stacklevel of the first frame outside the logging and filesystem helpers.

    A fixed stacklevel can't work because some log methods are called directly by an updater while others
    are dispatched through filesystem.py (with extra comprehension frames in between), so walk the stack to
    find the originating updater frame instead.
    """
    level = 1  # Start at the frame that emits the record (Logger._log) and skip helper frames from there.
    try:
        frame: FrameType | None = sys._getframe(level)  # noqa: SLF001
    except ValueError:  # pragma: no cover
        return level
    while frame is not None and str(Path(frame.f_code.co_filename).resolve()) in _HELPER_FILES:
        level += 1
        frame = frame.f_back
    return level


class Logger:
    """Wrap a logger and add update specific log methods."""

    def __init__(self, name: str) -> None:
        """Initialize the logger."""
        self.log = logging.getLogger(name)
        self.logged_changes: set[tuple[str, DependencyVersion]] = set()

    def _log(self, log_method: Callable[..., None], msg: str, *args: object) -> None:
        """Emit a log record, attributing it to the updater that triggered it rather than a helper."""
        log_method(msg, *args, stacklevel=_caller_stacklevel())

    def new_version(self, dependency: str, version: DependencyVersion, path: Path) -> None:
        """Log the availability of a new version for a dependency in a file, with its UTC publication date if known."""
        if (dependency, version) in self.logged_changes:
            changes = "Suppressing changelog already shown, see above"
        else:
            changes = version.changes or "No changelog available!"
        self.logged_changes.add((dependency, version))
        new_version = version.version
        if version.published is not None:
            new_version += f", published: {version.published.astimezone(UTC):%Y-%m-%d %H:%M}"
        relative_path = path.relative_to(Path.cwd())
        self._log(
            self.log.info, "New version available for %s in %s: %s\n%s", dependency, relative_path, new_version, changes
        )

    def pinned(self, dependency: str, version: DependencyVersion, path: Path) -> None:
        """Log that a previously unpinned reference in a file was pinned to a digest, without changing its version."""
        relative_path = path.relative_to(Path.cwd())
        self._log(self.log.info, "Pinned %s in %s to %s@%s", dependency, relative_path, version.version, version.sha)

    def no_version(self, dependency: str) -> None:
        """Log no version found."""
        self._log(self.log.error, "No valid version found for %s", dependency)

    def no_commit_sha(self, dependency: str, version: str, url: str) -> None:
        """Log that no commit SHA could be fetched for an otherwise-eligible release."""
        self._log(self.log.error, "Could not fetch commit SHA for %s %s: %s", dependency, version, url)

    def no_integrity_hash(self, dependency: str, version: str, filename: str) -> None:
        """Warn that a jsDelivr file's integrity hash couldn't be resolved, so the reference is left unchanged."""
        message = "Could not resolve the integrity hash for %s %s (%s), leaving it unchanged"
        self._log(self.log.warning, message, dependency, version, filename)

    def path(self, path: Path) -> None:
        """Log working on path."""
        self._log(self.log.debug, "Checking if there are updates for %s", path.relative_to(Path.cwd()))

    def ignored(self, dependency: str, path: Path) -> None:
        """Log that a reference was left unchanged because of an `# update-time: ignore` marker."""
        message = "Ignoring updates for %s in %s (update-time: ignore)"
        self._log(self.log.debug, message, dependency, path.relative_to(Path.cwd()))

    def skipped(self, path: Path, reason: str) -> None:
        """Log that a file was deliberately skipped without being checked for updates."""
        self._log(self.log.info, "Skipping %s: %s", path.relative_to(Path.cwd()), reason)

    def unsupported_package_manager(self, path: Path, manager: str, supported: str) -> None:
        """Warn that a file is managed by an unsupported package manager, so its dependencies are left unchanged."""
        message = "Skipping %s: %s is not supported, only %s"
        self._log(self.log.warning, message, path.relative_to(Path.cwd()), manager, supported)

    def expected_node_base_image(self, dockerfile: Path) -> None:
        """Log missing Node base image."""
        self._log(self.log.error, "Expected Dockerfile %s to have a Node base image", dockerfile)

    def non_numeric_node_base_image(self, dockerfile: Path, tag: str) -> None:
        """Log that the Node base image tag is not a concrete version, so the Node engine can't be derived."""
        self._log(
            self.log.warning,
            "Cannot derive the Node engine version from the non-numeric base image tag 'node:%s' in %s",
            tag,
            dockerfile,
        )

    def response(self, response: Response) -> None:
        """Log a response's status code."""
        self._log(self.log.warning, "Could not fetch %s: %s", response.url, response.status_code)

    def timeout(self, url: str) -> None:
        """Log a request timeout."""
        self._log(self.log.warning, "Timeout while fetching %s", url)

    def request_error(self, url: str, error: object) -> None:
        """Log a network error (connection failure, too many redirects, ...) while fetching a URL."""
        self._log(self.log.warning, "Could not fetch %s: %s", url, error)

    def command_not_found(self, command: list[str]) -> None:
        """Log that a command could not be run because its executable is not installed."""
        self._log(self.log.error, "Could not run %s: is %s installed?", " ".join(command), command[0])

    def command_stderr(self, command: list[str], stderr: str) -> None:
        """Log, at warning level, that a command wrote to stderr, including what it wrote.

        The message stays neutral about severity because the tool decides that: its stderr may be an `[ERROR]`, a
        `[WARN]`, or just a notice (e.g. a pnpm deprecation). The warning level is Update-time's own view — this is
        only logged when the command failed or produced nothing usable (see `run`/`run_json`), so it is worth
        surfacing whatever the tool called it.
        """
        self._log(self.log.warning, "%s wrote to stderr:\n%s", " ".join(command), stderr.rstrip())


def get_logger(name: str) -> Logger:
    """Initialize a logger."""
    logging.basicConfig(level=log_level(), datefmt="[%X]", format="%(message)s", handlers=[RichHandler()])
    return Logger(name)

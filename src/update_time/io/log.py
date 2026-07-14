"""Log helpers."""

import logging
import os
import re
import sys
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.highlighter import ReprHighlighter
from rich.logging import RichHandler
from rich.theme import Theme

from update_time.domain.marker import Marker
from update_time.domain.staleness import is_stale, stale_after_days, staleness_days

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

    from requests import Response
    from rich.text import Text

    from update_time.domain.version import DependencyVersion, VersionFilter, VersionString


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


# Private-use-area character that brackets a dependency name in a log message, so the highlighter can style the name
# without having to recognise it by shape. A dependency name has no fixed form (`humanize`, `actions/checkout`,
# `ghcr.io/astral-sh/uv`, …), so unlike a `sha256:` digest it can't be matched by a pattern; the marker identifies
# it unambiguously instead. It is a Private Use Area code point that never occurs in real content, and Rich does not
# strip it (it strips only a handful of C0 control codes), so it survives message formatting until the highlighter
# removes it. `Logger` wraps each name in it; `LogHighlighter` styles the wrapped run and strips the markers.
_DEPENDENCY_MARKER = ""


class LogHighlighter(ReprHighlighter):
    """Rich highlighter that colours a whole `sha256:` digest, and each marked dependency name, as a single token.

    Rich's built-in rules otherwise match only fragments of a digest — the `256` reads as a number and a run such
    as `a256:a4fd` reads as an IPv6 address — colouring parts of it and leaving the rest plain. Matching the full
    digest and dropping the built-in sub-spans inside it styles the whole digest uniformly (as `repr.digest`), while
    every other message keeps Rich's default highlighting of version numbers, paths, and the like.

    A dependency name can't be matched by shape, so `Logger` brackets it with `_DEPENDENCY_MARKER`; each bracketed
    run is styled (as `repr.dependency`), and the markers are stripped, so only the colouring reaches the output.
    """

    _DIGEST = re.compile(r"\bsha256:[0-9a-f]{64}\b")
    _DEPENDENCY = re.compile(f"{_DEPENDENCY_MARKER}([^{_DEPENDENCY_MARKER}]*){_DEPENDENCY_MARKER}")

    def highlight(self, text: Text) -> None:
        """Apply the default highlighting, restyle each digest as one token, then style and unwrap dependency names."""
        super().highlight(text)
        for match in self._DIGEST.finditer(text.plain):
            start, end = match.span()
            text.spans[:] = [span for span in text.spans if span.end <= start or span.start >= end]
            text.stylize("repr.digest", start, end)
        self._highlight_dependencies(text)

    def _highlight_dependencies(self, text: Text) -> None:
        """Style each marker-bracketed dependency name as `repr.dependency` and remove the markers from the text.

        The name is rebuilt from slices of the original text (rather than matched by a pattern) so its existing
        highlighting is preserved and Rich remaps the surrounding spans across the removed markers automatically.
        """
        matches = list(self._DEPENDENCY.finditer(text.plain))
        if not matches:
            return
        result = text[: matches[0].start()]
        for index, match in enumerate(matches):
            name = text[match.start() + 1 : match.end() - 1]  # the name itself, without its two markers
            name.stylize("repr.dependency")
            result += name
            following = matches[index + 1].start() if index + 1 < len(matches) else len(text.plain)
            result += text[match.end() : following]
        text.plain = result.plain
        text.spans = result.spans


# Files that wrap or dispatch logging on behalf of the updaters. Frames in these files are skipped when
# determining a log record's origin, so the reported origin is the updater that triggered the log rather than this
# wrapper or the generic file-finding (filesystem.py) and reference-rewriting (rewrite.py) engines.
_HELPER_FILES = frozenset(
    str(path.resolve())
    for path in (Path(__file__), Path(__file__).with_name("filesystem.py"), Path(__file__).with_name("rewrite.py"))
)


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

    @staticmethod
    def _log(log_method: Callable[..., None], msg: str, *args: object) -> None:
        """Emit a log record, attributing it to the updater that triggered it rather than a helper."""
        log_method(msg, *args, stacklevel=_caller_stacklevel())

    @staticmethod
    def _relative(path: Path) -> Path:
        """Render a path relative to the working directory, or its absolute self when it sits outside it.

        Most logged paths are files under the scan root (which is the working directory), but some — such as a uv
        workspace root above the current member — are not, so fall back to the absolute path rather than raising.
        """
        try:
            return path.relative_to(Path.cwd())
        except ValueError:
            return path

    # --- Run gating ---

    _MESSAGE_FORCED_OUTSIDE_GIT_REPOSITORY = (
        "Running outside a git repository (%s) because --force was given; changes are made in place and cannot be "
        "reverted"
    )

    def forced_outside_git_repository(self, path: Path) -> None:
        """Warn that Update-time is running outside the git repository at the path because --force overrode the refusal.

        The path is the scan root, which is the working directory, so it is logged as its absolute self (making it
        relative would collapse it to `.`).
        """
        self._log(self.log.warning, self._MESSAGE_FORCED_OUTSIDE_GIT_REPOSITORY, path)

    # --- Source results: resolving a dependency's latest version and digest ---

    MESSAGE_NEW_VERSION = f"New version available for {_DEPENDENCY_MARKER}%s{_DEPENDENCY_MARKER} in %s: %s\n%s"
    _SUPPRESSING_CHANGELOG = "Suppressing changelog already shown, see above"
    NO_CHANGELOG = "No changelog available!"

    def new_version(self, dependency: str, version: DependencyVersion, path: Path) -> None:
        """Log the availability of a new version for a dependency in a file, with its UTC publication date if known."""
        if (dependency, version) in self.logged_changes:
            changes = self._SUPPRESSING_CHANGELOG
        else:
            changes = version.changes or self.NO_CHANGELOG
        self.logged_changes.add((dependency, version))
        new_version = version.version
        if version.published is not None:
            new_version += f", published: {version.published.astimezone(UTC):%Y-%m-%d %H:%M}"
        self._log(self.log.info, self.MESSAGE_NEW_VERSION, dependency, self._relative(path), new_version, changes)

    _MESSAGE_PINNED = f"Pinned {_DEPENDENCY_MARKER}%s{_DEPENDENCY_MARKER} in %s to %s@%s"

    def pinned(self, dependency: str, version: DependencyVersion, path: Path) -> None:
        """Log that a previously unpinned reference in a file was pinned to a digest, without changing its version."""
        self._log(self.log.info, self._MESSAGE_PINNED, dependency, self._relative(path), version.version, version.sha)

    _MESSAGE_DIGEST_DRIFT = (
        f"Digest drift for {_DEPENDENCY_MARKER}%s{_DEPENDENCY_MARKER}:%s in %s: pinned to %s but the registry "
        "now serves %s; the pin was left unchanged, verify the change is expected before updating the pin"
    )

    def digest_drift(self, dependency: str, version: str, current_sha: str, new_sha: str, path: Path) -> None:
        """Warn that an already-pinned tag now resolves to a different digest, and that the pin was left unchanged.

        The tag was re-pushed (rebuilt) under the same name, so its pinned digest no longer matches what the registry
        serves. Update-time deliberately does not update the pin: silently adopting a re-pushed digest would defeat
        the immutability a digest pin exists to provide. The drift is surfaced instead, so it can be reviewed.
        """
        message = self._MESSAGE_DIGEST_DRIFT
        self._log(self.log.warning, message, dependency, version, self._relative(path), current_sha, new_sha)

    _MESSAGE_ADOPTED_DIGEST_DRIFT = (
        f"Adopted digest drift for {_DEPENDENCY_MARKER}%s{_DEPENDENCY_MARKER}:%s in %s: re-pinned from %s to %s (%s)"
    )

    def adopted_drift(  # noqa: PLR0913
        self, dependency: str, version: str, current_sha: str, new_sha: str, path: Path, cause: str
    ) -> None:
        """Log, at info level, that a re-pushed tag's new digest was adopted because the reference opted in.

        `cause` names the opt-in that triggered the adoption (the reference's `# update-time: allow[digest-drift]`
        marker, or the repo-wide `--allow-image-digest-drift` flag). Unlike `digest_drift`, this is a normal change the
        user asked for, so it is info, not a warning.
        """
        message = self._MESSAGE_ADOPTED_DIGEST_DRIFT
        self._log(self.log.info, message, dependency, version, self._relative(path), current_sha, new_sha, cause)

    _MESSAGE_STALE = (
        f"Stale dependency {_DEPENDENCY_MARKER}%s{_DEPENDENCY_MARKER} in %s: "
        "newest release %s was published %d days ago (> %d)"
    )

    def warn_if_stale(self, dependency: str, version: DependencyVersion, path: Path) -> None:
        """Warn if the dependency's newest release is old enough that the project may have gone quiet.

        Does nothing when the newest release date is unknown or within the threshold (or the check is disabled),
        so callers can hand off every resolved version unconditionally.
        """
        if (published := version.newest_published) is None or not is_stale(published):
            return
        message = self._MESSAGE_STALE
        days_ago, threshold = staleness_days(published), stale_after_days()
        self._log(self.log.warning, message, dependency, self._relative(path), version.version, days_ago, threshold)

    _MESSAGE_NO_VERSION = f"No valid version found for {_DEPENDENCY_MARKER}%s{_DEPENDENCY_MARKER}"

    def no_version(self, dependency: str) -> None:
        """Log no version found."""
        self._log(self.log.error, self._MESSAGE_NO_VERSION, dependency)

    _MESSAGE_NO_COMMIT_SHA = f"Could not fetch commit SHA for {_DEPENDENCY_MARKER}%s{_DEPENDENCY_MARKER} %s: %s"

    def no_commit_sha(self, dependency: str, version: str, url: str) -> None:
        """Log that no commit SHA could be fetched for an otherwise-eligible release."""
        self._log(self.log.error, self._MESSAGE_NO_COMMIT_SHA, dependency, version, url)

    _MESSAGE_NO_INTEGRITY_HASH = (
        f"Could not resolve the integrity hash for {_DEPENDENCY_MARKER}%s{_DEPENDENCY_MARKER} %s (%s), "
        "leaving it unchanged"
    )

    def no_integrity_hash(self, dependency: str, version: str, filename: str) -> None:
        """Warn that a jsDelivr file's integrity hash couldn't be resolved, so the reference is left unchanged."""
        self._log(self.log.warning, self._MESSAGE_NO_INTEGRITY_HASH, dependency, version, filename)

    _MESSAGE_INVALID_SPECIFIER = (
        f"Invalid %r in the update-time marker for {_DEPENDENCY_MARKER}%s{_DEPENDENCY_MARKER} "
        "in %s; leaving the reference unchanged"
    )

    def invalid_specifier(self, dependency: str, specifier: str, path: Path) -> None:
        """Warn that a marker carried an invalid version specifier or item, so the reference is left unchanged."""
        self._log(self.log.warning, self._MESSAGE_INVALID_SPECIFIER, specifier, dependency, self._relative(path))

    _MESSAGE_REDUNDANT_BOUND = (
        f"Redundant update bound %s on {_DEPENDENCY_MARKER}%s{_DEPENDENCY_MARKER} %s in %s: it %s"
    )

    def warn_if_redundant_bound(
        self, dependency: str, version_filter: VersionFilter, current_version: VersionString, path: Path
    ) -> None:
        """Warn when a version bound is redundant for the current version (never has an effect, or blocks everything).

        Does nothing when the reference has no bound (the keep-all `NO_BOUND`) or the bound is live (see
        `VersionFilter.redundancy`), so callers can hand off every reference unconditionally. The bound is rendered
        in its marker form (`allow[update<3.13]`, with the specifier in PEP 440's normalised clause order), so the
        warning shows which bound on which pin is redundant.
        """
        if (redundancy := version_filter.redundancy(current_version)) is None:
            return
        bound = str(Marker(version_filter=version_filter))  # Rendered in its marker form, e.g. `allow[update<3.13]`.
        message = self._MESSAGE_REDUNDANT_BOUND
        arguments = (bound, dependency, current_version, self._relative(path), redundancy.value)
        self._log(self.log.warning, message, *arguments)

    # --- File scanning and selection ---

    _MESSAGE_CHECKING_PATH = "Checking if there are updates for %s"

    def path(self, path: Path) -> None:
        """Log working on path."""
        self._log(self.log.debug, self._MESSAGE_CHECKING_PATH, self._relative(path))

    _MESSAGE_APPLYING_MARKER = f"Applying update-time marker %s to {_DEPENDENCY_MARKER}%s{_DEPENDENCY_MARKER} in %s"

    def applying_marker(self, dependency: str, marker: Marker, path: Path) -> None:
        """Log, at debug level, the marker applying to a reference, so users can confirm it is recognised.

        Does nothing when the line carries no marker (the rendered directive list is empty), so the caller can hand
        off every reference unconditionally.
        """
        if not (directives := str(marker)):
            return
        self._log(self.log.debug, self._MESSAGE_APPLYING_MARKER, directives, dependency, self._relative(path))

    _MESSAGE_IGNORED = f"Ignoring updates for {_DEPENDENCY_MARKER}%s{_DEPENDENCY_MARKER} in %s (update-time: %s)"

    def ignored(self, dependency: str, marker: Marker, path: Path) -> None:
        """Log that a reference's update was held back by the marker's `ignore` directive, naming its form."""
        directive = "ignore" if marker.ignore_update and marker.ignore_stale else "ignore[update]"
        self._log(self.log.debug, self._MESSAGE_IGNORED, dependency, self._relative(path), directive)

    _MESSAGE_EXCLUDING_PATH = "Excluding %s from the scan (--exclude-path)"

    def excluded_path(self, path: Path) -> None:
        """Log that a directory passed to `--exclude-path` is held back from the scan."""
        self._log(self.log.debug, self._MESSAGE_EXCLUDING_PATH, path)

    _MESSAGE_PATH_TO_EXCLUDE_DOES_NOT_EXIST = "Path %s passed to --exclude-path does not exist"

    def missing_excluded_path(self, path: Path) -> None:
        """Warn that a directory passed to `--exclude-path` does not exist, so it excludes nothing."""
        self._log(self.log.warning, self._MESSAGE_PATH_TO_EXCLUDE_DOES_NOT_EXIST, path)

    _MESSAGE_SKIP_PATH = "Skipping %s: %s"

    def skipped(self, path: Path, reason: str) -> None:
        """Log that a file was deliberately skipped without being checked for updates."""
        self._log(self.log.info, self._MESSAGE_SKIP_PATH, self._relative(path), reason)

    # --- Updater-specific diagnostics ---

    _MESSAGE_UV_COOLDOWN = "Set uv exclude-newer to %r in %s to apply the cooldown"

    def configured_uv_cooldown(self, path: Path, cooldown: str) -> None:
        """Log that Update-time wrote its cooldown into the project's uv configuration.

        The path is a workspace root, which can sit above the current directory (when Update-time runs inside a
        member), so fall back to the absolute path when it can't be made relative to the working directory.
        """
        self._log(self.log.info, self._MESSAGE_UV_COOLDOWN, cooldown, self._relative(path))

    _MESSAGE_SKIP_UNSUPPORTED = "Skipping %s: %s is not supported, only %s"

    def unsupported_package_manager(self, path: Path, manager: str, supported: str) -> None:
        """Warn that a file is managed by an unsupported package manager, so its dependencies are left unchanged."""
        self._log(self.log.warning, self._MESSAGE_SKIP_UNSUPPORTED, self._relative(path), manager, supported)

    _MESSAGE_INVALID_TOML = "Skipping %s: it is not valid TOML"

    def invalid_pyproject_toml(self, path: Path) -> None:
        """Warn that a pyproject.toml can't be parsed as TOML, so it is skipped rather than crashing the run."""
        self._log(self.log.warning, self._MESSAGE_INVALID_TOML, self._relative(path))

    _MESSAGE_MISSING_NODE_BASE_IMAGE = "Expected Dockerfile %s to have a Node base image"

    def expected_node_base_image(self, dockerfile: Path) -> None:
        """Log missing Node base image."""
        self._log(self.log.error, self._MESSAGE_MISSING_NODE_BASE_IMAGE, dockerfile)

    _MESSAGE_NON_NUMERIC_NODE_BASE_IMAGE_TAG = (
        "Cannot derive the Node engine version from the non-numeric base image tag 'node:%s' in %s"
    )

    def non_numeric_node_base_image(self, dockerfile: Path, tag: str) -> None:
        """Log that the Node base image tag is not a concrete version, so the Node engine can't be derived."""
        self._log(self.log.warning, self._MESSAGE_NON_NUMERIC_NODE_BASE_IMAGE_TAG, tag, dockerfile)

    # --- HTTP fetching ---

    _MESSAGE_COULD_NOT_FETCH = "Could not fetch %s: %s"

    def response(self, response: Response) -> None:
        """Log a response's status code."""
        self._log(self.log.warning, self._MESSAGE_COULD_NOT_FETCH, response.url, response.status_code)

    _MESSAGE_TIMEOUT = "Timeout while fetching %s"

    def timeout(self, url: str) -> None:
        """Log a request timeout."""
        self._log(self.log.warning, self._MESSAGE_TIMEOUT, url)

    def request_error(self, url: str, error: object) -> None:
        """Log a network error (connection failure, too many redirects, ...) while fetching a URL."""
        self._log(self.log.warning, self._MESSAGE_COULD_NOT_FETCH, url, error)

    # --- External commands ---

    _MESSAGE_COMMAND_NOT_FOUND = "Could not run %s: is %s installed?"

    def command_not_found(self, command: list[str]) -> None:
        """Log that a command could not be run because its executable is not installed."""
        self._log(self.log.error, self._MESSAGE_COMMAND_NOT_FOUND, " ".join(command), command[0])

    _MESSAGE_COMMAND_STDERR = "%s wrote to stderr:\n%s"

    def command_stderr(self, command: list[str], stderr: str) -> None:
        """Log, at warning level, that a command wrote to stderr, including what it wrote.

        The message stays neutral about severity because the tool decides that: its stderr may be an `[ERROR]`, a
        `[WARN]`, or just a notice (e.g. a pnpm deprecation). The warning level is Update-time's own view — this is
        only logged when the command failed or produced nothing usable (see `run`/`run_json`), so it is worth
        surfacing whatever the tool called it.
        """
        self._log(self.log.warning, self._MESSAGE_COMMAND_STDERR, " ".join(command), stderr.rstrip())


def get_logger(name: str) -> Logger:
    """Initialize a logger, configuring the root logger to send all diagnostics to stderr on the first call.

    Update-time's real output is the files it rewrites in place; everything it logs — the new-version report as much
    as the warnings and errors — is diagnostics about the run, so it all goes to stderr. That keeps stdout clean for
    the argparse-handled `--version`/`--help` output, so e.g. `v=$(update-time -V)` isn't polluted with log lines.
    `get_logger` is called once per module that owns a logger (an updater plus the sources it imports), so configure
    the root logger only the first time — when it has no handlers yet — instead of building a handler on every call.
    """
    if not logging.getLogger().handlers:
        # The theme adds the styles `LogHighlighter` applies: `repr.digest` for a whole `sha256:` digest and
        # `repr.dependency` (bold white) for a dependency name. When colour is off, both render as plain text.
        console = Console(stderr=True, theme=Theme({"repr.digest": "dim", "repr.dependency": "bold white"}))
        handler = RichHandler(console=console, highlighter=LogHighlighter())
        logging.basicConfig(level=log_level(), datefmt="[%X]", format="%(message)s", handlers=[handler])
    return Logger(name)

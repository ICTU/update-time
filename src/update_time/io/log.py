"""Log helpers."""

import logging
import re
import sys
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.highlighter import ReprHighlighter
from rich.logging import RichHandler
from rich.theme import Theme

from update_time.domain.bound import NO_BOUND, Verb
from update_time.domain.location import Location
from update_time.domain.staleness import STALE_AFTER, is_stale, staleness_days
from update_time.domain.version import SHA256_DIGEST
from update_time.primitives.environment import EnvVar

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

    from requests import Response
    from rich.text import Text

    from update_time.domain.marker import Marker
    from update_time.domain.version import DependencyVersion, VersionString


# The log levels that can be selected on the command line, and the default. Reporting an available new version is
# logged at INFO (it is the tool's regular output, not a problem), while genuinely unexpected situations stay at
# WARNING and failures at ERROR. The per-file "checking ..." progress is logged at DEBUG.
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# Private channel that passes the log level from the CLI to the updater subprocesses.
LOG_LEVEL = EnvVar("_UPDATE_TIME_LOG_LEVEL", default="INFO", parse=str)


# Private-use-area character that brackets a dependency name in a log message, so the highlighter can style the name
# without having to recognise it by shape. A dependency name has no fixed form (`humanize`, `actions/checkout`,
# `ghcr.io/astral-sh/uv`, …), so unlike a `sha256:` digest it can't be matched by a pattern; the delimiter identifies
# it unambiguously instead. It is a Private Use Area code point that never occurs in real content, and Rich does not
# strip it (it strips only a handful of C0 control codes), so it survives message formatting until the highlighter
# removes it. `Logger` wraps each name in it; `LogHighlighter` styles the wrapped run and strips the delimiters.
DEPENDENCY_DELIMITER = ""

# Private-use-area character that brackets a file location (a `path` or `path:line`) in a log message, so the
# highlighter can colour the whole run — directory, filename, and line number — as one token instead of the
# several fragments Rich's default rules would produce (a `repr.path` prefix, a `repr.filename`, and a
# `repr.number` for the line). It is not matched by shape for the same reason the digest is dropped and the
# dependency name is delimited: the path is known exactly when the message is built, whereas a regex over the
# finished message can't tell it apart from the version numbers and digests around it, and a bare filename such
# as `Dockerfile` carries no path marker to anchor on. A distinct code point from `DEPENDENCY_DELIMITER`, so the
# two runs never collide. `Logger` wraps each location in it; `LogHighlighter` styles the run and strips them.
LOCATION_DELIMITER = ""


class LogHighlighter(ReprHighlighter):
    """Rich highlighter that colours a whole `sha256:` digest, dependency name, and file location as single tokens.

    Rich's built-in rules otherwise match only fragments of a digest — the `256` reads as a number and a run such
    as `a256:a4fd` reads as an IPv6 address — colouring parts of it and leaving the rest plain. Matching the full
    digest and dropping the built-in sub-spans inside it styles the whole digest uniformly (as `repr.digest`), while
    every other message keeps Rich's default highlighting of version numbers and the like.

    A dependency name can't be matched by shape, and a file location can't be told apart from the versions and digests
    around it, so `Logger` brackets each with its own delimiter. A bracketed dependency name is styled (as
    `repr.dependency`) keeping Rich's inner highlighting; a bracketed location is styled (as `repr.filename`) with its
    inner fragments dropped, so the whole `path:line` reads as one unit. Either way the delimiters are stripped, so
    only the colouring reaches the output.
    """

    _DIGEST = re.compile(rf"\b{SHA256_DIGEST}\b")
    _DEPENDENCY = re.compile(f"{DEPENDENCY_DELIMITER}[^{DEPENDENCY_DELIMITER}]*{DEPENDENCY_DELIMITER}")
    _LOCATION = re.compile(f"{LOCATION_DELIMITER}[^{LOCATION_DELIMITER}]*{LOCATION_DELIMITER}")

    def highlight(self, text: Text) -> None:
        """Apply the default highlighting, restyle each digest, then style and unwrap dependency names and locations."""
        super().highlight(text)
        for match in self._DIGEST.finditer(text.plain):
            start, end = match.span()
            text.spans[:] = [span for span in text.spans if span.end <= start or span.start >= end]
            text.stylize("repr.digest", start, end)
        self._restyle_delimited(text, self._DEPENDENCY, "repr.dependency", keep_inner=True)
        self._restyle_delimited(text, self._LOCATION, "repr.filename", keep_inner=False)

    @staticmethod
    def _restyle_delimited(text: Text, pattern: re.Pattern[str], style: str, *, keep_inner: bool) -> None:
        """Style each delimiter-bracketed run as `style` and remove its two delimiters from the text.

        The run is rebuilt from slices of the original text (rather than matched by a pattern) so Rich remaps the
        surrounding spans across the removed delimiters automatically. With `keep_inner`, the run's existing
        highlighting is kept and `style` layered on top (a dependency name keeps Rich's inner colours); without it,
        the inner spans are dropped first so the whole run takes `style` uniformly (a location's `path:line` reads as
        one token rather than a separate path, filename, and number).
        """
        matches = list(pattern.finditer(text.plain))
        if not matches:
            return
        result = text[: matches[0].start()]
        for index, match in enumerate(matches):
            inner = text[match.start() + 1 : match.end() - 1]  # the run itself, without its two delimiters
            if not keep_inner:
                inner.spans.clear()  # Drop Rich's path/filename/number fragments so the run colours uniformly.
            inner.stylize(style)
            result += inner
            following = matches[index + 1].start() if index + 1 < len(matches) else len(text.plain)
            result += text[match.end() : following]
        text.plain = result.plain
        text.spans = result.spans


# The theme adds the styles `LogHighlighter` applies: `repr.digest` for a whole `sha256:` digest and `repr.dependency`
# (bold white) for a dependency name; a file location reuses Rich's built-in `repr.filename`, so it needs no entry.
# When colour is off, all render as plain text. The theme and formats are shared with `docs/generate_log_svg.py`, so
# the README screenshot renders exactly like the real output.
LOG_THEME = Theme({"repr.digest": "dim", "repr.dependency": "bold white"})
LOG_TIME_FORMAT = "[%X]"
LOG_MESSAGE_FORMAT = "%(message)s"


# This wrapper, and the packages that log on behalf of the updaters (see `attribute_logs_to_caller`). Frames in
# these are skipped when determining a log record's origin, so the reported origin is the updater that triggered the
# log rather than this wrapper or the shared machinery in between.
_wrapper_file = Path(__file__).resolve()
_helper_packages: set[Path] = set()


def attribute_logs_to_caller(package_file: str) -> None:
    """Register a package whose frames should be skipped when determining a log record's origin.

    A package whose modules log on behalf of the updaters registers itself, passing its `__init__.py`'s `__file__`,
    so the frames of every module in it are walked past and a log record is attributed to the updater that triggered
    it rather than to the shared machinery in between. Registration covers the whole package, so a module added to it
    needs none of its own.
    """
    _helper_packages.add(Path(package_file).resolve().parent)


def _is_helper_frame(filename: str) -> bool:
    """Return whether the frame's file is this wrapper or a module in a registered package."""
    path = Path(filename).resolve()
    return path == _wrapper_file or any(path.is_relative_to(package) for package in _helper_packages)


def _caller_stacklevel() -> int:
    """Return the stacklevel of the first frame outside this wrapper and the registered packages.

    A fixed stacklevel can't work because some log methods are called directly by an updater while others
    are dispatched through a registered package (with extra comprehension frames in between), so walk
    the stack to find the originating updater frame instead.
    """
    level = 1  # Start at the frame that emits the record (Logger._log) and skip helper frames from there.
    try:
        frame: FrameType | None = sys._getframe(level)  # noqa: SLF001
    except ValueError:  # pragma: no cover
        return level
    while frame is not None and _is_helper_frame(frame.f_code.co_filename):
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
    def _render_dependency(dependency: str) -> str:
        """Bracket a dependency name in `DEPENDENCY_DELIMITER` so the highlighter styles it as one token."""
        return f"{DEPENDENCY_DELIMITER}{dependency}{DEPENDENCY_DELIMITER}"

    @staticmethod
    def _render_location(location: Location) -> str:
        """Bracket a location's text in `LOCATION_DELIMITER` so the highlighter styles the whole run as one token."""
        return f"{LOCATION_DELIMITER}{location}{LOCATION_DELIMITER}"

    def _log_ignored(self, message: str, dependency: str, marker: Marker, location: Location) -> None:
        """Log, at debug level, that a marker held a reference's update or one of its warnings back.

        Only the `ignore` directive is named (`raw_marker(Verb.IGNORE)`), not the whole marker: it is the one that
        held the update or the warning back, and it is echoed exactly as the user spelled it.
        """
        self._log(
            self.log.debug,
            message,
            self._render_dependency(dependency),
            self._render_location(location),
            marker.raw_marker(Verb.IGNORE),
        )

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

    MESSAGE_NEW_VERSION = "New version available for %s in %s: %s\n%s"
    _SUPPRESSING_CHANGELOG = "Suppressing changelog already shown, see above"
    NO_CHANGELOG = "No changelog available!"

    def new_version(self, dependency: str, version: DependencyVersion, location: Location) -> None:
        """Log the availability of a new version for a dependency in a file, with its UTC publication date if known."""
        if (dependency, version) in self.logged_changes:
            changes = self._SUPPRESSING_CHANGELOG
        else:
            changes = version.changes or self.NO_CHANGELOG
        self.logged_changes.add((dependency, version))
        new_version = version.version
        if version.published is not None:
            new_version += f", published: {version.published.astimezone(UTC):%Y-%m-%d %H:%M}"
        self._log(
            self.log.info,
            self.MESSAGE_NEW_VERSION,
            self._render_dependency(dependency),
            self._render_location(location),
            new_version,
            changes,
        )

    _MESSAGE_PINNED = "Pinned %s in %s to %s@%s"

    def pinned(self, dependency: str, version: DependencyVersion, location: Location) -> None:
        """Log that a previously unpinned reference in a file was pinned to a digest, without changing its version."""
        self._log(
            self.log.info,
            self._MESSAGE_PINNED,
            self._render_dependency(dependency),
            self._render_location(location),
            version.version,
            version.sha,
        )

    _MESSAGE_DIGEST_DRIFT = (
        "Digest drift for %s:%s in %s: pinned to %s but the registry "
        "now serves %s; the pin was left unchanged, verify the change is expected before updating the pin"
    )

    def digest_drift(self, dependency: str, version: str, current_sha: str, new_sha: str, location: Location) -> None:
        """Warn that an already-pinned tag now resolves to a different digest, and that the pin was left unchanged.

        The tag was re-pushed (rebuilt) under the same name, so its pinned digest no longer matches what the registry
        serves. Update-time deliberately does not update the pin: silently adopting a re-pushed digest would defeat
        the immutability a digest pin exists to provide. The drift is surfaced instead, so it can be reviewed.
        """
        self._log(
            self.log.warning,
            self._MESSAGE_DIGEST_DRIFT,
            self._render_dependency(dependency),
            version,
            self._render_location(location),
            current_sha,
            new_sha,
        )

    _MESSAGE_ADOPTED_DIGEST_DRIFT = "Adopted digest drift for %s:%s in %s: re-pinned from %s to %s (%s)"

    def adopted_drift(  # noqa: PLR0913
        self, dependency: str, version: str, current_sha: str, new_sha: str, location: Location, cause: str
    ) -> None:
        """Log, at info level, that a re-pushed tag's new digest was adopted because the reference opted in.

        `cause` names the opt-in that triggered the adoption (the reference's `# update-time: allow[digest-drift]`
        marker, or the repo-wide `--allow-image-digest-drift` flag). Unlike `digest_drift`, this is a normal change the
        user asked for, so it is info, not a warning.
        """
        self._log(
            self.log.info,
            self._MESSAGE_ADOPTED_DIGEST_DRIFT,
            self._render_dependency(dependency),
            version,
            self._render_location(location),
            current_sha,
            new_sha,
            cause,
        )

    _MESSAGE_STALE = "Stale dependency %s in %s: newest release %s was published %d days ago (> %d)"

    def warn_if_stale(self, dependency: str, version: DependencyVersion, location: Location) -> None:
        """Warn if the dependency's newest release is old enough that the project may have gone quiet.

        Does nothing when the newest release date is unknown or within the threshold (or the check is disabled),
        so callers can hand off every resolved version unconditionally.
        """
        if (published := version.newest_published) is None or not is_stale(published):
            return
        self._log(
            self.log.warning,
            self._MESSAGE_STALE,
            self._render_dependency(dependency),
            self._render_location(location),
            version.version,
            staleness_days(published),
            STALE_AFTER.get(),
        )

    _MESSAGE_IGNORED_STALENESS = "Ignoring the staleness warning for %s in %s (update-time: %s)"

    def ignored_staleness(
        self, dependency: str, version: DependencyVersion, marker: Marker, location: Location
    ) -> None:
        """Log, at debug level, that the marker held back a staleness warning that would otherwise have been logged.

        Guards on the same condition as `warn_if_stale`, so callers can hand off every reference unconditionally and
        a marker that suppresses nothing stays silent.
        """
        if is_stale(version.newest_published):
            self._log_ignored(self._MESSAGE_IGNORED_STALENESS, dependency, marker, location)

    _MESSAGE_YANKED = "Yanked dependency %s in %s: version %s was yanked (%s)"

    def warn_if_yanked(self, dependency: str, version: DependencyVersion, location: Location) -> None:
        """Warn that the version the reference is pinned to has been yanked; do nothing when it was not yanked.

        The maintainer's reason is appended in parentheses, quoted when given and "reason not specified" when not.
        """
        if not version.yank.yanked:
            return
        reason = f'"{version.yank.reason}"' if version.yank.reason else "reason not specified"
        self._log(
            self.log.warning,
            self._MESSAGE_YANKED,
            self._render_dependency(dependency),
            self._render_location(location),
            version.version,
            reason,
        )

    _MESSAGE_IGNORED_YANK = "Ignoring the yank warning for %s in %s (update-time: %s)"

    def ignored_yank(self, dependency: str, version: DependencyVersion, marker: Marker, location: Location) -> None:
        """Log, at debug level, that the marker held back a yank warning that would otherwise have been logged.

        Guards on the same condition as `warn_if_yanked`, so callers can hand off every reference unconditionally and
        a marker that suppresses nothing stays silent.
        """
        if version.yank.yanked:
            self._log_ignored(self._MESSAGE_IGNORED_YANK, dependency, marker, location)

    _MESSAGE_REDUNDANT_YANK_SCOPE = (
        "Redundant update-time marker %s for %s in %s: this dependency's source has no yank concept, "
        "so the marker holds nothing back"
    )

    def redundant_yank_scope(self, dependency: str, marker: Marker, location: Location) -> None:
        """Warn that the marker's yank scope can never hold anything back for this dependency.

        The dependency's source reports no yanks, so its version's yank state is always "not yanked" and the scope is
        inert for as long as the reference points where it points. Like a redundant version bound, that is worth
        reporting rather than leaving silent. Only the `ignore` directive is named, echoed as the user spelled it,
        since it is the one that holds nothing back.
        """
        self._log(
            self.log.warning,
            self._MESSAGE_REDUNDANT_YANK_SCOPE,
            marker.raw_marker(Verb.IGNORE),
            self._render_dependency(dependency),
            self._render_location(location),
        )

    _MESSAGE_NO_VERSION = "No valid version found for %s"

    def no_version(self, dependency: str) -> None:
        """Log no version found."""
        self._log(self.log.error, self._MESSAGE_NO_VERSION, self._render_dependency(dependency))

    _MESSAGE_NO_COMMIT_SHA = "Could not fetch commit SHA for %s %s (%s): %s"

    def no_commit_sha(self, dependency: str, version: str, reason: str, url: str) -> None:
        """Log that no commit SHA could be fetched for an otherwise-eligible release, and why."""
        self._log(
            self.log.error, self._MESSAGE_NO_COMMIT_SHA, self._render_dependency(dependency), version, reason, url
        )

    _MESSAGE_NO_TAG_DATE = (
        "Could not determine the publication date of %s tag %s (%s), "
        "so the cooldown can't be verified; skipping this version"
    )

    def no_tag_date(self, dependency: str, tag: str, reason: str) -> None:
        """Log that a tag's commit date couldn't be resolved, and why, so the tag was skipped as an update candidate."""
        self._log(self.log.error, self._MESSAGE_NO_TAG_DATE, self._render_dependency(dependency), tag, reason)

    _MESSAGE_NO_INTEGRITY_HASH = "Could not resolve the integrity hash for %s %s (%s), leaving it unchanged"

    def no_integrity_hash(self, dependency: str, version: str, filename: str) -> None:
        """Warn that a jsDelivr file's integrity hash couldn't be resolved, so the reference is left unchanged."""
        self._log(
            self.log.warning, self._MESSAGE_NO_INTEGRITY_HASH, self._render_dependency(dependency), version, filename
        )

    _MESSAGE_INVALID_SPECIFIER = "Invalid %r in the update-time marker for %s in %s; leaving the reference unchanged"

    def invalid_specifier(self, dependency: str, specifier: str, location: Location) -> None:
        """Warn that a marker carried an invalid version specifier or item, so the reference is left unchanged."""
        self._log(
            self.log.warning,
            self._MESSAGE_INVALID_SPECIFIER,
            specifier,
            self._render_dependency(dependency),
            self._render_location(location),
        )

    _MESSAGE_REDUNDANT_BOUND = "Redundant update bound %s on %s %s in %s: it %s"

    def warn_if_redundant_bound(
        self, dependency: str, marker: Marker, current_version: VersionString, location: Location
    ) -> None:
        """Warn when the marker's version bound is redundant for the current version.

        The bound is redundant when it never has an effect or blocks every update (see
        `VersionBound.redundancy`). Does nothing when the reference has no bound or the bound is live, so callers can
        hand off every reference unconditionally. The keep-all `NO_BOUND`, the unmarked default, is not a bound to
        report on. The bound renders itself in its marker form (`allow[update<3.13]`, or a level-based
        `ignore[minor-update]`), so the warning shows which bound on which pin is redundant.
        """
        if (bound := marker.version_bound) == NO_BOUND:
            return
        if (redundancy := bound.redundancy(current_version)) is None:
            return
        self._log(
            self.log.warning,
            self._MESSAGE_REDUNDANT_BOUND,
            bound,
            self._render_dependency(dependency),
            current_version,
            self._render_location(location),
            redundancy.value,
        )

    # --- File scanning and selection ---

    _MESSAGE_CHECKING_PATH = "Checking if there are updates for %s"

    def path(self, path: Path) -> None:
        """Log working on path."""
        self._log(self.log.debug, self._MESSAGE_CHECKING_PATH, self._render_location(Location(path)))

    _MESSAGE_RECOGNISED_MARKER = "Recognised update-time marker %s for %s in %s"

    def recognised_marker(self, dependency: str, marker: Marker, location: Location) -> None:
        """Log, at debug level, that a reference's marker was recognised, so users can confirm it was understood.

        Reports that the marker was read and parsed, not that it had any effect: what a marker actually held back is
        reported separately, by `ignored`, `ignored_staleness`, and `ignored_yank`. The marker's directives are echoed
        verbatim (the `raw` text the user wrote), so a user comparing the log line against their file sees their own
        marker. Does nothing when the line carries no marker (the `raw` text is empty), so the caller can hand off
        every reference unconditionally.
        """
        if not (directives := marker.raw_marker()):
            return
        self._log(
            self.log.debug,
            self._MESSAGE_RECOGNISED_MARKER,
            directives,
            self._render_dependency(dependency),
            self._render_location(location),
        )

    _MESSAGE_IGNORED = "Ignoring updates for %s in %s (update-time: %s)"

    def ignored(self, dependency: str, marker: Marker, location: Location) -> None:
        """Log that a reference's update was held back by the marker's `ignore` directive, echoing it as written."""
        self._log_ignored(self._MESSAGE_IGNORED, dependency, marker, location)

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
        self._log(self.log.info, self._MESSAGE_SKIP_PATH, self._render_location(Location(path)), reason)

    # --- Updater-specific diagnostics ---

    _MESSAGE_UV_COOLDOWN = "Set uv exclude-newer to %r in %s to apply the cooldown"

    def configured_uv_cooldown(self, path: Path, cooldown: str) -> None:
        """Log that Update-time wrote its cooldown into the project's uv configuration.

        The path is a workspace root, which can sit above the current directory (when Update-time runs inside a
        member), so fall back to the absolute path when it can't be made relative to the working directory.
        """
        self._log(self.log.info, self._MESSAGE_UV_COOLDOWN, cooldown, self._render_location(Location(path)))

    _MESSAGE_SKIP_UNSUPPORTED = "Skipping %s: %s is not supported, only %s"

    def unsupported_package_manager(self, path: Path, manager: str, supported: str) -> None:
        """Warn that a file is managed by an unsupported package manager, so its dependencies are left unchanged."""
        self._log(
            self.log.warning, self._MESSAGE_SKIP_UNSUPPORTED, self._render_location(Location(path)), manager, supported
        )

    _MESSAGE_INVALID_TOML = "Skipping %s: it is not valid TOML"

    def invalid_pyproject_toml(self, path: Path) -> None:
        """Warn that a pyproject.toml can't be parsed as TOML, so it is skipped rather than crashing the run."""
        self._log(self.log.warning, self._MESSAGE_INVALID_TOML, self._render_location(Location(path)))

    _MESSAGE_NON_NUMERIC_NODE_BASE_IMAGE_TAG = (
        "Cannot derive the Node engine version from the non-numeric base image tag 'node:%s' in %s"
    )

    def non_numeric_node_base_image(self, dockerfile: Path, tag: str) -> None:
        """Log that the Node base image tag is not a concrete version, so the Node engine can't be derived."""
        self._log(self.log.warning, self._MESSAGE_NON_NUMERIC_NODE_BASE_IMAGE_TAG, tag, dockerfile)

    # --- HTTP fetching ---

    _MESSAGE_NOT_OK_RESPONSE = "Could not fetch %s: HTTP %s %s"

    def response(self, response: Response) -> None:
        """Log a response's status code and reason phrase (e.g. `HTTP 404 Not Found`)."""
        self._log(self.log.warning, self._MESSAGE_NOT_OK_RESPONSE, response.url, response.status_code, response.reason)

    _MESSAGE_TIMEOUT = "Timeout while fetching %s"

    def timeout(self, url: str) -> None:
        """Log a request timeout."""
        self._log(self.log.warning, self._MESSAGE_TIMEOUT, url)

    _MESSAGE_REQUEST_ERROR = "Could not fetch %s: %s"

    def request_error(self, url: str, error: object) -> None:
        """Log a network error (connection failure, too many redirects, ...) while fetching a URL."""
        self._log(self.log.warning, self._MESSAGE_REQUEST_ERROR, url, error)

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
        console = Console(stderr=True, theme=LOG_THEME)
        handler = RichHandler(console=console, highlighter=LogHighlighter())
        logging.basicConfig(
            level=LOG_LEVEL.get(), datefmt=LOG_TIME_FORMAT, format=LOG_MESSAGE_FORMAT, handlers=[handler]
        )
    return Logger(name)

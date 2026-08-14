"""Log helpers."""

import logging
import re
from dataclasses import dataclass
from logging import DEBUG, ERROR, INFO, WARNING
from typing import TYPE_CHECKING

from rich.console import Console
from rich.highlighter import ReprHighlighter
from rich.logging import RichHandler
from rich.theme import Theme

from update_time.domain.bound import NO_BOUND, Verb
from update_time.domain.staleness import is_stale
from update_time.primitives.digest import SHA256_DIGEST
from update_time.primitives.environment import EnvVar
from update_time.primitives.location import Location
from update_time.primitives.timestamp import days_since

if TYPE_CHECKING:
    from pathlib import Path

    from requests import Response
    from rich.text import Text

    from update_time.domain.dependency import DependencyVersion
    from update_time.domain.drift import DriftedPin
    from update_time.domain.marker import Marker
    from update_time.domain.reference import Reference, ResolvedReference
    from update_time.domain.vulnerability import Vulnerability
    from update_time.primitives.command import Command


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

# Private-use-area character that brackets a file location (a `path` or `path:line`) in a log message. The
# highlighter can then colour the whole run — directory, filename, and line number — as one token, instead of the
# several fragments Rich's default rules produce: a `repr.path` prefix, a `repr.filename`, and a `repr.number` for
# the line. It is not matched by shape for the same reason the digest is dropped and the dependency name is
# delimited. The path is known exactly when the message is built, whereas a regex over the finished message can't
# tell it apart from the version numbers and digests around it, and a bare filename such as `Dockerfile` carries
# no path marker to anchor on. A distinct code point from `DEPENDENCY_DELIMITER`, so the two runs never collide.
# `Logger` wraps each location in it, and `LogHighlighter` styles the run and strips them.
LOCATION_DELIMITER = ""


class LogHighlighter(ReprHighlighter):
    """Rich highlighter that colours a whole `sha256:` digest, dependency name, and file location as single tokens.

    Rich's built-in rules otherwise match only fragments of a digest — the `256` reads as a number and a run such
    as `a256:a4fd` reads as an IPv6 address — colouring parts of it and leaving the rest plain. Matching the full
    digest and dropping the built-in sub-spans inside it styles the whole digest uniformly (as `repr.digest`), while
    every other message keeps Rich's default highlighting of version numbers and the like.

    A dependency name can't be matched by shape, and a file location can't be told apart from the versions and digests
    around it, so `Logger` brackets each with its own delimiter. A bracketed dependency name is styled as
    `repr.dependency`, keeping Rich's inner highlighting. A bracketed location is styled as `repr.filename` with its
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
        highlighting is kept and `style` layered on top, so a dependency name keeps Rich's inner colours. Without it,
        the inner spans are dropped first and the whole run takes `style` uniformly, so a location's `path:line`
        reads as one token rather than a separate path, filename, and number.
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
# When colour is off, all render as plain text. The theme is shared with `tools/generate_log_svg.py`, which logs its
# sample through `Logger` and `configure_logging`, so the README screenshot renders exactly like the real output.
LOG_THEME = Theme({"repr.digest": "dim", "repr.dependency": "bold white"})
_LOG_TIME_FORMAT = "[%X]"
_LOG_MESSAGE_FORMAT = "%(message)s"


def configure_logging(console: Console, level: str) -> RichHandler:
    """Send every record at the level or above to the console, and return the handler that renders it there."""
    handler = RichHandler(console=console, highlighter=LogHighlighter(), show_path=False)
    logging.basicConfig(level=level, datefmt=_LOG_TIME_FORMAT, format=_LOG_MESSAGE_FORMAT, handlers=[handler])
    return handler


@dataclass(frozen=True)
class LogMessage:
    """A log message: the level it is logged at and the format string the arguments are interpolated into."""

    level: int
    format: str

    def __str__(self) -> str:
        """Return the format string, so logging can interpolate the arguments into it."""
        return self.format

    def __repr__(self) -> str:
        """Return the format string's repr, so a failing assertion reads as it did before the level travelled along."""
        return repr(self.format)


def _redundant_marker(reason: str) -> str:
    """Return the warning a marker that decides nothing is reported as, ending at the reason the caller gives."""
    return f"Redundant update-time marker %(directive)s for %(dependency)s in %(location)s: {reason}"


class Logger:
    """Wrap a logger and add update specific log methods."""

    def __init__(self, name: str) -> None:
        """Initialize the logger."""
        self.log = logging.getLogger(name)
        self._logged_changes: set[tuple[str, DependencyVersion]] = set()

    def forget_shown_changelogs(self) -> None:
        """Forget which changelogs were shown, so reporting one of them again shows it rather than suppressing it."""
        self._logged_changes.clear()

    def _log(self, message: LogMessage, **fields: object) -> None:
        """Emit a log record at the message's own level."""
        self.log.log(message.level, message, self._rendered(fields))

    @classmethod
    def _rendered(cls, fields: dict[str, object]) -> dict[str, object]:
        """Return the fields with the ones the highlighter styles wrapped in their delimiter."""
        return {name: cls._render_field(name, value) for name, value in fields.items()}

    @classmethod
    def _render_field(cls, name: str, value: object) -> object:
        """Return the field's value, wrapped in its delimiter when the highlighter styles it as one token.

        A location is recognised by its type; a dependency has no type of its own, and its name has no fixed shape to
        match either, so the field's name identifies it instead. Which of the two a file arrives as is the message's
        own choice: a file the scan found travels as a `Location`, reported relative to the working directory and
        styled as one token, while a path the user named on the command line stays a plain `Path`, logged as written.
        """
        if isinstance(value, Location):
            return cls._render_location(value)
        if name == "dependency":
            return cls._render_dependency(str(value))
        return value

    @staticmethod
    def _render_dependency(dependency: str) -> str:
        """Bracket a dependency name in `DEPENDENCY_DELIMITER` so the highlighter styles it as one token."""
        return f"{DEPENDENCY_DELIMITER}{dependency}{DEPENDENCY_DELIMITER}"

    @staticmethod
    def _render_location(location: Location) -> str:
        """Bracket a location's text in `LOCATION_DELIMITER` so the highlighter styles the whole run as one token."""
        return f"{LOCATION_DELIMITER}{location}{LOCATION_DELIMITER}"

    @staticmethod
    def _reference_fields(reference: Reference, **extra: object) -> dict[str, object]:
        """Return the fields every message about a reference carries, plus the ones the message reporting it adds."""
        return {"dependency": reference.dependency, "location": reference.location, **extra}

    def _log_ignored(self, message: LogMessage, dependency: str, marker: Marker, location: Location) -> None:
        """Log that a marker held a reference's update or one of its warnings back, at the message's own level.

        Only the `ignore` directive is named, because it is the one that held the update or the warning back.
        """
        self._log(message, dependency=dependency, location=location, directive=marker.raw_directives(Verb.IGNORE))

    def _log_file(self, message: LogMessage, path: Path, **fields: object) -> None:
        """Log a message about a file the scan found, at the message's own level.

        Wrapping the path in a `Location` is what makes it one of those files, reported relative to the working
        directory and styled as a single token. A path the user named on the command line stays a plain `Path` instead
        (see `_render_field`).
        """
        self._log(message, location=Location(path), **fields)

    # --- Run gating ---

    _MESSAGE_FORCED_OUTSIDE_GIT_REPOSITORY = LogMessage(
        WARNING,
        "Running outside a git repository (%(path)s) because --force was given; changes are made in place and cannot "
        "be reverted",
    )

    def forced_outside_git_repository(self, path: Path) -> None:
        """Warn that Update-time is running outside the git repository at the path because --force overrode the refusal.

        The path is the scan root, which is the working directory, so it is logged as its absolute self (making it
        relative would collapse it to `.`).
        """
        self._log(self._MESSAGE_FORCED_OUTSIDE_GIT_REPOSITORY, path=path)

    # --- Source results: resolving a dependency's latest version and digest ---

    _MESSAGE_NEW_VERSION = LogMessage(
        INFO, "New version available for %(dependency)s in %(location)s: %(version)s\n%(changes)s"
    )
    _SUPPRESSING_CHANGELOG = "Suppressing changelog already shown, see above"
    _NO_CHANGELOG = "No changelog available!"

    def new_version(self, reference: Reference, version: DependencyVersion) -> None:
        """Log the availability of a new version for a dependency in a file, with its UTC publication date if known."""
        dependency = reference.dependency
        if (dependency, version) in self._logged_changes:
            changes = self._SUPPRESSING_CHANGELOG
        else:
            changes = version.changes or self._NO_CHANGELOG
        self._logged_changes.add((dependency, version))
        self._log(self._MESSAGE_NEW_VERSION, **self._reference_fields(reference, version=str(version), changes=changes))

    _MESSAGE_PINNED = LogMessage(INFO, "Pinned %(dependency)s in %(location)s to %(version)s@%(sha)s")

    def pinned(self, reference: Reference, version: DependencyVersion) -> None:
        """Log that a previously unpinned reference in a file was pinned to a digest, without changing its version."""
        self._log(self._MESSAGE_PINNED, **self._reference_fields(reference, version=version.version, sha=version.sha))

    _MESSAGE_CANNOT_PIN = LogMessage(
        INFO,
        "Cannot pin %(dependency)s in %(location)s: the URL is declared as a bare string, so it has no attribute "
        "dictionary to hold an integrity hash",
    )

    def cannot_pin(self, dependency: str, location: Location) -> None:
        """Log that a reference was left unpinned because it declares nowhere to hold the hash that would pin it."""
        self._log(self._MESSAGE_CANNOT_PIN, dependency=dependency, location=location)

    _MESSAGE_DIGEST_DRIFT = LogMessage(
        WARNING,
        "Digest drift for %(dependency)s:%(version)s in %(location)s: pinned to %(current_sha)s but the registry "
        "now serves %(new_sha)s; the pin was left unchanged, verify the change is expected before updating the pin",
    )

    @staticmethod
    def _drift_fields(drifted: DriftedPin, **extra: object) -> dict[str, object]:
        """Return the fields every drift message carries, plus the ones the message reporting it adds."""
        return {
            "dependency": drifted.dependency,
            "version": drifted.current_version,
            "location": drifted.location,
            "current_sha": drifted.current_sha,
            "new_sha": drifted.new_sha,
            **extra,
        }

    def digest_drift(self, drifted: DriftedPin) -> None:
        """Warn that an already-pinned tag now resolves to a different digest at the registry."""
        self._log(self._MESSAGE_DIGEST_DRIFT, **self._drift_fields(drifted))

    _MESSAGE_ADOPTED_DIGEST_DRIFT = LogMessage(
        INFO,
        "Adopted digest drift for %(dependency)s:%(version)s in %(location)s: "
        "re-pinned from %(current_sha)s to %(new_sha)s (%(cause)s)",
    )

    def adopted_drift(self, drifted: DriftedPin, cause: str) -> None:
        """Log that a re-pushed tag's new digest was adopted because the reference opted in.

        `cause` names the opt-in that triggered the adoption (see `report_drift`). Unlike `digest_drift`, this is a
        normal change the user asked for, so it is info, not a warning.
        """
        self._log(self._MESSAGE_ADOPTED_DIGEST_DRIFT, **self._drift_fields(drifted, cause=cause))

    _MESSAGE_TAG_DRIFT = LogMessage(
        WARNING,
        "Tag drift for %(dependency)s@%(version)s in %(location)s: pinned to commit %(current_sha)s but the tag now "
        "points at %(new_sha)s; the pin was left unchanged, verify the tag was moved deliberately before updating "
        "the pin",
    )

    def tag_drift(self, drifted: DriftedPin) -> None:
        """Warn that a version tag now points at another commit than the one the reference is pinned to."""
        self._log(self._MESSAGE_TAG_DRIFT, **self._drift_fields(drifted))

    _MESSAGE_ADOPTED_TAG_DRIFT = LogMessage(
        INFO,
        "Adopted tag drift for %(dependency)s@%(version)s in %(location)s: "
        "re-pinned from commit %(current_sha)s to %(new_sha)s (%(cause)s)",
    )

    def adopted_tag_drift(self, drifted: DriftedPin, cause: str) -> None:
        """Log that a moved tag's new commit was adopted because the reference opted in.

        `cause` names the opt-in that triggered the adoption. Like `adopted_drift`, this is a change the user asked
        for, so it is logged at info rather than as a warning.
        """
        self._log(self._MESSAGE_ADOPTED_TAG_DRIFT, **self._drift_fields(drifted, cause=cause))

    _MESSAGE_HASH_MISMATCH = LogMessage(
        WARNING,
        "Integrity hash mismatch for %(dependency)s@%(version)s in %(location)s: declares %(declared_hash)s but "
        "jsDelivr serves %(served_hash)s; the hash was left unchanged, and since npm does not republish a version "
        "it is probably the declared hash that is wrong",
    )

    def hash_mismatch(
        self, dependency: str, version: str, declared_hash: str, served_hash: str, location: Location
    ) -> None:
        """Warn that a declared Subresource Integrity hash disagrees with the one the CDN serves for that version."""
        self._log(
            self._MESSAGE_HASH_MISMATCH,
            dependency=dependency,
            version=version,
            location=location,
            declared_hash=declared_hash,
            served_hash=served_hash,
        )

    _MESSAGE_STALE = LogMessage(
        WARNING,
        "Stale dependency %(dependency)s in %(location)s: newest release %(version)s was published "
        "%(days)d days ago (> %(threshold)d)",
    )

    def warn_if_stale(self, resolved: ResolvedReference, threshold: int) -> None:
        """Warn if the dependency's newest release is old enough that the project may have gone quiet.

        Does nothing when the newest release date is unknown or within the threshold (or the check is disabled),
        so callers can hand off every resolved version unconditionally. Both the decision and the reported day count
        are whole days, so the count in the message always exceeds the threshold beside it.
        """
        if (published := resolved.release.newest_published) is None or not is_stale(published, threshold):
            return
        self._log(
            self._MESSAGE_STALE,
            **self._reference_fields(
                resolved, version=resolved.release.version, days=days_since(published), threshold=threshold
            ),
        )

    _MESSAGE_IGNORED_STALENESS = LogMessage(
        DEBUG, "Ignoring the staleness warning for %(dependency)s in %(location)s (update-time: %(directive)s)"
    )

    def _ignored_staleness(self, resolved: ResolvedReference, marker: Marker, threshold: int) -> None:
        """Log that the marker held back a staleness warning that would otherwise have been logged.

        Guards on the same condition as `warn_if_stale`, so a marker that suppresses nothing stays silent.
        """
        if is_stale(resolved.release.newest_published, threshold):
            self._log_ignored(self._MESSAGE_IGNORED_STALENESS, resolved.dependency, marker, resolved.location)

    def report_staleness(self, resolved: ResolvedReference, marker: Marker, threshold: int) -> None:
        """Report the reference's staleness, as a warning or as the hold-back of the marker that silences it.

        Every reference that can carry a marker is reported through here, so a caller reporting one cannot forget
        that its marker may hold the warning back.
        """
        if marker.ignore_stale:
            self._ignored_staleness(resolved, marker, threshold)
        else:
            self.warn_if_stale(resolved, threshold)

    _MESSAGE_YANKED = LogMessage(
        WARNING, "Yanked dependency %(dependency)s in %(location)s: version %(version)s was yanked (%(reason)s)"
    )

    def warn_if_yanked(self, resolved: ResolvedReference) -> None:
        """Warn that the version the reference is pinned to has been yanked; do nothing when it was not yanked.

        The message shows the yank in parentheses, where it renders itself as the maintainer's reason.
        """
        release = resolved.release
        if not release.yank.yanked:
            return
        self._log(
            self._MESSAGE_YANKED, **self._reference_fields(resolved, version=release.version, reason=release.yank)
        )

    _MESSAGE_IGNORED_YANK = LogMessage(
        DEBUG, "Ignoring the yank warning for %(dependency)s in %(location)s (update-time: %(directive)s)"
    )

    def ignored_yank(self, resolved: ResolvedReference, marker: Marker) -> None:
        """Log that the marker held back a yank warning that would otherwise have been logged.

        Guards on the same condition as `warn_if_yanked`, so callers can hand off every reference unconditionally and
        a marker that suppresses nothing stays silent.
        """
        if resolved.release.yank.yanked:
            self._log_ignored(self._MESSAGE_IGNORED_YANK, resolved.dependency, marker, resolved.location)

    _MESSAGE_MALFORMED_CVSS_VECTOR = LogMessage(
        WARNING,
        "Could not score the CVSS vector of advisory %(advisory)s (%(error)s), so it is reported at unknown severity",
    )

    def malformed_cvss_vector(self, advisory: str, error: object) -> None:
        """Warn that an advisory's CVSS vector could not be scored, so its risk level could not be derived."""
        self._log(self._MESSAGE_MALFORMED_CVSS_VECTOR, advisory=advisory, error=error)

    _MESSAGE_VULNERABLE_DEPENDENCY = LogMessage(
        WARNING,
        "Vulnerable dependency %(dependency)s in %(location)s: version %(version)s has %(vulnerability)s "
        "(%(advisory)s, %(url)s)",
    )

    @classmethod
    def _vulnerability_fields(cls, reference: Reference, vulnerability: Vulnerability) -> dict[str, object]:
        """Return the fields the vulnerability warning carries."""
        return cls._reference_fields(
            reference,
            version=reference.current_version,
            vulnerability=str(vulnerability),
            advisory=vulnerability.advisory,
            url=vulnerability.url,
        )

    def vulnerable_dependency(self, reference: Reference, vulnerability: Vulnerability) -> None:
        """Warn that the version the reference is pinned to has a known vulnerability, naming the advisory."""
        self._log(self._MESSAGE_VULNERABLE_DEPENDENCY, **self._vulnerability_fields(reference, vulnerability))

    _MESSAGE_IGNORED_VULNERABILITY = LogMessage(
        DEBUG, "Ignoring the vulnerability warning for %(dependency)s in %(location)s (update-time: %(directive)s)"
    )

    def ignored_vulnerability(self, reference: Reference, marker: Marker) -> None:
        """Log that the marker held back a vulnerability warning that would otherwise have been logged.

        Called per vulnerability the risk level in force would have reported, so a marker suppressing nothing stays
        silent, as the staleness and yank hold-backs do. This is what the reference is looked up for although its
        warning is suppressed: without the lookup there is nothing to say the marker held anything back.
        """
        self._log_ignored(self._MESSAGE_IGNORED_VULNERABILITY, reference.dependency, marker, reference.location)

    _MESSAGE_GLOBALLY_IGNORED_VULNERABILITY = LogMessage(
        DEBUG,
        "Ignoring the vulnerability warning for %(dependency)s in %(location)s (--ignore-vulnerability %(advisory)s)",
    )

    def globally_ignored_vulnerability(self, reference: Reference, advisory: str) -> None:
        """Log that the run-wide option held back a vulnerability warning, naming the advisory it silenced.

        The advisory named is the one the warning would have reported, which the reader may have passed under
        another of its identifiers.
        """
        self._log(self._MESSAGE_GLOBALLY_IGNORED_VULNERABILITY, **self._reference_fields(reference, advisory=advisory))

    @classmethod
    def _redundant_suppression_fields(cls, reference: Reference, directive: str) -> dict[str, object]:
        """Return the fields a vulnerability suppression that holds nothing back is reported with.

        The directive is the caller's, since each of these messages judges one form of the `vulnerable` scope and
        the forms beside it may hold plenty back.
        """
        return cls._reference_fields(reference, directive=directive, version=reference.current_version)

    _MESSAGE_REDUNDANT_VULNERABLE_SCOPE = LogMessage(
        WARNING, _redundant_marker("version %(version)s has no vulnerability")
    )

    def redundant_vulnerable_scope(self, reference: Reference, marker: Marker) -> None:
        """Warn that the marker's vulnerability scope found no vulnerability to hold back for the pinned version."""
        self._log(
            self._MESSAGE_REDUNDANT_VULNERABLE_SCOPE,
            **self._redundant_suppression_fields(reference, marker.vulnerable_scope_directive),
        )

    _MESSAGE_REDUNDANT_VULNERABLE_ADVISORY = LogMessage(
        WARNING, _redundant_marker("version %(version)s has no such vulnerability")
    )

    def redundant_vulnerable_advisory(self, reference: Reference, marker: Marker) -> None:
        """Warn that the marker names an advisory none of the pinned version's vulnerabilities answers to."""
        self._log(
            self._MESSAGE_REDUNDANT_VULNERABLE_ADVISORY,
            **self._redundant_suppression_fields(reference, marker.advisory_directives),
        )

    _MESSAGE_REDUNDANT_VULNERABLE_LEVEL = LogMessage(
        WARNING, _redundant_marker("version %(version)s has no vulnerability below %(level)s")
    )

    def redundant_vulnerable_level(self, reference: Reference, marker: Marker, level: str) -> None:
        """Warn that the marker's risk level left no vulnerability of the pinned version below it to hold back."""
        self._log(
            self._MESSAGE_REDUNDANT_VULNERABLE_LEVEL,
            **self._redundant_suppression_fields(reference, marker.vulnerable.directive),
            level=level,
        )

    _MESSAGE_REDUNDANT_VULNERABLE_SOURCE = LogMessage(
        WARNING, _redundant_marker("this dependency's source reports no vulnerabilities")
    )

    def redundant_vulnerable_source(self, reference: Reference, marker: Marker) -> None:
        """Warn that the marker's vulnerability scope can never hold anything back for this dependency."""
        self._log(
            self._MESSAGE_REDUNDANT_VULNERABLE_SOURCE,
            **self._reference_fields(reference, directive=marker.vulnerable_directives),
        )

    _MESSAGE_REDUNDANT_COOLDOWN_ITEM = LogMessage(
        WARNING, _redundant_marker("this dependency's source reports no publication date to measure a cooldown against")
    )

    def redundant_cooldown_item(self, reference: Reference, marker: Marker) -> None:
        """Warn that the marker's cooldown can never hold anything back for this dependency."""
        self._log(
            self._MESSAGE_REDUNDANT_COOLDOWN_ITEM,
            **self._reference_fields(reference, directive=marker.cooldown_directive),
        )

    _MESSAGE_REDUNDANT_STALE_SOURCE = LogMessage(
        WARNING, _redundant_marker("this dependency's source reports no publication date to measure staleness against")
    )

    def redundant_stale_source(self, reference: Reference, marker: Marker) -> None:
        """Warn that the marker's `stale` directive decides nothing here, in whichever form it was written."""
        self._log(
            self._MESSAGE_REDUNDANT_STALE_SOURCE,
            **self._reference_fields(reference, directive=marker.stale_directive),
        )

    _MESSAGE_REDUNDANT_YANK_SCOPE = LogMessage(
        WARNING, _redundant_marker("this dependency's source has no yank concept")
    )

    def redundant_yank_scope(self, reference: Reference, marker: Marker) -> None:
        """Warn that the marker's yank scope can never hold anything back for this dependency."""
        self._log(
            self._MESSAGE_REDUNDANT_YANK_SCOPE,
            **self._reference_fields(reference, directive=marker.yank_directive),
        )

    _MESSAGE_REDUNDANT_WITHOUT_AN_UPDATE = LogMessage(
        WARNING, _redundant_marker("this requirement pins no version to update")
    )

    def redundant_without_an_update(self, reference: Reference, directive: str) -> None:
        """Warn that a directive steering the update holds nothing back, no update being resolved for the reference.

        A bound decides which versions an update may move to, a cooldown which of them are too fresh to trust, so
        both decide nothing where no version is resolved. The directive is named by the caller, each reading its
        own from the marker.
        """
        self._log(self._MESSAGE_REDUNDANT_WITHOUT_AN_UPDATE, **self._reference_fields(reference, directive=directive))

    _MESSAGE_REDUNDANT_YANK_WITHOUT_A_VERSION = LogMessage(
        WARNING, _redundant_marker("this requirement pins no version to check for a yank")
    )

    def redundant_yank_without_a_version(self, reference: Reference, marker: Marker) -> None:
        """Warn that the marker's yank scope holds nothing back for a reference that pins no version."""
        self._log(
            self._MESSAGE_REDUNDANT_YANK_WITHOUT_A_VERSION,
            **self._reference_fields(reference, directive=marker.yank_directive),
        )

    _MESSAGE_REDUNDANT_VULNERABLE_WITHOUT_A_VERSION = LogMessage(
        WARNING, _redundant_marker("this requirement pins no version to check for a vulnerability")
    )

    def redundant_vulnerable_without_a_version(self, reference: Reference, marker: Marker) -> None:
        """Warn that the marker's vulnerability scope holds nothing back for a reference that pins no version."""
        self._log(
            self._MESSAGE_REDUNDANT_VULNERABLE_WITHOUT_A_VERSION,
            **self._reference_fields(reference, directive=marker.vulnerable_directives),
        )

    _MESSAGE_NO_VERSION = LogMessage(ERROR, "No valid version found for %(dependency)s")

    def no_version(self, dependency: str) -> None:
        """Log no version found."""
        self._log(self._MESSAGE_NO_VERSION, dependency=dependency)

    _MESSAGE_NO_COMMIT_SHA = LogMessage(
        ERROR, "Could not fetch commit SHA for %(dependency)s %(version)s (%(reason)s): %(url)s"
    )

    def no_commit_sha(self, dependency: str, version: str, reason: str, url: str) -> None:
        """Log that no commit SHA could be fetched for an otherwise-eligible release, and why."""
        self._log(self._MESSAGE_NO_COMMIT_SHA, dependency=dependency, version=version, reason=reason, url=url)

    _MESSAGE_NO_TAG_DATE = LogMessage(
        ERROR,
        "Could not determine the publication date of %(dependency)s tag %(tag)s (%(reason)s), "
        "so the cooldown can't be verified; skipping this version",
    )

    def no_tag_date(self, dependency: str, tag: str, reason: str) -> None:
        """Log that a tag's commit date couldn't be resolved, and why, so the tag was skipped as an update candidate."""
        self._log(self._MESSAGE_NO_TAG_DATE, dependency=dependency, tag=tag, reason=reason)

    _MESSAGE_NO_INTEGRITY_HASH = LogMessage(
        WARNING,
        "Could not resolve the integrity hash for %(dependency)s %(version)s (%(filename)s), leaving it unchanged",
    )

    def no_integrity_hash(self, dependency: str, version: str, filename: str) -> None:
        """Warn that a jsDelivr file's integrity hash couldn't be resolved, so the reference is left unchanged."""
        self._log(self._MESSAGE_NO_INTEGRITY_HASH, dependency=dependency, version=version, filename=filename)

    _MESSAGE_INVALID_BRACKET_ITEM = LogMessage(
        WARNING,
        "Invalid %(bracket_item)r in the update-time marker for %(dependency)s in %(location)s; "
        "leaving the reference unchanged",
    )

    def invalid_bracket_item(self, dependency: str, item: str, location: Location) -> None:
        """Warn that a marker carried an invalid bracket item, so the reference is left unchanged."""
        self._log(self._MESSAGE_INVALID_BRACKET_ITEM, bracket_item=item, dependency=dependency, location=location)

    # What both inverted day-count items are reported as; each names what its own comparison does instead.
    _INVERTED_ITEM = "Incorrect %(item)r in the update-time marker for %(dependency)s in %(location)s: this comparison "

    _MESSAGE_INVERTED_STALE_ITEM = LogMessage(
        WARNING,
        _INVERTED_ITEM + "warns while a release is fresh and goes quiet once it is old, so it sets no threshold",
    )

    def inverted_stale_item(self, reference: Reference, item: str) -> None:
        """Warn that a `stale` item compares the wrong way round, so it sets no threshold."""
        self._log(self._MESSAGE_INVERTED_STALE_ITEM, **self._reference_fields(reference, item=item))

    _MESSAGE_INVERTED_COOLDOWN_ITEM = LogMessage(
        WARNING,
        _INVERTED_ITEM + "adopts a release only while it is fresh and holds it back once it is old, so it sets no "
        "cooldown",
    )

    def inverted_cooldown_item(self, reference: Reference, item: str) -> None:
        """Warn that a `cooldown` item compares the wrong way round, so it sets no cooldown."""
        self._log(self._MESSAGE_INVERTED_COOLDOWN_ITEM, **self._reference_fields(reference, item=item))

    _MESSAGE_INVERTED_VULNERABLE_ITEM = LogMessage(
        WARNING,
        _INVERTED_ITEM + "warns about the mild vulnerabilities and stays quiet about the severe ones, so it sets no "
        "risk level",
    )

    def inverted_vulnerable_item(self, reference: Reference, item: str) -> None:
        """Warn that a `vulnerable` item compares the wrong way round, so it sets no risk level."""
        self._log(self._MESSAGE_INVERTED_VULNERABLE_ITEM, **self._reference_fields(reference, item=item))

    _MESSAGE_REDUNDANT_BOUND = LogMessage(
        WARNING, "Redundant update bound %(bound)s on %(dependency)s %(version)s in %(location)s: it %(redundancy)s"
    )

    def warn_if_redundant_bound(self, reference: Reference, marker: Marker) -> None:
        """Warn when the marker's version bound is redundant for the current version.

        The bound is redundant when it never has an effect or blocks every update (see `VersionBound.redundancy`).
        Does nothing when the reference has no bound or the bound is live, so callers can hand off every reference
        unconditionally.
        """
        if (bound := marker.version_bound) == NO_BOUND:
            return
        current_version = reference.current_version
        if (redundancy := bound.redundancy(current_version)) is None:
            return
        self._log(
            self._MESSAGE_REDUNDANT_BOUND,
            **self._reference_fields(reference, bound=bound, version=current_version, redundancy=redundancy),
        )

    # --- File scanning and selection ---

    _MESSAGE_CHECKING_PATH = LogMessage(DEBUG, "Checking if there are updates for %(location)s")

    def path(self, path: Path) -> None:
        """Log working on path."""
        self._log_file(self._MESSAGE_CHECKING_PATH, path)

    _MESSAGE_RECOGNISED_MARKER = LogMessage(
        DEBUG, "Recognised update-time marker %(directives)s for %(dependency)s in %(location)s"
    )

    def recognised_marker(self, dependency: str, marker: Marker, location: Location) -> None:
        """Log that a reference's marker was recognised, so users can confirm it was understood.

        Reports that the marker was read and parsed, not that it had any effect. The marker's directives are echoed
        verbatim (the `raw` text the user wrote), so a user comparing the log line against their file sees their own
        marker. Does nothing when the line carries no marker (the `raw` text is empty), so the caller can hand off
        every reference unconditionally.
        """
        if not marker.raw:
            return
        self._log(self._MESSAGE_RECOGNISED_MARKER, directives=marker, dependency=dependency, location=location)

    _MESSAGE_IGNORED = LogMessage(
        DEBUG, "Ignoring updates for %(dependency)s in %(location)s (update-time: %(directive)s)"
    )

    def ignored(self, dependency: str, marker: Marker, location: Location) -> None:
        """Log that a reference's update was held back by the marker's `ignore` directive, echoing it as written."""
        self._log_ignored(self._MESSAGE_IGNORED, dependency, marker, location)

    _MESSAGE_EXCLUDING_PATH = LogMessage(DEBUG, "Excluding %(path)s from the scan (--exclude-path)")

    def excluded_path(self, path: Path) -> None:
        """Log that a directory passed to `--exclude-path` is held back from the scan."""
        self._log(self._MESSAGE_EXCLUDING_PATH, path=path)

    _MESSAGE_PATH_TO_EXCLUDE_DOES_NOT_EXIST = LogMessage(
        WARNING, "Path %(path)s passed to --exclude-path does not exist"
    )

    def missing_excluded_path(self, path: Path) -> None:
        """Warn that a directory passed to `--exclude-path` does not exist, so it excludes nothing."""
        self._log(self._MESSAGE_PATH_TO_EXCLUDE_DOES_NOT_EXIST, path=path)

    _MESSAGE_SKIP_PATH = LogMessage(INFO, "Skipping %(location)s: %(reason)s")

    def skipped(self, path: Path, reason: str) -> None:
        """Log that a file was deliberately skipped without being checked for updates."""
        self._log_file(self._MESSAGE_SKIP_PATH, path, reason=reason)

    # --- Updater-specific diagnostics ---

    _MESSAGE_UV_COOLDOWN = LogMessage(
        INFO, "Set uv exclude-newer to %(cooldown)r in %(location)s to apply the cooldown"
    )

    def configured_uv_cooldown(self, path: Path, cooldown: str) -> None:
        """Log that Update-time wrote its cooldown into the project's uv configuration.

        The path is a workspace root, which can sit above the current directory (when Update-time runs inside a
        member), so fall back to the absolute path when it can't be made relative to the working directory.
        """
        self._log_file(self._MESSAGE_UV_COOLDOWN, path, cooldown=cooldown)

    _MESSAGE_SKIP_UNSUPPORTED = LogMessage(
        WARNING, "Skipping %(location)s: %(manager)s is not supported, only %(supported)s"
    )

    def unsupported_package_manager(self, path: Path, manager: str, supported: str) -> None:
        """Warn that a file is managed by an unsupported package manager, so its dependencies are left unchanged."""
        self._log_file(self._MESSAGE_SKIP_UNSUPPORTED, path, manager=manager, supported=supported)

    _MESSAGE_INVALID_TOML = LogMessage(WARNING, "Skipping %(location)s: it is not valid TOML")

    def invalid_pyproject_toml(self, path: Path) -> None:
        """Warn that a pyproject.toml can't be parsed as TOML, so it is skipped rather than crashing the run."""
        self._log_file(self._MESSAGE_INVALID_TOML, path)

    _MESSAGE_NON_NUMERIC_NODE_BASE_IMAGE_TAG = LogMessage(
        WARNING,
        "Cannot derive the Node engine version from the non-numeric base image tag 'node:%(tag)s' in %(location)s",
    )

    def non_numeric_node_base_image(self, dockerfile: Path, tag: str) -> None:
        """Log that the Node base image tag is not a concrete version, so the Node engine can't be derived."""
        self._log_file(self._MESSAGE_NON_NUMERIC_NODE_BASE_IMAGE_TAG, dockerfile, tag=tag)

    # --- HTTP fetching ---

    _MESSAGE_NOT_OK_RESPONSE = LogMessage(WARNING, "Could not fetch %(url)s: HTTP %(status)s %(reason)s")

    def response(self, response: Response) -> None:
        """Log a response's status code and reason phrase (e.g. `HTTP 404 Not Found`)."""
        self._log(self._MESSAGE_NOT_OK_RESPONSE, url=response.url, status=response.status_code, reason=response.reason)

    _MESSAGE_TIMEOUT = LogMessage(WARNING, "Timeout while fetching %(url)s")

    def timeout(self, url: str) -> None:
        """Log a request timeout."""
        self._log(self._MESSAGE_TIMEOUT, url=url)

    _MESSAGE_REQUEST_ERROR = LogMessage(WARNING, "Could not fetch %(url)s: %(error)s")

    def request_error(self, url: str, error: object) -> None:
        """Log a network error (connection failure, too many redirects, ...) while fetching a URL."""
        self._log(self._MESSAGE_REQUEST_ERROR, url=url, error=error)

    # --- External commands ---

    _MESSAGE_COMMAND_NOT_FOUND = LogMessage(ERROR, "Could not run %(command)s: is %(executable)s installed?")

    def command_not_found(self, command: Command) -> None:
        """Log that a command could not be run because its executable is not installed."""
        self._log(self._MESSAGE_COMMAND_NOT_FOUND, command=command, executable=command.executable)

    _MESSAGE_COMMAND_STDERR = LogMessage(WARNING, "%(command)s wrote to stderr:\n%(stderr)s")

    def command_stderr(self, command: Command, stderr: str) -> None:
        """Log that a command wrote to stderr, including what it wrote.

        The message stays neutral about severity because the tool decides that: its stderr may be an `[ERROR]`, a
        `[WARN]`, or just a notice (e.g. a pnpm deprecation). The warning level is Update-time's own view — this is
        only logged when the command failed or produced nothing usable (see `run`), so it is worth
        surfacing whatever the tool called it.
        """
        self._log(self._MESSAGE_COMMAND_STDERR, command=command, stderr=stderr)


# The loggers handed out so far, so their changelog-suppression state can be reset without hunting for the module
# constants holding them. A run resets nothing; the tests do, between test cases sharing this process.
_LOGGERS: list[Logger] = []


def reset_changelog_suppression() -> None:
    """Forget which changelogs were shown, so the next report of one shows it again.

    A logger suppresses a changelog it has already shown, which lasts as long as the logger does. The loggers are
    module constants, and so outlive a single test, hence this way of putting them back as they started.
    """
    for logger in _LOGGERS:
        logger.forget_shown_changelogs()


def get_logger(name: str) -> Logger:
    """Initialize a logger, configuring the root logger to send all diagnostics to stderr on the first call.

    Update-time's real output is the files it rewrites in place; everything it logs — the new-version report as much
    as the warnings and errors — is diagnostics about the run, so it all goes to stderr. That keeps stdout clean for
    the argparse-handled `--version`/`--help` output, so e.g. `v=$(update-time -V)` isn't polluted with log lines.
    `get_logger` is called once per module that owns a logger (an updater plus the sources it imports), so configure
    the root logger only the first time — when it has no handlers yet — instead of building a handler on every call.
    """
    if not logging.getLogger().handlers:
        configure_logging(Console(stderr=True, theme=LOG_THEME), LOG_LEVEL.get())
    logger = Logger(name)
    _LOGGERS.append(logger)
    return logger

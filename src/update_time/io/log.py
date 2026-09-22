"""Log helpers."""

import logging
from dataclasses import dataclass
from logging import DEBUG, ERROR, INFO, WARNING
from typing import TYPE_CHECKING

from rich.console import Console

from update_time.domain.bound import NO_BOUND
from update_time.domain.dependency import tag_of
from update_time.domain.staleness import stale_release
from update_time.io.console import CHANGES, LOG_THEME, NOTE, configure_logging, delimit_dependency, delimit_location
from update_time.markers.bound import spell
from update_time.markers.marker import Scope
from update_time.primitives.environment import EnvVar
from update_time.primitives.location import Location
from update_time.primitives.timestamp import days_since

if TYPE_CHECKING:
    from pathlib import Path

    from requests import Response

    from update_time.domain.dependency import AccountedFor, Changes, DependencyVersion, FloatingPin, VersionString
    from update_time.domain.reference import DriftedPin, Reference, ResolvedReference
    from update_time.domain.vulnerability import Vulnerability
    from update_time.markers.directive import Reason
    from update_time.markers.marker import Marker
    from update_time.primitives.command import Command


# The log levels that can be selected on the command line, and the default. Reporting an available new version is
# logged at INFO (it is the tool's regular output, not a problem), while genuinely unexpected situations stay at
# WARNING and failures at ERROR. The per-file "checking ..." progress is logged at DEBUG.
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# Private channel that passes the log level from the CLI to the updater subprocesses.
LOG_LEVEL = EnvVar("_UPDATE_TIME_LOG_LEVEL", default="INFO", parse=str)


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


@dataclass(frozen=True)
class _Check:
    """A check a reference's marker can silence: the scope silencing it, its warning, and what is logged instead."""

    scope: Scope
    warning: LogMessage
    ignored: LogMessage


@dataclass(frozen=True)
class _Drift:
    """A kind of hash pin that can drift: the warning that it did, and the report that the new value was adopted."""

    warning: LogMessage
    adopted: LogMessage


def _redundant_directive(reason: str) -> str:
    """Return a message saying that a marker has a redundant directive, for the given reason."""
    return f"Redundant update-time directive %(directive)s for %(dependency)s in %(location)s: {reason}"


def _ignoring(subject: str, cause: str = "update-time: %(directive)s") -> str:
    """Return a message saying what is being ignored, and why."""
    return f"Ignoring {subject} for %(dependency)s in %(location)s ({cause})"


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

    def _log_changes(self, message: LogMessage, changes: Changes, **fields: object) -> None:
        """Emit a log record at the message's own level, carrying a changelog's changes beside its fields."""
        self.log.log(message.level, message, self._rendered(fields), extra={CHANGES: changes})

    def _log_note(self, message: LogMessage, note: str, **fields: object) -> None:
        """Emit a log record at the message's own level, carrying Update-time's own note beside its fields."""
        self.log.log(message.level, message, self._rendered(fields), extra={NOTE: note})

    @classmethod
    def _rendered(cls, fields: dict[str, object]) -> dict[str, object]:
        """Return the fields with the ones the highlighter styles wrapped in their delimiter."""
        return {name: cls._render_field(name, value) for name, value in fields.items()}

    @classmethod
    def _render_field(cls, name: str, value: object) -> object:
        """Return the field's value, wrapped in its delimiter when the highlighter styles it as one token.

        A location is recognised by its type; a dependency has no type of its own, and its name has no fixed shape
        to match either, so the field's name identifies it instead.
        """
        if isinstance(value, Location):
            return delimit_location(value)
        if name == "dependency":
            return delimit_dependency(str(value))
        return value

    @staticmethod
    def _reference_fields(reference: Reference, **extra: object) -> dict[str, object]:
        """Return the fields every message about a reference carries, plus the ones the message reporting it adds."""
        return {"dependency": reference.dependency, "location": reference.location, **extra}

    def _log_ignored(
        self, message: LogMessage, dependency: str, directive: str, location: Location, **extra: object
    ) -> None:
        """Log that a marker held a reference's update back or silenced one of its warnings."""
        self._log(message, dependency=dependency, location=location, directive=directive, **extra)

    def _report(
        self, check: _Check, marker: Marker, resolved: ResolvedReference, fields: dict[str, object] | None
    ) -> None:
        """Report what a check found: its warning, or the marker that silenced it."""
        if fields is None:
            return
        if marker.ignores(check.scope):
            directive = marker.written_directive(check.scope)
            self._log_ignored(check.ignored, resolved.dependency, directive, resolved.location)
        else:
            self._log(check.warning, **fields)

    def _log_file(self, message: LogMessage, path: Path, **fields: object) -> None:
        """Log a message about a file the scan found.

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
        """Warn that Update-time is running outside a git repository because --force overrode the refusal to run."""
        self._log(self._MESSAGE_FORCED_OUTSIDE_GIT_REPOSITORY, path=path)

    # --- Source results: resolving a dependency's latest version and digest ---

    _MESSAGE_NEW_VERSION = LogMessage(INFO, "New version available for %(dependency)s in %(location)s: %(version)s")
    _SUPPRESSING_CHANGELOG = "Suppressing changelog already shown, see above"
    _NO_CHANGELOG = "No changelog available!"

    def new_version(self, reference: Reference, version: DependencyVersion) -> None:
        """Log the availability of a new version for a dependency in a file, with its UTC publication date if known."""
        dependency = reference.dependency
        shown_before = (dependency, version) in self._logged_changes
        self._logged_changes.add((dependency, version))
        fields = self._reference_fields(reference, version=str(version))
        if shown_before:
            self._log_note(self._MESSAGE_NEW_VERSION, self._SUPPRESSING_CHANGELOG, **fields)
        elif version.changes:
            self._log_changes(self._MESSAGE_NEW_VERSION, version.changes, **fields)
        else:
            self._log_note(self._MESSAGE_NEW_VERSION, self._NO_CHANGELOG, **fields)

    _MESSAGE_PINNED = LogMessage(INFO, "Pinned %(dependency)s in %(location)s to %(version)s@%(sha)s")

    def pinned(self, reference: Reference, version: DependencyVersion) -> None:
        """Log that a previously unpinned reference in a file was pinned to a digest, without changing its version."""
        self._log(self._MESSAGE_PINNED, **self._reference_fields(reference, version=version.version, sha=version.sha))

    _MESSAGE_KEEPING_FLOATING_TAG = LogMessage(
        DEBUG,
        "Keeping the floating tag %(dependency)s%(tag)s in %(location)s: it resolves to %(resolved)s@%(sha)s "
        "(%(cause)s)",
    )

    @classmethod
    def _tagged_fields(cls, reference: Reference, tag: VersionString) -> dict[str, object]:
        """Return the fields a report names a reference by when it names the tag attached to it as well."""
        return cls._reference_fields(reference, tag=tag_of(tag))

    def keeping_floating_tag(self, reference: Reference, release: DependencyVersion, cause: str) -> None:
        """Log that a floating tag was left as it is, naming the release it resolves to.

        A reference naming no tag is named by its image alone, the release naming what it resolves to already.
        """
        fields = self._tagged_fields(reference, reference.current_version)
        self._log(self._MESSAGE_KEEPING_FLOATING_TAG, **fields, resolved=release.version, sha=release.sha, cause=cause)

    _MESSAGE_UNPINNED_FLOATING_TAG = LogMessage(
        DEBUG,
        "Floating tag %(dependency)s%(tag)s in %(location)s was left as it is: %(reason)s",
    )

    def unpinned_floating_tag(self, reference: Reference, release: DependencyVersion, reason: FloatingPin) -> None:
        """Log that a floating tag was pinned to no version, naming which of the reasons left it as it is.

        The tag named is the one the source looked up. A reference naming none means `latest`, which the source
        reports back as the release it resolved, nothing having been pinned in its place.
        """
        fields = self._tagged_fields(reference, reference.current_version or release.version)
        self._log(self._MESSAGE_UNPINNED_FLOATING_TAG, **fields, reason=reason)

    _MESSAGE_ACCOUNTED_FOR_REFERENCE = LogMessage(
        DEBUG,
        "Reference %(dependency)s%(tag)s in %(location)s was left as it is: %(reason)s",
    )

    def accounted_for_reference(self, reference: Reference, reason: AccountedFor) -> None:
        """Log that a reference the file itself accounts for was left as it is, explaining why."""
        fields = self._tagged_fields(reference, reference.current_version)
        self._log(self._MESSAGE_ACCOUNTED_FOR_REFERENCE, **fields, reason=reason)

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

    _MESSAGE_ADOPTED_DIGEST_DRIFT = LogMessage(
        INFO,
        "Adopted digest drift for %(dependency)s:%(version)s in %(location)s: "
        "re-pinned from %(current_sha)s to %(new_sha)s (%(cause)s)",
    )

    DIGEST_DRIFT = _Drift(_MESSAGE_DIGEST_DRIFT, _MESSAGE_ADOPTED_DIGEST_DRIFT)

    _MESSAGE_TAG_DRIFT = LogMessage(
        WARNING,
        "Tag drift for %(dependency)s@%(version)s in %(location)s: pinned to commit %(current_sha)s but the tag now "
        "points at %(new_sha)s; the pin was left unchanged, verify the tag was moved deliberately before updating "
        "the pin",
    )

    _MESSAGE_ADOPTED_TAG_DRIFT = LogMessage(
        INFO,
        "Adopted tag drift for %(dependency)s@%(version)s in %(location)s: "
        "re-pinned from commit %(current_sha)s to %(new_sha)s (%(cause)s)",
    )

    TAG_DRIFT = _Drift(_MESSAGE_TAG_DRIFT, _MESSAGE_ADOPTED_TAG_DRIFT)

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

    def drift(self, kind: _Drift, drifted: DriftedPin) -> None:
        """Warn that a hash pin no longer matches what it points at, and was left unchanged."""
        self._log(kind.warning, **self._drift_fields(drifted))

    def adopted_drift(self, kind: _Drift, drifted: DriftedPin, cause: str) -> None:
        """Log that what a hash pin now points at was adopted because the reference opted in.

        `cause` names the opt-in that triggered the adoption.
        """
        self._log(kind.adopted, **self._drift_fields(drifted, cause=cause))

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

    _MESSAGE_IGNORED_STALENESS = LogMessage(DEBUG, _ignoring("the staleness warning"))

    _STALENESS = _Check(Scope.STALE, _MESSAGE_STALE, _MESSAGE_IGNORED_STALENESS)

    @classmethod
    def _stale_fields(cls, resolved: ResolvedReference, threshold: int) -> dict[str, object] | None:
        """Return the staleness warning's fields, or None when the newest release is not old enough to warn about."""
        if (newest := stale_release(resolved.release, threshold)) is None:
            return None
        return cls._reference_fields(
            resolved, version=newest.version, days=days_since(newest.published), threshold=threshold
        )

    def report_staleness(self, resolved: ResolvedReference, marker: Marker, threshold: int) -> None:
        """Report the reference's staleness: a warning, or the marker that silenced it."""
        self._report(self._STALENESS, marker, resolved, self._stale_fields(resolved, threshold))

    _MESSAGE_YANKED = LogMessage(
        WARNING, "Yanked dependency %(dependency)s in %(location)s: version %(version)s was yanked (%(reason)s)"
    )

    _MESSAGE_IGNORED_YANK = LogMessage(DEBUG, _ignoring("the yank warning"))

    _YANK = _Check(Scope.YANKED, _MESSAGE_YANKED, _MESSAGE_IGNORED_YANK)

    @classmethod
    def _yank_fields(cls, resolved: ResolvedReference) -> dict[str, object] | None:
        """Return the yank warning's fields, or None when the version the run leaves the reference on stands.

        The reason is the yank itself, which renders as the maintainer's words where they gave any.
        """
        release = resolved.release
        if not release.yank.yanked:
            return None
        return cls._reference_fields(resolved, version=release.version, reason=release.yank)

    def report_yank(self, resolved: ResolvedReference, marker: Marker) -> None:
        """Report the version's yank: a warning, or the marker that silenced it."""
        self._report(self._YANK, marker, resolved, self._yank_fields(resolved))

    _MESSAGE_ARCHIVED = LogMessage(
        WARNING, "Archived dependency %(dependency)s in %(location)s: the %(subject)s was archived%(reason)s"
    )

    _MESSAGE_IGNORED_ARCHIVAL = LogMessage(DEBUG, _ignoring("the archival warning"))

    _ARCHIVAL = _Check(Scope.ARCHIVED, _MESSAGE_ARCHIVED, _MESSAGE_IGNORED_ARCHIVAL)

    @classmethod
    def _archival_fields(cls, resolved: ResolvedReference) -> dict[str, object] | None:
        """Return the archival warning's fields, or None when the source declares nothing archived."""
        archival = resolved.release.project.archival
        if not archival.archived:
            return None
        reason = f' ("{archival.reason}")' if archival.reason else ""
        return cls._reference_fields(resolved, subject=archival.subject, reason=reason)

    def report_archival(self, resolved: ResolvedReference, marker: Marker) -> None:
        """Report the reference's archival: a warning, or the marker that silenced it."""
        self._report(self._ARCHIVAL, marker, resolved, self._archival_fields(resolved))

    _MESSAGE_MALFORMED_CVSS_VECTOR = LogMessage(
        WARNING,
        "Could not score the CVSS vector of advisory %(advisory)s (%(error)s), so it is reported at unknown severity",
    )

    def malformed_cvss_vector(self, advisory: str, error: Exception) -> None:
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

    _MESSAGE_IGNORED_VULNERABILITY = LogMessage(DEBUG, _ignoring("the %(advisory)s vulnerability warning"))

    def ignored_vulnerability(self, reference: Reference, vulnerability: Vulnerability, marker: Marker) -> None:
        """Log that the marker silenced a vulnerability warning, naming the advisory."""
        directive = marker.written_directive(Scope.VULNERABLE)
        self._log_ignored(
            self._MESSAGE_IGNORED_VULNERABILITY,
            reference.dependency,
            directive,
            reference.location,
            advisory=vulnerability.advisory,
        )

    _MESSAGE_GLOBALLY_IGNORED_VULNERABILITY = LogMessage(
        DEBUG, _ignoring("the %(advisory)s vulnerability warning", "--ignore-vulnerability %(identifiers)s")
    )

    def globally_ignored_vulnerability(
        self, reference: Reference, vulnerability: Vulnerability, passed: frozenset[str]
    ) -> None:
        """Log that the run-wide option silenced a vulnerability warning, naming the advisory and what was passed.

        The option takes its identifiers comma-separated, so several of them read back as a value it accepts.
        """
        identifiers = ",".join(sorted(passed))
        fields = self._reference_fields(reference, advisory=vulnerability.advisory, identifiers=identifiers)
        self._log(self._MESSAGE_GLOBALLY_IGNORED_VULNERABILITY, **fields)

    def _redundant_suppression(
        self, message: LogMessage, reference: Reference, directive: str, **extra: object
    ) -> None:
        """Warn that a vulnerability suppression silences nothing for the version the reference pins.

        The directive is the caller's, since each of these messages judges one form of the `vulnerable` scope and
        the forms beside it may silence plenty.
        """
        fields = self._reference_fields(reference, directive=directive, version=reference.current_version)
        self._log(message, **fields, **extra)

    _MESSAGE_REDUNDANT_VULNERABLE_SCOPE = LogMessage(
        WARNING, _redundant_directive("version %(version)s has no vulnerability")
    )

    def redundant_vulnerable_scope(self, reference: Reference, marker: Marker) -> None:
        """Warn that the marker's vulnerability scope found no vulnerability to silence for the pinned version."""
        directive = marker.scope_directive(Scope.VULNERABLE)
        self._redundant_suppression(self._MESSAGE_REDUNDANT_VULNERABLE_SCOPE, reference, directive)

    _MESSAGE_REDUNDANT_VULNERABLE_ADVISORY = LogMessage(
        WARNING, _redundant_directive("version %(version)s has no such vulnerability")
    )

    def redundant_vulnerable_advisory(self, reference: Reference, marker: Marker) -> None:
        """Warn that the marker names an advisory none of the pinned version's vulnerabilities answers to."""
        directive = marker.advisory_directives
        self._redundant_suppression(self._MESSAGE_REDUNDANT_VULNERABLE_ADVISORY, reference, directive)

    _MESSAGE_REDUNDANT_VULNERABLE_LEVEL = LogMessage(
        WARNING, _redundant_directive("version %(version)s has no vulnerability below %(level)s")
    )

    def redundant_vulnerable_level(self, reference: Reference, marker: Marker, level: str) -> None:
        """Warn that the marker's risk level left no vulnerability of the pinned version below it to silence."""
        directive = marker.vulnerable.directive
        self._redundant_suppression(self._MESSAGE_REDUNDANT_VULNERABLE_LEVEL, reference, directive, level=level)

    _MESSAGE_REDUNDANT_DIRECTIVE = LogMessage(WARNING, _redundant_directive("%(reason)s"))

    def redundant_directive(self, reference: Reference, directive: str, reason: Reason) -> None:
        """Warn that a directive the marker carries decides nothing, saying why it cannot."""
        self._log(
            self._MESSAGE_REDUNDANT_DIRECTIVE, **self._reference_fields(reference, directive=directive, reason=reason)
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

    _MESSAGE_NO_ENTRY = LogMessage(
        WARNING, "Could not find where %(location)s declares %(dependency)s, leaving it unchanged"
    )

    def no_entry(self, dependency: str, path: Path) -> None:
        """Warn that a file declares the dependency but the entry declaring it cannot be found in the file."""
        self._log_file(self._MESSAGE_NO_ENTRY, path, dependency=dependency)

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

    def _inverted_stale_item(self, reference: Reference, item: str) -> None:
        """Warn that a `stale` item compares the wrong way round, so it sets no threshold."""
        self._log(self._MESSAGE_INVERTED_STALE_ITEM, **self._reference_fields(reference, item=item))

    _MESSAGE_INVERTED_COOLDOWN_ITEM = LogMessage(
        WARNING,
        _INVERTED_ITEM + "adopts a release only while it is fresh and holds it back once it is old, so it sets no "
        "cooldown",
    )

    def _inverted_cooldown_item(self, reference: Reference, item: str) -> None:
        """Warn that a `cooldown` item compares the wrong way round, so it sets no cooldown."""
        self._log(self._MESSAGE_INVERTED_COOLDOWN_ITEM, **self._reference_fields(reference, item=item))

    _MESSAGE_INVERTED_VULNERABLE_ITEM = LogMessage(
        WARNING,
        _INVERTED_ITEM + "warns about the mild vulnerabilities and stays quiet about the severe ones, so it sets no "
        "risk level",
    )

    def _inverted_vulnerable_item(self, reference: Reference, item: str) -> None:
        """Warn that a `vulnerable` item compares the wrong way round, so it sets no risk level."""
        self._log(self._MESSAGE_INVERTED_VULNERABLE_ITEM, **self._reference_fields(reference, item=item))

    def report_inverted_items(self, reference: Reference, marker: Marker) -> None:
        """Report each of the marker's comparison items whose operator runs the wrong way, so it sets nothing."""
        inverted_items = (
            (marker.stale, self._inverted_stale_item),
            (marker.cooldown, self._inverted_cooldown_item),
            (marker.vulnerable, self._inverted_vulnerable_item),
        )
        for threshold, warn in inverted_items:
            if threshold.inverted_item is not None:
                warn(reference, threshold.inverted_item)

    _MESSAGE_REDUNDANT_BOUND = LogMessage(
        WARNING, "Redundant update bound %(bound)s on %(dependency)s %(version)s in %(location)s: it %(redundancy)s"
    )

    def warn_if_redundant_bound(self, reference: Reference, marker: Marker) -> None:
        """Warn when the marker's version bound is redundant for the current version.

        The bound is redundant when it never has an effect or blocks every update (see `VersionBound.redundancy`).
        """
        if (bound := marker.version_bound) == NO_BOUND:
            return
        current_version = reference.current_version
        if (redundancy := bound.redundancy(current_version)) is None:
            return
        self._log(
            self._MESSAGE_REDUNDANT_BOUND,
            **self._reference_fields(reference, bound=spell(bound), version=current_version, redundancy=redundancy),
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

        The marker's directives are echoed verbatim, the `raw` text the user wrote, so a user comparing the log
        line against their file sees their own marker.
        """
        if not marker.raw:
            return
        self._log(self._MESSAGE_RECOGNISED_MARKER, directives=marker, dependency=dependency, location=location)

    _MESSAGE_IGNORED = LogMessage(DEBUG, _ignoring("updates"))

    def ignored(self, dependency: str, marker: Marker, location: Location) -> None:
        """Log that a reference's update was held back, naming the `ignore` directive that held it back.

        A bare `ignore` names no scope, so it is echoed as the user wrote it rather than spelled out as
        `ignore[update]`, a directive they never typed.
        """
        directive = marker.written_directive(Scope.UPDATE)
        self._log_ignored(self._MESSAGE_IGNORED, dependency, directive, location)

    _MESSAGE_EXCLUDING_PATH = LogMessage(DEBUG, "Excluding %(path)s from the scan (--exclude-path)")

    def excluded_path(self, path: Path) -> None:
        """Log that a directory passed to `--exclude-path` is excluded from the scan."""
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
        """Log that Update-time wrote its cooldown into the project's uv configuration."""
        self._log_file(self._MESSAGE_UV_COOLDOWN, path, cooldown=cooldown)

    _MESSAGE_SKIP_UNSUPPORTED = LogMessage(
        WARNING, "Skipping %(location)s: %(manager)s is not supported, only %(supported)s"
    )

    def unsupported_package_manager(self, path: Path, manager: str, supported: str) -> None:
        """Warn that a file is managed by an unsupported package manager, so its dependencies are left unchanged."""
        self._log_file(self._MESSAGE_SKIP_UNSUPPORTED, path, manager=manager, supported=supported)

    _MESSAGE_INVALID_FILE = LogMessage(WARNING, "Skipping %(location)s: it is not valid %(format)s")

    def invalid_file(self, path: Path, file_format: str) -> None:
        """Warn that a file can't be parsed, so it is skipped rather than crashing the run."""
        self._log_file(self._MESSAGE_INVALID_FILE, path, format=file_format)

    _MESSAGE_INVALID_POM = LogMessage(WARNING, "Could not read the pom at %(url)s: it is not valid XML")

    def invalid_pom(self, url: str) -> None:
        """Warn that a pom a repository served cannot be parsed, so what it declares goes unread."""
        self._log(self._MESSAGE_INVALID_POM, url=url)

    _MESSAGE_INVALID_XML_AFTER_UPDATE = LogMessage(
        ERROR, "Could not read %(location)s after updating it: it is not valid XML"
    )

    def invalid_xml_after_update(self, path: Path) -> None:
        """Report that the file an updater rewrote cannot be parsed, so its update failed rather than being skipped."""
        self._log_file(self._MESSAGE_INVALID_XML_AFTER_UPDATE, path)

    _MESSAGE_DECLARATIONS_CHANGED = LogMessage(
        ERROR,
        "Could not tell what changed in %(location)s: it declared %(before)s dependencies before the update and "
        "%(after)s after it",
    )

    def declarations_changed(self, path: Path, before: int, after: int) -> None:
        """Report that the file an updater rewrote declares more or fewer dependencies than before the update."""
        self._log_file(self._MESSAGE_DECLARATIONS_CHANGED, path, before=before, after=after)

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
        """Log that a URL could not be fetched, because the source answered it with an error."""
        self._log_response(self._MESSAGE_NOT_OK_RESPONSE, response)

    _MESSAGE_UNSERVED_CHANGELOG = LogMessage(
        DEBUG, "Could not fetch the changelog at %(url)s: HTTP %(status)s %(reason)s"
    )

    def unserved_changelog(self, response: Response) -> None:
        """Log that a changelog URL a project publishes is not served."""
        self._log_response(self._MESSAGE_UNSERVED_CHANGELOG, response)

    _MESSAGE_UNSERVED_LISTING = LogMessage(
        DEBUG, "Could not fetch the version listing at %(url)s: HTTP %(status)s %(reason)s"
    )

    def unserved_listing(self, response: Response) -> None:
        """Log that a repository does not serve a version listing for an artefact."""
        self._log_response(self._MESSAGE_UNSERVED_LISTING, response)

    def _log_response(self, message: LogMessage, response: Response) -> None:
        """Log the message with the response's URL, status code, and reason phrase."""
        self._log(message, url=response.url, status=response.status_code, reason=response.reason)

    _MESSAGE_TIMEOUT = LogMessage(WARNING, "Timeout while fetching %(url)s")

    def timeout(self, url: str) -> None:
        """Log a request timeout."""
        self._log(self._MESSAGE_TIMEOUT, url=url)

    _MESSAGE_REQUEST_ERROR = LogMessage(WARNING, "Could not fetch %(url)s: %(error)s")

    def request_error(self, url: str, error: Exception) -> None:
        """Log a network error (connection failure, too many redirects, ...) while fetching a URL."""
        self._log(self._MESSAGE_REQUEST_ERROR, url=url, error=error)

    # --- External commands ---

    _MESSAGE_COMMAND_NOT_FOUND = LogMessage(ERROR, "Could not run %(command)s: is %(executable)s installed?")

    def command_not_found(self, command: Command) -> None:
        """Log that a command could not be run because its executable is not installed."""
        self._log(self._MESSAGE_COMMAND_NOT_FOUND, command=command, executable=command.executable)

    _MESSAGE_COMMAND_ERROR = LogMessage(ERROR, "Could not run %(command)s: %(error)s")

    def command_error(self, command: Command, error: OSError) -> None:
        """Log that the system refused to run a command."""
        self._log(self._MESSAGE_COMMAND_ERROR, command=command, error=error)

    _MESSAGE_COMMAND_FAILED = LogMessage(ERROR, "%(command)s failed:\n%(output)s")

    def command_failed(self, command: Command, output: str) -> None:
        """Log that a command exited non-zero, including the output it produced."""
        self._log(self._MESSAGE_COMMAND_FAILED, command=command, output=output)

    _MESSAGE_COMMAND_STDERR = LogMessage(WARNING, "%(command)s wrote to stderr:\n%(stderr)s")

    def command_stderr(self, command: Command, stderr: str) -> None:
        """Log that a command wrote to stderr, including what it wrote.

        The message stays neutral about severity because the tool decides that: its stderr may be an `[ERROR]`, a
        `[WARN]`, or just a notice (e.g. a pnpm deprecation).
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
    """
    if not logging.getLogger().handlers:
        configure_logging(Console(stderr=True, theme=LOG_THEME), LOG_LEVEL.get())
    logger = Logger(name)
    _LOGGERS.append(logger)
    return logger

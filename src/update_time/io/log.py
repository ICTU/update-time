"""Log helpers."""

import logging
import shlex
from dataclasses import dataclass
from logging import DEBUG, ERROR, INFO, WARNING
from signal import SIGINT, SIGQUIT, Signals
from typing import TYPE_CHECKING

from rich.console import Console

from update_time.domain.bound import NO_BOUND
from update_time.domain.cooldown import cooldown_end
from update_time.domain.dependency import tag_of
from update_time.domain.staleness import stale_release
from update_time.io.console import CHANGES, LOG_THEME, NOTE, configure_logging, delimit_dependency, delimit_location
from update_time.markers.bound import spell
from update_time.markers.marker import Scope
from update_time.primitives.environment import EnvVar
from update_time.primitives.location import Location
from update_time.primitives.timestamp import days_since, utc_date_and_time

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from requests import Response

    from update_time.domain.dependency import (
        Changes,
        DependencyVersion,
        FloatingPin,
        ImageNotLookedUp,
        Release,
        VersionString,
    )
    from update_time.domain.reference import DriftedPin, Reference, RefKind, ResolvedReference
    from update_time.domain.vulnerability import Vulnerability
    from update_time.markers.directive import RedundancyReason
    from update_time.markers.marker import Marker
    from update_time.markers.opt_in import OptInCause
    from update_time.primitives.command import Command


# The log levels that can be selected on the command line, and the default. Reporting an available new version is
# logged at INFO (it is the tool's regular output, not a problem), while genuinely unexpected situations stay at
# WARNING and failures at ERROR. The per-file "checking ..." progress is logged at DEBUG.
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# Private channel that passes the log level from the CLI to the updater subprocesses.
LOG_LEVEL = EnvVar("_UPDATE_TIME_LOG_LEVEL", default="INFO", parse=str)

# The log names the key beside the signal a terminal sends for it.
_KEYS = {SIGINT: "Ctrl-C", SIGQUIT: "Ctrl-\\"}


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
class _SetBy:
    """Two forms of one report: for a setting from the run, and for one from a marker, which the report names."""

    run: LogMessage
    marker: LogMessage


@dataclass(frozen=True)
class Drift:
    """A kind of hash pin that can drift: the warning that it did, and the report that it was adopted.

    Both messages name the reference as its dependency, then the separator and the version it records, if any.
    """

    warning: LogMessage
    adopted: LogMessage
    separator: str


def _redundant_directive(reason: str) -> str:
    """Return a message saying that a marker has a redundant directive, for the given reason."""
    return f"Redundant update-time directive %(directive)s for %(dependency)s in %(location)s: {reason}"


def _effective_pom_unused(problem: str, cause: str) -> str:
    """Return a message saying why the effective pom went unused, what the run missed, and what may cause it."""
    return (
        f"{problem}, so the cooldown and the checks passed over any group or artifact the pom leaves to its parent, "
        f"and the vulnerability check passed over any version the pom leaves to another pom or to Maven; {cause}"
    )


# The parts of the errors about a reactor Maven cannot read.
_CANNOT_READ_THE_REACTOR = "Maven cannot read the reactor in %(location)s"
_SINCE_UPDATE = " since Update-time updated %(updated)s"
_AS_FOUND = " as the scan found it"
_POMS_LEFT = ", so Update-time does not run Maven over the %(poms)s left in it"
_OTHER_POMS = ", so Update-time does not run Maven over its %(poms)s"
_FIX = "; revert that pom and hold the update that broke the reactor back with a marker, or fix what Maven reports"
_UPDATE_TIME_CANNOT_READ_THE_REACTOR = "Update-time cannot read the reactor in %(location)s"
_SINCE_UNPARSABLE = " since %(unparsable)s does not parse"


def _poms(count: int, *qualifiers: str) -> str:
    """Return the count of poms, with the qualifiers between, such as `1 pom` or `2 other poms`."""
    return " ".join((str(count), *qualifiers, "pom" if count == 1 else "poms"))


def _ignoring(subject: str, cause: str = "update-time: %(directive)s") -> str:
    """Return a message saying what is being ignored, and why."""
    return f"Ignoring {subject} for %(dependency)s in %(location)s ({cause})"


def _commit_drift(ref: str, moved: str, advice: str) -> str:
    """Return a message warning that a tag, a branch, or a ref moved to another commit, saying how and advising."""
    return (
        f"{ref.capitalize()} drift for %(dependency)s%(tag)s in %(location)s: pinned to commit %(current_sha)s but "
        f"the {ref} {moved} %(new_sha)s; the pin was left unchanged, verify {advice} before updating the pin"
    )


def _undated(source: str, event: str, in_between: str) -> str:
    """Return a message saying the source could not date a candidate, so the reference stays on its version."""
    return (
        f"Could not determine from {source} when %(dependency)s{event} (%(reason)s), so the cooldown can't be "
        f"verified; {_stays_on(in_between)}"
    )


def _stays_on(in_between: str) -> str:
    """Return the clause saying the reference stays on its version, since a candidate stopped the walk."""
    return f"%(dependency)s stays on %(current)s rather than trying the {in_between} in between"


def _adopted_commit_drift(ref: str) -> str:
    """Return a message saying that the commit a tag, a branch, or a ref moved to was adopted."""
    return (
        f"Adopted {ref} drift for %(dependency)s%(tag)s in %(location)s: "
        "re-pinned from commit %(current_sha)s to %(new_sha)s (%(cause)s)"
    )


class Logger:
    """Wrap a logger and add update specific log methods."""

    def __init__(self, name: str) -> None:
        """Initialize the logger."""
        self.log = logging.getLogger(name)
        self._logged_changes: set[tuple[str, DependencyVersion]] = set()

    def logs_debug(self) -> bool:
        """Return whether the logger shows debug messages."""
        return self.log.isEnabledFor(DEBUG)

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

    # --- The run ---

    _MESSAGE_RUNNING = LogMessage(DEBUG, "Running Update-time v%(version)s on %(platform)s with Python %(python)s")

    def running(self, version: str, platform: str, python: str) -> None:
        """Log the version of Update-time, and the OS and Python version it runs on."""
        self._log(self._MESSAGE_RUNNING, version=version, platform=platform, python=python)

    _MESSAGE_ARGUMENTS = LogMessage(DEBUG, "Command-line arguments: %(arguments)s")

    def arguments(self, arguments: Sequence[str]) -> None:
        """Log the command-line arguments, quoted as a shell reads them."""
        self._log(self._MESSAGE_ARGUMENTS, arguments=shlex.join(arguments) or "none")

    _MESSAGE_CREDENTIALS = LogMessage(DEBUG, "Credentials: %(credentials)s")

    def credentials(self, credentials: Mapping[str, bool]) -> None:
        """Log which of the credential environment variables are set."""
        listed = ", ".join(f"{name} {'set' if is_set else 'not set'}" for name, is_set in credentials.items())
        self._log(self._MESSAGE_CREDENTIALS, credentials=listed)

    _MESSAGE_NETWORK_SETTINGS = LogMessage(DEBUG, "Network settings: %(settings)s")

    def network_settings(self, proxies: Mapping[str, str], no_proxy: str, ca_bundle: str) -> None:
        """Log the proxies, the hosts they skip, and the CA bundle."""
        listed = ", ".join(f"{scheme}={proxy}" for scheme, proxy in proxies.items())
        if not listed:
            connections = "direct connections"
        elif no_proxy:
            connections = f"proxies {listed}, except for {no_proxy}"
        else:
            connections = f"proxies {listed}"
        certificates = f"CA bundle {ca_bundle}" if ca_bundle else "the default CA bundle"
        self._log(self._MESSAGE_NETWORK_SETTINGS, settings=f"{connections}; {certificates}")

    _MESSAGE_STOPPED = LogMessage(
        ERROR,
        "%(signal)s stopped Update-time before it finished; review the files it changed, since a file it was writing "
        "may be incomplete",
    )

    def stopped(self, signal: Signals) -> None:
        """Log that a signal stopped the run before it finished."""
        name = f"{signal.name} ({key})" if (key := _KEYS.get(signal)) else signal.name
        self._log(self._MESSAGE_STOPPED, signal=name)

    _MESSAGE_UPDATE_STOPPED = LogMessage(ERROR, "Could not finish updating %(dependency_type)s: %(signal)s stopped it")

    def update_stopped(self, dependency_type: str, signal: Signals) -> None:
        """Log that a signal stopped the update of a dependency type before it finished."""
        self._log(self._MESSAGE_UPDATE_STOPPED, dependency_type=dependency_type, signal=signal.name)

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

    _HELD_BACK_BY_COOLDOWN = (
        "Holding back version %(version)s of %(dependency)s in %(location)s: published %(published)s, within the "
        "cooldown of %(days)d days until %(until)s"
    )
    _MESSAGE_HELD_BACK_BY_COOLDOWN = LogMessage(DEBUG, _HELD_BACK_BY_COOLDOWN)
    _MESSAGE_HELD_BACK_BY_MARKER_COOLDOWN = LogMessage(DEBUG, f"{_HELD_BACK_BY_COOLDOWN} (update-time: %(directive)s)")

    _HELD_BACK = _SetBy(_MESSAGE_HELD_BACK_BY_COOLDOWN, _MESSAGE_HELD_BACK_BY_MARKER_COOLDOWN)

    def held_back_by_cooldown(self, reference: Reference, fresh: Release, days: int, directive: str) -> None:
        """Log a release held back by the cooldown, naming the marker's directive where a marker sets the cooldown."""
        fields = self._reference_fields(
            reference,
            version=fresh.version,
            published=utc_date_and_time(fresh.published),
            days=days,
            until=utc_date_and_time(cooldown_end(fresh.published, days)),
        )
        if directive:
            self._log(self._HELD_BACK.marker, **fields, directive=directive)
        else:
            self._log(self._HELD_BACK.run, **fields)

    _MESSAGE_PINNED = LogMessage(INFO, "Pinned %(dependency)s in %(location)s to %(version)s@%(sha)s")

    def pinned(self, reference: Reference, version: DependencyVersion) -> None:
        """Log that a reference was pinned to a hash, named by the version or the ref it pins."""
        self._log(self._MESSAGE_PINNED, **self._reference_fields(reference, version=version.version, sha=version.sha))

    _MESSAGE_PINNED_VERSION = LogMessage(INFO, "Pinned %(dependency)s in %(location)s to %(version)s")

    def pinned_version(self, reference: Reference, version: DependencyVersion) -> None:
        """Log that a floating reference was pinned to the version it serves, without a hash."""
        self._log(self._MESSAGE_PINNED_VERSION, **self._reference_fields(reference, version=version.version))

    _MESSAGE_KEEPING_FLOATING_TAG = LogMessage(
        DEBUG,
        "Keeping the floating tag %(dependency)s%(tag)s in %(location)s: it resolves to %(resolved)s@%(sha)s "
        "(%(cause)s)",
    )

    @classmethod
    def _tagged_fields(cls, reference: Reference, tag: VersionString) -> dict[str, object]:
        """Return the fields a report names a reference by when it names the tag attached to it as well."""
        return cls._reference_fields(reference, tag=tag_of(tag))

    def keeping_floating_tag(self, reference: Reference, release: DependencyVersion, cause: OptInCause) -> None:
        """Log that a floating tag was left as it is, naming the release it resolves to.

        A reference naming no tag is named by its image alone, the release naming what it resolves to already.
        """
        fields = self._tagged_fields(reference, reference.current_version)
        self._log(self._MESSAGE_KEEPING_FLOATING_TAG, **fields, resolved=release.version, sha=release.sha, cause=cause)

    _MESSAGE_KEEPING_REF = LogMessage(
        DEBUG,
        "Keeping the %(ref)s %(dependency)s@%(name)s in %(location)s: it resolves to %(resolved)s@%(sha)s (%(cause)s)",
    )

    def keeping_ref(self, reference: Reference, release: DependencyVersion, cause: OptInCause, kind: RefKind) -> None:
        """Log that a reference following a branch, a tag, or a ref was kept floating, and what it resolves to."""
        fields = self._reference_fields(reference, ref=kind, name=reference.current_version)
        self._log(self._MESSAGE_KEEPING_REF, **fields, resolved=release.version, sha=release.sha, cause=cause)

    _MESSAGE_UNPINNED_REF = LogMessage(
        ERROR,
        "Could not fetch the commit of the %(ref)s %(dependency)s@%(name)s in %(location)s, so it was left as it is "
        "(%(reason)s)",
    )

    def unpinned_ref(self, reference: Reference, reason: str, kind: RefKind) -> None:
        """Log that a reference naming a ref or a short commit SHA was left as it is, and why its commit is unknown."""
        fields = self._reference_fields(reference, ref=kind, name=reference.current_version, reason=reason)
        self._log(self._MESSAGE_UNPINNED_REF, **fields)

    _MESSAGE_UNCHECKED_BRANCH_DRIFT = LogMessage(
        ERROR,
        "Could not check branch drift for %(dependency)s@%(name)s in %(location)s: GitHub did not compare the pinned "
        "commit with %(new_sha)s, the branch's newest commit older than the cooldown (%(reason)s); the pin was left "
        "unchanged",
    )

    def unchecked_branch_drift(self, reference: Reference, new_sha: str, reason: str) -> None:
        """Log that a branch's drift could not be checked, saying why, so its pin was left unchanged."""
        fields = self._reference_fields(reference, name=reference.current_version, new_sha=new_sha, reason=reason)
        self._log(self._MESSAGE_UNCHECKED_BRANCH_DRIFT, **fields)

    _MESSAGE_COMMENT_NAMING_NO_REF = LogMessage(
        DEBUG,
        "Commit SHA of %(dependency)s in %(location)s was left as it is: its comment %(name)s does not name a branch "
        "or tag of the repository",
    )

    def comment_naming_no_ref(self, reference: Reference) -> None:
        """Log that a commit SHA was left as it is, since the word its comment holds does not name a branch or tag."""
        fields = self._reference_fields(reference, name=reference.current_version)
        self._log(self._MESSAGE_COMMENT_NAMING_NO_REF, **fields)

    _MESSAGE_UNPINNED_FLOATING_TAG = LogMessage(
        DEBUG,
        "Floating tag %(dependency)s%(tag)s in %(location)s was left as it is: %(reason)s",
    )

    def unpinned_floating_tag(self, reference: Reference, release: DependencyVersion, reason: FloatingPin) -> None:
        """Log that a floating tag was pinned to no version, naming which of the reasons left it as it is.

        A reference that does not name a tag is named by its image alone, as the file writes it. The reason names
        the tag the source looked up, which is the release it reports back.
        """
        fields = self._tagged_fields(reference, reference.current_version)
        self._log(self._MESSAGE_UNPINNED_FLOATING_TAG, **fields, reason=reason.explained(release.version))

    _MESSAGE_IMAGE_NOT_LOOKED_UP = LogMessage(
        DEBUG,
        "Reference %(dependency)s%(tag)s in %(location)s was left as it is: %(reason)s",
    )

    def image_not_looked_up(self, reference: Reference, reason: ImageNotLookedUp) -> None:
        """Log that a reference whose image Update-time does not look up was left as it is, explaining why."""
        fields = self._tagged_fields(reference, reference.current_version)
        self._log(self._MESSAGE_IMAGE_NOT_LOOKED_UP, **fields, reason=reason)

    _MESSAGE_REPOSITORY_NOT_SERVED = LogMessage(
        DEBUG,
        "Reference %(dependency)s%(tag)s in %(location)s was left as it is: the registry does not serve the image's "
        "repository",
    )

    def repository_not_served(self, reference: Reference) -> None:
        """Log that a reference was left as it is, because its registry does not serve the image's repository."""
        self._log(self._MESSAGE_REPOSITORY_NOT_SERVED, **self._tagged_fields(reference, reference.current_version))

    _MESSAGE_SUBSTITUTED_REFERENCE = LogMessage(
        DEBUG,
        "Reference %(dependency)s%(tag)s in %(location)s is %(substituted)s with its build arguments substituted; it "
        "does not get a digest, since a digest would overrule a --build-arg",
    )

    def substituted_reference(self, reference: Reference, substituted: str) -> None:
        """Log a reference naming build arguments as Docker builds on it, and why it does not get a digest."""
        fields = self._tagged_fields(reference, reference.current_version)
        self._log(self._MESSAGE_SUBSTITUTED_REFERENCE, **fields, substituted=substituted)

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
        "Digest drift for %(dependency)s%(tag)s in %(location)s: pinned to %(current_sha)s but the registry "
        "now serves %(new_sha)s; the pin was left unchanged, verify the change is expected before updating the pin",
    )

    _MESSAGE_ADOPTED_DIGEST_DRIFT = LogMessage(
        INFO,
        "Adopted digest drift for %(dependency)s%(tag)s in %(location)s: "
        "re-pinned from %(current_sha)s to %(new_sha)s (%(cause)s)",
    )

    DIGEST_DRIFT = Drift(_MESSAGE_DIGEST_DRIFT, _MESSAGE_ADOPTED_DIGEST_DRIFT, ":")

    _MESSAGE_TAG_DRIFT = LogMessage(WARNING, _commit_drift("tag", "now points at", "the tag was moved deliberately"))
    _MESSAGE_ADOPTED_TAG_DRIFT = LogMessage(INFO, _adopted_commit_drift("tag"))
    TAG_DRIFT = Drift(_MESSAGE_TAG_DRIFT, _MESSAGE_ADOPTED_TAG_DRIFT, "@")

    # A branch's drift names its newest commit older than the cooldown, which the branch may have moved on from already.
    _MESSAGE_BRANCH_DRIFT = LogMessage(
        WARNING, _commit_drift("branch", "has moved on to", "the branch moved to a commit you trust")
    )
    _MESSAGE_ADOPTED_BRANCH_DRIFT = LogMessage(INFO, _adopted_commit_drift("branch"))
    BRANCH_DRIFT = Drift(_MESSAGE_BRANCH_DRIFT, _MESSAGE_ADOPTED_BRANCH_DRIFT, "@")

    _MESSAGE_REF_DRIFT = LogMessage(
        WARNING, _commit_drift("ref", "now points at", "the ref moved to a commit you trust")
    )
    _MESSAGE_ADOPTED_REF_DRIFT = LogMessage(INFO, _adopted_commit_drift("ref"))
    REF_DRIFT = Drift(_MESSAGE_REF_DRIFT, _MESSAGE_ADOPTED_REF_DRIFT, "@")

    @staticmethod
    def _drift_fields(kind: Drift, drifted: DriftedPin, **extra: object) -> dict[str, object]:
        """Return the fields every drift message carries, plus the ones the message reporting it adds."""
        return Logger._drifted_reference_fields(
            kind, drifted, current_sha=drifted.current_sha, new_sha=drifted.new_sha, **extra
        )

    @staticmethod
    def _drifted_reference_fields(kind: Drift, drifted: DriftedPin, **extra: object) -> dict[str, object]:
        """Return the fields naming the drifted reference, plus the ones the message reporting it adds."""
        return Logger._reference_fields(drifted, tag=tag_of(drifted.current_version, kind.separator), **extra)

    def drift(self, kind: Drift, drifted: DriftedPin) -> None:
        """Warn that a hash pin no longer matches what it points at, and was left unchanged."""
        self._log(kind.warning, **self._drift_fields(kind, drifted))

    def adopted_drift(self, kind: Drift, drifted: DriftedPin, cause: OptInCause) -> None:
        """Log that what a hash pin now points at was adopted because the reference opted in."""
        self._log(kind.adopted, **self._drift_fields(kind, drifted, cause=cause))

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

    _MESSAGE_UNREADABLE_AFFECTED_VERSIONS = LogMessage(
        WARNING,
        "Could not read the affected versions of advisory %(advisory)s (%(error)s), so the vulnerability check "
        "passes over that part of the advisory",
    )

    def unreadable_affected_versions(self, advisory: str, error: Exception) -> None:
        """Warn that the affected versions an advisory states do not all parse."""
        self._log(self._MESSAGE_UNREADABLE_AFFECTED_VERSIONS, advisory=advisory, error=error)

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

    def redundant_directive(self, reference: Reference, directive: str, reason: RedundancyReason) -> None:
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

    _MESSAGE_NO_RELEASE_METADATA = LogMessage(
        ERROR,
        "Could not fetch from PyPI the metadata of %(dependency)s %(candidate)s (%(reason)s), so its yank state and "
        f"publication date are unknown; {_stays_on('versions')}",
    )

    def no_release_metadata(self, dependency: str, candidate: str, current: str, reason: str) -> None:
        """Log that a release's metadata couldn't be fetched, and why, so the reference stays on its version."""
        self._log_stopped_walk(self._MESSAGE_NO_RELEASE_METADATA, dependency, candidate, current, reason)

    _MESSAGE_UNDATED_PUSH = LogMessage(ERROR, _undated("Docker Hub", ":%(candidate)s was pushed", "tags"))

    def undated_push(self, dependency: str, candidate: str, current: str, reason: str) -> None:
        """Log that an image tag couldn't be dated, and why, so the reference stays on its tag."""
        self._log_stopped_walk(self._MESSAGE_UNDATED_PUSH, dependency, candidate, current, reason)

    _MESSAGE_UNDATED_TAGGED_COMMIT = LogMessage(ERROR, _undated("GitHub", " %(candidate)s was committed", "versions"))

    def undated_commit(self, dependency: str, candidate: str, current: str, reason: str) -> None:
        """Log that the commit a version tag points at couldn't be dated, and why, so the reference stays put."""
        self._log_stopped_walk(self._MESSAGE_UNDATED_TAGGED_COMMIT, dependency, candidate, current, reason)

    def _log_stopped_walk(
        self, message: LogMessage, dependency: str, candidate: str, current: str, reason: str
    ) -> None:
        """Log why a candidate stopped the walk, naming the candidate and the version kept."""
        self._log(message, dependency=dependency, candidate=candidate, current=current, reason=reason)

    _MESSAGE_NO_COMMIT_DATE = LogMessage(
        ERROR,
        "Could not determine from GitHub the date of commit %(new_sha)s that %(dependency)s%(tag)s in %(location)s "
        "now points at (%(reason)s), so the cooldown can't be verified; not adopting the drift",
    )

    def no_commit_date(self, kind: Drift, drifted: DriftedPin, reason: str) -> None:
        """Log that the commit a drifted pin moved to couldn't be dated, and why, so the drift was not adopted."""
        fields = self._drifted_reference_fields(kind, drifted, new_sha=drifted.new_sha, reason=reason)
        self._log(self._MESSAGE_NO_COMMIT_DATE, **fields)

    _MESSAGE_NO_PUSH_DATE = LogMessage(
        ERROR,
        "Could not determine from Docker Hub when %(dependency)s%(tag)s in %(location)s was pushed (%(reason)s), "
        "so the cooldown can't be verified; not adopting the drift",
    )

    def no_push_date(self, kind: Drift, drifted: DriftedPin, reason: str) -> None:
        """Log that the re-pushed tag of a drifted digest couldn't be dated, and why, so the drift was not adopted."""
        self._log(self._MESSAGE_NO_PUSH_DATE, **self._drifted_reference_fields(kind, drifted, reason=reason))

    _MESSAGE_UNREADABLE_PUBLICATION_DATE = LogMessage(
        WARNING,
        "Could not read the publication date '%(date)s' that Maven Central lists for %(dependency)s %(version)s, "
        "so the cooldown holds this version back",
    )

    def unreadable_publication_date(self, dependency: str, version: str, date: str) -> None:
        """Warn that a listed version's date does not parse, so the cooldown holds that version back."""
        self._log(self._MESSAGE_UNREADABLE_PUBLICATION_DATE, dependency=dependency, version=version, date=date)

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

    _MESSAGE_HOLDING_BACK_RANGE = LogMessage(
        DEBUG, "Holding back the versions %(range)s of %(dependency)s in %(location)s (update-time: %(directive)s)"
    )

    def holding_back_range(self, reference: Reference, maven_range: str, directive: str) -> None:
        """Log the range of versions a bound holds the reference back from, as the package manager reads it."""
        self._log(
            self._MESSAGE_HOLDING_BACK_RANGE,
            **self._reference_fields(reference, range=maven_range, directive=directive),
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
        """Log that a reference's update was held back, naming the directive that held it back."""
        self._log_ignored(self._MESSAGE_IGNORED, dependency, marker.update_directive, location)

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

    _MESSAGE_NO_DEPENDENCIES = LogMessage(
        DEBUG, "Not running %(manager)s for %(location)s: it does not declare dependencies for %(manager)s to %(verb)s"
    )

    def no_dependencies(self, path: Path, manager: str, verb: str) -> None:
        """Log that the package manager does not run for a file that declares nothing for it to install or resolve."""
        self._log_file(self._MESSAGE_NO_DEPENDENCIES, path, manager=manager, verb=verb)

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

    _MESSAGE_EFFECTIVE_POM_UNREADABLE = LogMessage(
        WARNING,
        _effective_pom_unused(
            "Could not read the effective pom of %(location)s",
            "perhaps a --quiet in the project's Maven config hid where Maven wrote it",
        ),
    )

    def effective_pom_unreadable(self, path: Path) -> None:
        """Warn that the effective pom cannot be read, so the checks and the cooldown miss what it resolves."""
        self._log_file(self._MESSAGE_EFFECTIVE_POM_UNREADABLE, path)

    _MESSAGE_EFFECTIVE_POM_WITHOUT_INPUT_LOCATIONS = LogMessage(
        WARNING,
        _effective_pom_unused(
            "The effective pom of %(location)s lacks the input locations Update-time reads",
            "the project's help plugin configuration sets verbose to false",
        ),
    )

    def effective_pom_without_input_locations(self, path: Path) -> None:
        """Warn that the effective pom lacks input locations, so it resolves nothing."""
        self._log_file(self._MESSAGE_EFFECTIVE_POM_WITHOUT_INPUT_LOCATIONS, path)

    _MESSAGE_DECLARATIONS_CHANGED = LogMessage(
        ERROR,
        "Could not tell what changed in %(location)s: it declared %(before)s dependencies and plugins before the "
        "update and %(after)s after it",
    )

    def declarations_changed(self, path: Path, before: int, after: int) -> None:
        """Report that the file an updater rewrote declares more or fewer dependencies and plugins than before."""
        self._log_file(self._MESSAGE_DECLARATIONS_CHANGED, path, before=before, after=after)

    _MESSAGE_UNREADABLE_REACTOR = LogMessage(ERROR, _CANNOT_READ_THE_REACTOR + _POMS_LEFT)

    def unreadable_reactor(self, root: Path, poms_left: int) -> None:
        """Report that Maven cannot read the reactor in the root directory, naming how many of its poms are left."""
        self._log_file(self._MESSAGE_UNREADABLE_REACTOR, root, poms=_poms(poms_left))

    _MESSAGE_UNREADABLE_REACTOR_AS_FOUND = LogMessage(ERROR, _CANNOT_READ_THE_REACTOR + _AS_FOUND + _OTHER_POMS)

    def unreadable_reactor_as_found(self, root: Path, poms_left: int) -> None:
        """Report that Maven cannot read the reactor as the scan found it, naming how many other poms it holds."""
        self._log_file(self._MESSAGE_UNREADABLE_REACTOR_AS_FOUND, root, poms=_poms(poms_left, "other"))

    _MESSAGE_UNREADABLE_REACTOR_SINCE_UPDATE = LogMessage(
        ERROR, _CANNOT_READ_THE_REACTOR + _SINCE_UPDATE + _POMS_LEFT + _FIX
    )

    def unreadable_reactor_since_update(self, root: Path, updated: Path, poms_left: int) -> None:
        """Report that Maven cannot read the reactor since Update-time updated the pom, naming the poms left."""
        self._log_file(
            self._MESSAGE_UNREADABLE_REACTOR_SINCE_UPDATE, root, updated=Location(updated), poms=_poms(poms_left)
        )

    _MESSAGE_UNREADABLE_REACTOR_SINCE_UPDATE_WITHOUT_POMS_LEFT = LogMessage(
        ERROR, _CANNOT_READ_THE_REACTOR + _SINCE_UPDATE + _FIX
    )

    def unreadable_reactor_since_update_without_poms_left(self, root: Path, updated: Path) -> None:
        """Report that Maven cannot read the reactor since Update-time updated the pom."""
        self._log_file(self._MESSAGE_UNREADABLE_REACTOR_SINCE_UPDATE_WITHOUT_POMS_LEFT, root, updated=Location(updated))

    _MESSAGE_UNPARSABLE_REACTOR = LogMessage(
        ERROR, _UPDATE_TIME_CANNOT_READ_THE_REACTOR + _SINCE_UNPARSABLE + _OTHER_POMS
    )

    def unparsable_reactor(self, root: Path, unparsable: Path, other_poms: int) -> None:
        """Report that Update-time cannot read the reactor since the pom does not parse, naming its other poms."""
        self._log_file(
            self._MESSAGE_UNPARSABLE_REACTOR, root, unparsable=Location(unparsable), poms=_poms(other_poms, "other")
        )

    _MESSAGE_MAVEN_NOT_RUN = LogMessage(
        DEBUG,
        "Not running Maven over %(location)s: an earlier Maven run could not read the reactor this pom is part of",
    )

    def maven_not_run(self, path: Path) -> None:
        """Log that Maven does not run over the pom, because it cannot read the pom's reactor."""
        self._log_file(self._MESSAGE_MAVEN_NOT_RUN, path)

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

    _MESSAGE_GITHUB_RATE_LIMIT_USED_UP = LogMessage(
        ERROR,
        "GitHub's API rate limit of %(limit)s requests is used up until %(reset)s; set GITHUB_TOKEN to raise it",
    )

    def github_rate_limit_used_up(self, limit: int, reset: datetime) -> None:
        """Report that the anonymous GitHub rate limit is used up until the reset."""
        self._log(self._MESSAGE_GITHUB_RATE_LIMIT_USED_UP, limit=limit, reset=utc_date_and_time(reset))

    _MESSAGE_GITHUB_TOKEN_RATE_LIMIT_USED_UP = LogMessage(
        ERROR, "GitHub's API rate limit of %(limit)s requests for GITHUB_TOKEN is used up until %(reset)s"
    )

    def github_token_rate_limit_used_up(self, limit: int, reset: datetime) -> None:
        """Report that the GitHub rate limit of the token is used up until the reset."""
        self._log(self._MESSAGE_GITHUB_TOKEN_RATE_LIMIT_USED_UP, limit=limit, reset=utc_date_and_time(reset))

    _MESSAGE_UNREADABLE_GITHUB_RATE_LIMIT = LogMessage(DEBUG, "Could not read GitHub's rate-limit headers: %(error)s")

    def unreadable_github_rate_limit(self, error: KeyError | ValueError) -> None:
        """Log that GitHub's rate-limit headers could not be read, and why."""
        self._log(self._MESSAGE_UNREADABLE_GITHUB_RATE_LIMIT, error=str(error))

    # --- External commands ---

    _MESSAGE_TOOL_VERSION = LogMessage(DEBUG, "Version of %(executable)s in %(directory)s: %(version)s")

    def tool_version(self, executable: str, directory: Path, version: str) -> None:
        """Log the version an executable reports in a directory."""
        self._log(
            self._MESSAGE_TOOL_VERSION,
            executable=executable,
            directory=Location(directory),
            version=version,
        )

    _MESSAGE_TOOL_VERSION_UNKNOWN = LogMessage(
        WARNING, "Could not determine the version of %(executable)s in %(directory)s: %(reason)s"
    )

    def tool_version_unknown(self, executable: str, directory: Path, reason: str) -> None:
        """Warn that an executable reports no version in a directory, and why."""
        self._log(
            self._MESSAGE_TOOL_VERSION_UNKNOWN,
            executable=executable,
            directory=Location(directory),
            reason=reason,
        )

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


def _configure_root_logger() -> None:
    """Send all diagnostics to stderr, at the run's log level.

    Update-time's real output is the files it rewrites in place; everything it logs — the new-version report as much
    as the warnings and errors — is diagnostics about the run, so it all goes to stderr. That keeps stdout clean for
    the argparse-handled `--version`/`--help` output, so e.g. `v=$(update-time -V)` isn't polluted with log lines.
    """
    configure_logging(Console(stderr=True, theme=LOG_THEME), LOG_LEVEL.get())


def set_log_level(level: str) -> None:
    """Log at the level from now on, in this process and in the processes it starts."""
    LOG_LEVEL.set(level)
    _configure_root_logger()


def get_logger(name: str) -> Logger:
    """Initialize a logger, configuring the root logger on the first call."""
    if not logging.getLogger().handlers:
        _configure_root_logger()
    logger = Logger(name)
    _LOGGERS.append(logger)
    return logger


def report_marker(
    log: Logger, dependency: str, marker: Marker, location: Location, *, holds_the_update_back: bool
) -> Marker:
    """Report a reference's marker to the log, and return it, frozen where an item is invalid.

    An invalid item may have been meant to hold the update back.
    """
    if marker.invalid_item is not None:
        log.invalid_bracket_item(dependency, marker.invalid_item, location)
        return marker.frozen
    log.recognised_marker(dependency, marker, location)
    if holds_the_update_back:
        log.ignored(dependency, marker, location)
    return marker

"""The logger Update-time reports through, the messages it logs, and the configuration of the root logger."""

import logging
import re
import shlex
from dataclasses import dataclass
from functools import wraps
from logging import DEBUG, ERROR, INFO, WARNING
from signal import SIGINT, SIGQUIT, Signals
from typing import TYPE_CHECKING, Concatenate, cast

from rich.console import Console

from update_time.domain.bound import NO_BOUND
from update_time.domain.cooldown import cooldown_end
from update_time.domain.dependency import tag_of
from update_time.domain.reference import Reference, RefKind
from update_time.domain.staleness import STALE_AFTER, stale_release
from update_time.domain.vulnerability import VULNERABILITY_LEVEL
from update_time.io.console import CHANGES, LOG_THEME, NOTE, configure_logging, delimit_dependency, delimit_location
from update_time.markers.bound import spell
from update_time.markers.directive import Reason
from update_time.markers.marker import Scope
from update_time.primitives.environment import EnvVar
from update_time.primitives.location import Location
from update_time.primitives.text import prose_list
from update_time.primitives.timestamp import days_since, utc_date_and_time

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from requests import Response

    from update_time.domain.dependency import (
        DependencyVersion,
        FloatingPin,
        ImageNotLookedUp,
        Release,
        VersionString,
    )
    from update_time.domain.reference import DriftedPin, ResolvedReference
    from update_time.domain.vulnerability import Vulnerability
    from update_time.markers.directive import RedundancyReason
    from update_time.markers.marker import Marker, Threshold
    from update_time.primitives.command import Command


# The log levels that can be selected on the command line.
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
        """Return the format string's repr, so a failing assertion shows the text of the message."""
        return repr(self.format)

    @property
    def holes(self) -> set[str]:
        """Return the names of the fields the format string interpolates."""
        return set(re.findall(r"%\((\w+)\)", self.format))


type _Fields = dict[str, object] | None
type _FieldsMethod[**P] = Callable[Concatenate[Logger, P], _Fields]
type _LogMethod[**P] = Callable[Concatenate[Logger, P], None]

# The console renders these fields below a record's line, rather than in its message.
_EXTRAS = (CHANGES, NOTE)


def _debug[**P](format_string: str) -> Callable[[_FieldsMethod[P]], _LogMethod[P]]:
    """Return a decorator adding the message with the format at debug level to a log method."""
    return _logs_at(DEBUG, format_string)


def _info[**P](format_string: str) -> Callable[[_FieldsMethod[P]], _LogMethod[P]]:
    """Return a decorator adding the message with the format at info level to a log method."""
    return _logs_at(INFO, format_string)


def _warning[**P](format_string: str) -> Callable[[_FieldsMethod[P]], _LogMethod[P]]:
    """Return a decorator adding the message with the format at warning level to a log method."""
    return _logs_at(WARNING, format_string)


def _error[**P](format_string: str) -> Callable[[_FieldsMethod[P]], _LogMethod[P]]:
    """Return a decorator adding the message with the format at error level to a log method."""
    return _logs_at(ERROR, format_string)


def _logs_at[**P](level: int, format_string: str) -> Callable[[_FieldsMethod[P]], _LogMethod[P]]:
    """Return a decorator adding the message with the format at the level to a log method.

    The log method logs the message with the fields the decorated method returns, if any. Decorators stack: an outer
    one adds its message to the log method the inner one made, which then logs the message whose holes its fields
    fill.
    """
    message = LogMessage(level, format_string)

    def decorate(method: _FieldsMethod[P]) -> _LogMethod[P]:
        if (stacked := vars(method).get("messages")) is not None:
            stacked.append(message)
            return cast("_LogMethod[P]", method)  # An inner decorator made it a log method already
        messages = [message]

        @wraps(method)
        def log_method(self: Logger, *args: P.args, **kwargs: P.kwargs) -> None:
            if (fields := method(self, *args, **kwargs)) is not None:
                extra = {name: value for name, value in fields.items() if name in _EXTRAS}
                filling = {name: value for name, value in fields.items() if name not in _EXTRAS}
                self._emit(_filled_by(messages, filling), filling, extra)

        vars(log_method)["messages"] = messages
        return log_method

    return decorate


def _filled_by(messages: list[LogMessage], fields: dict[str, object]) -> LogMessage:
    """Return the one message whose holes are the fields, or the method's only message, whatever its holes."""
    if len(messages) == 1:
        return messages[0]
    (filled,) = (message for message in messages if message.holes == fields.keys())
    return filled


@dataclass(frozen=True)
class Drift:
    """A kind of hash pin that can drift: the separator before the version it records, and the words naming it.

    A commit drifts as the tag, the branch, or the ref pointing at it moves, so its kind names the ref, how it moved,
    and what to verify. A digest's kind names none of them.
    """

    separator: str
    ref: RefKind | None = None
    moved: str = ""
    advice: str = ""

    @property
    def warning_words(self) -> dict[str, str]:
        """Return the words the warning about this kind of drift fills in."""
        return (
            {"Ref": self.ref.capitalize(), "ref": self.ref, "moved": self.moved, "advice": self.advice}
            if self.ref
            else {}
        )

    @property
    def adopted_words(self) -> dict[str, str]:
        """Return the words the report that this kind of drift was adopted fills in."""
        return {"ref": self.ref} if self.ref else {}


def _redundant_directive(reason: str) -> str:
    """Return a message saying that a marker has a redundant directive, for the given reason."""
    return f"Redundant update-time directive %(directive)s for %(dependency)s in %(location)s: {reason}"


def _run_setting_field[T](name: str, setting: EnvVar[T], value: T) -> dict[str, T]:
    """Return the field naming the run's value: `default_<name>` if it is the setting's default, else `run_<name>`."""
    return {f"default_{name}" if value == setting.default else f"run_{name}": value}


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


def _undated(source: str, event: str, in_between: str) -> str:
    """Return a message saying the source could not date a candidate, so the reference stays on its version."""
    return (
        f"Could not determine from {source} when %(dependency)s{event} (%(reason)s), so the cooldown can't be "
        f"verified; {_stays_on(in_between)}"
    )


def _stays_on(in_between: str) -> str:
    """Return the clause saying the reference stays on its version, since a candidate stopped the walk."""
    return f"%(dependency)s stays on %(current)s rather than trying the {in_between} in between"


class Logger:
    """A named logger, with a method per message Update-time logs."""

    def __init__(self, name: str) -> None:
        """Start with no changelog shown yet."""
        self.log = logging.getLogger(name)
        self._logged_changes: set[tuple[str, DependencyVersion]] = set()

    def logs_debug(self) -> bool:
        """Return whether the logger shows debug messages."""
        return self.log.isEnabledFor(DEBUG)

    def forget_shown_changelogs(self) -> None:
        """Forget which changelogs were shown, so reporting one of them again shows it rather than suppressing it."""
        self._logged_changes.clear()

    def _emit(self, message: LogMessage, fields: dict[str, object], extra: dict[str, object]) -> None:
        """Emit a log record at the message's own level, carrying the extra beside its fields where there is any."""
        if extra:
            self.log.log(message.level, message, self._rendered(fields), extra=extra)
        else:
            self.log.log(message.level, message, self._rendered(fields))

    @classmethod
    def _rendered(cls, fields: dict[str, object]) -> dict[str, object]:
        """Return the fields with the ones the highlighter styles wrapped in their delimiter."""
        return {name: cls._render_field(name, value) for name, value in fields.items()}

    @classmethod
    def _render_field(cls, name: str, value: object) -> object:
        """Return the field's value, wrapped in its delimiter when the highlighter styles it as one token.

        A tuple of locations renders as a list of such tokens, as a sentence lists them.

        A location is recognised by its type; a dependency has no type of its own, and its name has no fixed shape
        to match either, so the field's name identifies it instead.
        """
        if isinstance(value, Location):
            return delimit_location(value)
        if isinstance(value, tuple) and value and all(isinstance(item, Location) for item in value):
            return prose_list([delimit_location(location) for location in value])
        if name == "dependency":
            return delimit_dependency(str(value))
        return value

    @staticmethod
    def _reference_fields(reference: Reference, **extra: object) -> dict[str, object]:
        """Return the fields every message about a reference carries, plus the ones the message reporting it adds."""
        return {"dependency": reference.dependency, "location": reference.location, **extra}

    @staticmethod
    def _ignored_fields(dependency: str, directive: str, location: Location, **extra: object) -> dict[str, object]:
        """Return the fields of a message that a marker held an update back or silenced a warning."""
        return {"dependency": dependency, "location": location, "directive": directive, **extra}

    @classmethod
    def _check_fields(
        cls,
        scope: Scope,
        marker: Marker,
        resolved: ResolvedReference,
        warning_fields: dict[str, object] | None,
        redundancy_fields: Mapping[str, object] | None,
    ) -> dict[str, object] | None:
        """Return the fields of what a check found: its warning's, the silencing marker's, or the redundant marker's.

        `redundancy_fields` are what the redundancy warning adds, and None where the check did not answer.
        """
        if warning_fields is None:
            if redundancy_fields is None or not marker.ignores(scope):
                return None
            return cls._reference_fields(resolved, directive=marker.scope_directive(scope), **redundancy_fields)
        if marker.ignores(scope):
            return cls._ignored_fields(resolved.dependency, marker.directive_for(scope), resolved.location)
        return warning_fields

    @staticmethod
    def _file_fields(path: Path, **fields: object) -> dict[str, object]:
        """Return the fields of a message about a file the scan found, plus the ones the message adds.

        Wrapping the path in a `Location` is what makes it one of those files, reported relative to the working
        directory and styled as a single token. A path the user named on the command line stays a plain `Path` instead
        (see `_render_field`).
        """
        return {"location": Location(path), **fields}

    # --- The run ---

    @_debug("Running Update-time v%(version)s on %(platform)s with Python %(python)s")
    def running(self, version: str, platform: str, python: str) -> dict[str, object]:
        """Log the version of Update-time, and the OS and Python version it runs on."""
        return {"version": version, "platform": platform, "python": python}

    @_debug("Command-line arguments: %(arguments)s")
    def arguments(self, arguments: Sequence[str]) -> dict[str, object]:
        """Log the command-line arguments, quoted as a shell reads them."""
        return {"arguments": shlex.join(arguments) or "none"}

    @_debug("Credentials: %(credentials)s")
    def credentials(self, credentials: Mapping[str, bool]) -> dict[str, object]:
        """Log which of the credential environment variables are set."""
        listed = ", ".join(f"{name} {'set' if is_set else 'not set'}" for name, is_set in credentials.items())
        return {"credentials": listed}

    @_debug("Network settings: %(settings)s")
    def network_settings(self, proxies: Mapping[str, str], no_proxy: str, ca_bundle: str) -> dict[str, object]:
        """Log the proxies, the hosts they skip, and the CA bundle."""
        listed = ", ".join(f"{scheme}={proxy}" for scheme, proxy in proxies.items())
        if not listed:
            connections = "direct connections"
        elif no_proxy:
            connections = f"proxies {listed}, except for {no_proxy}"
        else:
            connections = f"proxies {listed}"
        certificates = f"CA bundle {ca_bundle}" if ca_bundle else "the default CA bundle"
        return {"settings": f"{connections}; {certificates}"}

    @_error(
        "%(signal)s stopped Update-time before it finished; review the files it changed, since a file it was writing "
        "may be incomplete"
    )
    def stopped(self, signal: Signals) -> dict[str, object]:
        """Log that a signal stopped the run."""
        name = f"{signal.name} ({key})" if (key := _KEYS.get(signal)) else signal.name
        return {"signal": name}

    @_error("Could not finish updating %(dependency_type)s: %(signal)s stopped it")
    def update_stopped(self, dependency_type: str, signal: Signals) -> dict[str, object]:
        """Log that a signal stopped the update of a dependency type."""
        return {"dependency_type": dependency_type, "signal": signal.name}

    @_warning(
        "Running outside a git repository (%(path)s) because --force was given; changes are made in place and cannot "
        "be reverted"
    )
    def forced_outside_git_repository(self, path: Path) -> dict[str, object]:
        """Warn that --force overrode the refusal to run outside a git repository."""
        return {"path": path}

    # --- A reference: its update, its pin, its drift, its checks, and its marker ---

    _SUPPRESSING_CHANGELOG = "Suppressing changelog already shown, see above"
    _NO_CHANGELOG = "No changelog available!"

    @_info("New version available for %(dependency)s in %(location)s: %(version)s")
    def new_version(self, reference: Reference, version: DependencyVersion) -> dict[str, object]:
        """Log a new version for a reference, with its changelog."""
        dependency = reference.dependency
        shown_before = (dependency, version) in self._logged_changes
        self._logged_changes.add((dependency, version))
        fields = self._reference_fields(reference, version=str(version))
        if shown_before:
            return {**fields, NOTE: self._SUPPRESSING_CHANGELOG}
        if version.changes:
            return {**fields, CHANGES: version.changes}
        return {**fields, NOTE: self._NO_CHANGELOG}

    _RANGE_REWRITE = "%(manager)s rewrote the range of %(dependency)s in %(location)s from %(range)s to %(new_range)s"

    @_info(f"{_RANGE_REWRITE}; the version in the lock file is unchanged at %(version)s")
    @_info(f"{_RANGE_REWRITE}; the lock file did not record a version before")
    def range_rewrite(
        self, manager: str, reference: Reference, new_range: str, version: VersionString
    ) -> dict[str, object]:
        """Log that a package manager rewrote the range a reference declares, with the version locked before if any."""
        fields = self._reference_fields(
            reference, manager=manager, range=reference.current_version, new_range=new_range
        )
        return {**fields, "version": version} if version else fields

    _HELD_BACK_BY_COOLDOWN = (
        "Holding back version %(version)s of %(dependency)s in %(location)s: published %(published)s, within the "
        "cooldown of %(days)d days until %(until)s"
    )

    @_debug(f"{_HELD_BACK_BY_COOLDOWN} (update-time: %(directive)s)")
    @_debug(_HELD_BACK_BY_COOLDOWN)
    def held_back_by_cooldown(
        self, reference: Reference, fresh: Release, days: int, directive: str
    ) -> dict[str, object]:
        """Log that the cooldown holds a release back."""
        fields = self._reference_fields(
            reference,
            version=fresh.version,
            published=utc_date_and_time(fresh.published),
            days=days,
            until=utc_date_and_time(cooldown_end(fresh.published, days)),
        )
        return {**fields, "directive": directive} if directive else fields

    @_info("Pinned %(dependency)s in %(location)s to %(version)s@%(sha)s")
    def pinned(self, reference: Reference, version: DependencyVersion) -> dict[str, object]:
        """Log that a reference was pinned to a hash."""
        return self._reference_fields(reference, version=version.version, sha=version.sha)

    @_info("Pinned %(dependency)s in %(location)s to %(version)s")
    def pinned_version(self, reference: Reference, version: DependencyVersion) -> dict[str, object]:
        """Log that a floating reference was pinned to a version alone."""
        return self._reference_fields(reference, version=version.version)

    @classmethod
    def _tagged_fields(cls, reference: Reference) -> dict[str, object]:
        """Return the fields a report names a reference by when it names the tag attached to it as well."""
        return cls._reference_fields(reference, tag=tag_of(reference.current_version))

    @_debug(
        "Keeping the floating tag %(dependency)s%(tag)s in %(location)s: it resolves to %(resolved)s@%(sha)s "
        "(update-time: %(directive)s)"
    )
    def keeping_floating_tag(
        self, reference: Reference, release: DependencyVersion, directive: str
    ) -> dict[str, object]:
        """Log that a floating tag was kept floating.

        A reference naming no tag is named by its image alone, the release naming what it resolves to already.
        """
        fields = self._tagged_fields(reference)
        return {**fields, "resolved": release.version, "sha": release.sha, "directive": directive}

    @_debug(
        "Keeping the %(ref)s %(dependency)s@%(name)s in %(location)s: it resolves to %(resolved)s@%(sha)s "
        "(update-time: %(directive)s)"
    )
    def keeping_ref(
        self, reference: Reference, release: DependencyVersion, directive: str, kind: RefKind
    ) -> dict[str, object]:
        """Log that a branch, a tag, or a ref was kept floating."""
        fields = self._reference_fields(reference, ref=kind, name=reference.current_version)
        return {**fields, "resolved": release.version, "sha": release.sha, "directive": directive}

    @_error(
        "Could not fetch the commit of the %(ref)s %(dependency)s@%(name)s in %(location)s, so it was left as it is "
        "(%(reason)s)"
    )
    def unpinned_ref(self, reference: Reference, reason: str, kind: RefKind) -> dict[str, object]:
        """Log that the commit of a ref or a short commit SHA could not be fetched."""
        return self._reference_fields(reference, ref=kind, name=reference.current_version, reason=reason)

    @_error(
        "Could not check branch drift for %(dependency)s@%(name)s in %(location)s: GitHub did not compare the pinned "
        "commit with %(new_sha)s, the branch's newest commit older than the cooldown (%(reason)s); the pin was left "
        "unchanged"
    )
    def unchecked_branch_drift(self, reference: Reference, new_sha: str, reason: str) -> dict[str, object]:
        """Log that a branch's drift could not be checked."""
        return self._reference_fields(reference, name=reference.current_version, new_sha=new_sha, reason=reason)

    @_debug(
        "Commit SHA of %(dependency)s in %(location)s was left as it is: its comment %(name)s does not name a branch "
        "or tag of the repository"
    )
    def comment_naming_no_ref(self, reference: Reference) -> dict[str, object]:
        """Log that the comment beside a commit SHA does not name a branch or tag."""
        return self._reference_fields(reference, name=reference.current_version)

    @_debug("Floating tag %(dependency)s%(tag)s in %(location)s was left as it is: %(reason)s")
    def unpinned_floating_tag(
        self, reference: Reference, release: DependencyVersion, reason: FloatingPin
    ) -> dict[str, object]:
        """Log that a floating tag was left as it is.

        A reference that does not name a tag is named by its image alone, as the file writes it. The reason names
        the tag the source looked up, which is the release it reports back.
        """
        fields = self._tagged_fields(reference)
        return {**fields, "reason": reason.explained(release.version)}

    @_debug("Reference %(dependency)s%(tag)s in %(location)s was left as it is: %(reason)s")
    def image_not_looked_up(self, reference: Reference, reason: ImageNotLookedUp) -> dict[str, object]:
        """Log that Update-time does not look up a reference's image."""
        fields = self._tagged_fields(reference)
        return {**fields, "reason": reason}

    @_debug(
        "Reference %(dependency)s%(tag)s in %(location)s was left as it is: the registry does not serve the image's "
        "repository"
    )
    def repository_not_served(self, reference: Reference) -> dict[str, object]:
        """Log that the registry does not serve a reference's repository."""
        return self._tagged_fields(reference)

    @_debug(
        "Reference %(dependency)s%(tag)s in %(location)s is %(substituted)s with its build arguments substituted; it "
        "does not get a digest, since a digest would overrule a --build-arg"
    )
    def substituted_reference(self, reference: Reference, substituted: str) -> dict[str, object]:
        """Log a reference naming build arguments as Docker builds on it."""
        fields = self._tagged_fields(reference)
        return {**fields, "substituted": substituted}

    @_info(
        "Cannot pin %(dependency)s in %(location)s: the URL is declared as a bare string, so it has no attribute "
        "dictionary to hold an integrity hash"
    )
    def cannot_pin(self, reference: Reference) -> dict[str, object]:
        """Log that a URL declared as a bare string cannot be pinned."""
        return self._reference_fields(reference)

    DIGEST_DRIFT = Drift(":")
    TAG_DRIFT = Drift("@", RefKind.TAG, "now points at", "the tag was moved deliberately")
    # A branch's drift names its newest commit older than the cooldown, which the branch may have moved on from already.
    BRANCH_DRIFT = Drift("@", RefKind.BRANCH, "has moved on to", "the branch moved to a commit you trust")
    REF_DRIFT = Drift("@", RefKind.REF, "now points at", "the ref moved to a commit you trust")

    @classmethod
    def _drift_fields(cls, kind: Drift, drifted: DriftedPin, **extra: object) -> dict[str, object]:
        """Return the fields every drift message carries, plus the ones the message reporting it adds."""
        return cls._drifted_reference_fields(
            kind, drifted, current_sha=drifted.current_sha, new_sha=drifted.new_sha, **extra
        )

    @classmethod
    def _drifted_reference_fields(cls, kind: Drift, drifted: DriftedPin, **extra: object) -> dict[str, object]:
        """Return the fields naming the drifted reference, plus the ones the message reporting it adds."""
        return cls._reference_fields(drifted, tag=tag_of(drifted.current_version, kind.separator), **extra)

    @_warning(
        "%(Ref)s drift for %(dependency)s%(tag)s in %(location)s: pinned to commit %(current_sha)s but the %(ref)s "
        "%(moved)s %(new_sha)s; the pin was left unchanged, verify %(advice)s before updating the pin"
    )
    @_warning(
        "Digest drift for %(dependency)s%(tag)s in %(location)s: pinned to %(current_sha)s but the registry "
        "now serves %(new_sha)s; the pin was left unchanged, verify the change is expected before updating the pin"
    )
    def drift(self, kind: Drift, drifted: DriftedPin) -> dict[str, object]:
        """Warn that a hash pin drifted."""
        return self._drift_fields(kind, drifted, **kind.warning_words)

    @_info(
        "Adopted %(ref)s drift for %(dependency)s%(tag)s in %(location)s: "
        "re-pinned from commit %(current_sha)s to %(new_sha)s (update-time: %(directive)s)"
    )
    @_info(
        "Adopted digest drift for %(dependency)s%(tag)s in %(location)s: "
        "re-pinned from %(current_sha)s to %(new_sha)s (update-time: %(directive)s)"
    )
    def adopted_drift(self, kind: Drift, drifted: DriftedPin, directive: str) -> dict[str, object]:
        """Log that a reference adopted the drift of its hash pin."""
        return self._drift_fields(kind, drifted, directive=directive, **kind.adopted_words)

    @_warning(
        "Integrity hash mismatch for %(dependency)s@%(version)s in %(location)s: declares %(declared_hash)s but "
        "jsDelivr serves %(served_hash)s; the hash was left unchanged, and since npm does not republish a version "
        "it is probably the declared hash that is wrong"
    )
    def hash_mismatch(self, reference: Reference, declared_hash: str, served_hash: str) -> dict[str, object]:
        """Warn that a declared integrity hash differs from the one jsDelivr serves."""
        return self._reference_fields(
            reference, version=reference.current_version, declared_hash=declared_hash, served_hash=served_hash
        )

    @_warning(
        "Stale dependency %(dependency)s in %(location)s: newest release %(version)s was published "
        "%(days)d days ago (> %(threshold)d)"
    )
    def report_staleness(self, resolved: ResolvedReference, threshold: int) -> dict[str, object] | None:
        """Warn that the reference's newest release is older than the threshold, where it is."""
        if (newest := stale_release(resolved.release, threshold)) is None:
            return None
        return self._reference_fields(
            resolved, version=newest.version, days=days_since(newest.published), threshold=threshold
        )

    _JUDGED_ALIKE = (
        "newest release %(version)s was published %(days)d days ago, which the threshold of %(threshold)d days"
    )

    @_warning(_redundant_directive(f"{_JUDGED_ALIKE} and the default of %(default_threshold)d days judge alike"))
    @_warning(_redundant_directive(f"{_JUDGED_ALIKE} and --stale-after %(run_threshold)d judge alike"))
    def redundant_stale_threshold(
        self, reference: Reference, threshold: Threshold[int], newest: Release, run_threshold: int
    ) -> dict[str, object]:
        """Warn that the marker's staleness threshold judges the newest release as the run's threshold does."""
        return self._reference_fields(
            reference,
            directive=threshold.directive,
            version=newest.version,
            days=days_since(newest.published),
            threshold=threshold.value,
            **_run_setting_field("threshold", STALE_AFTER, run_threshold),
        )

    @_debug(_ignoring("the staleness warning"))
    def ignored_staleness(self, reference: Reference, directive: str) -> dict[str, object]:
        """Log that the marker's staleness threshold silenced a warning the run's threshold gives."""
        return self._ignored_fields(reference.dependency, directive, reference.location)

    @classmethod
    def _yank_fields(cls, resolved: ResolvedReference) -> dict[str, object] | None:
        """Return the yank warning's fields, or None when the version the run leaves the reference on was not yanked.

        The reason is the yank itself, which renders as the maintainer's words where they gave any.
        """
        release = resolved.release
        if release.yank is None or not release.yank.yanked:
            return None
        return cls._reference_fields(resolved, version=release.version, reason=release.yank)

    @_warning(_redundant_directive("version %(version)s was not yanked"))
    @_debug(_ignoring("the yank warning"))
    @_warning("Yanked dependency %(dependency)s in %(location)s: version %(version)s was yanked (%(reason)s)")
    def report_yank(self, resolved: ResolvedReference, marker: Marker) -> dict[str, object] | None:
        """Report the version's yank, or the marker that silenced it or had nothing to silence."""
        release = resolved.release
        redundancy_fields = None if release.yank is None else {"version": release.version}
        return self._check_fields(Scope.YANKED, marker, resolved, self._yank_fields(resolved), redundancy_fields)

    @_warning(_redundant_directive("the %(subject)s is not archived"))
    @_debug(_ignoring("the archival warning"))
    @_warning("Archived dependency %(dependency)s in %(location)s: the %(subject)s was archived%(reason)s")
    def report_archival(self, resolved: ResolvedReference, marker: Marker) -> dict[str, object] | None:
        """Report the reference's archival, or the marker that silenced it or had nothing to silence."""
        if (archival := resolved.release.project.archival) is None:
            return None
        reason = f' ("{archival.reason}")' if archival.reason else ""
        warning_fields = (
            self._reference_fields(resolved, subject=archival.subject, reason=reason) if archival.archived else None
        )
        redundancy_fields = {"subject": archival.subject}
        return self._check_fields(Scope.ARCHIVED, marker, resolved, warning_fields, redundancy_fields)

    @_warning(
        "Could not score the CVSS vector of advisory %(advisory)s (%(error)s), so it is reported at unknown severity"
    )
    def malformed_cvss_vector(self, advisory: str, error: Exception) -> dict[str, object]:
        """Warn that an advisory's CVSS vector could not be scored."""
        return {"advisory": advisory, "error": error}

    @_warning(
        "Could not read the affected versions of advisory %(advisory)s (%(error)s), so the vulnerability check "
        "passes over that part of the advisory"
    )
    def unreadable_affected_versions(self, advisory: str, error: Exception) -> dict[str, object]:
        """Warn that the affected versions an advisory states do not all parse."""
        return {"advisory": advisory, "error": error}

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

    @_warning(
        "Vulnerable dependency %(dependency)s in %(location)s: version %(version)s has %(vulnerability)s "
        "(%(advisory)s, %(url)s)"
    )
    def vulnerable_dependency(self, reference: Reference, vulnerability: Vulnerability) -> dict[str, object]:
        """Warn that the pinned version has a known vulnerability."""
        return self._vulnerability_fields(reference, vulnerability)

    @_debug(_ignoring("the %(advisory)s vulnerability warning"))
    def ignored_vulnerability(
        self, reference: Reference, vulnerability: Vulnerability, directive: str
    ) -> dict[str, object]:
        """Log that the marker's directive silenced a vulnerability warning."""
        return self._ignored_fields(
            reference.dependency, directive, reference.location, advisory=vulnerability.advisory
        )

    @_debug(_ignoring("the %(advisory)s vulnerability warning", "--ignore-vulnerability %(identifiers)s"))
    def globally_ignored_vulnerability(
        self, reference: Reference, vulnerability: Vulnerability, passed: frozenset[str]
    ) -> dict[str, object]:
        """Log that `--ignore-vulnerability` silenced a vulnerability warning.

        The option takes its identifiers comma-separated, so several of them read back as a value it accepts.
        """
        identifiers = ",".join(sorted(passed))
        return self._reference_fields(reference, advisory=vulnerability.advisory, identifiers=identifiers)

    @classmethod
    def _redundant_suppression_fields(cls, reference: Reference, directive: str, **extra: object) -> dict[str, object]:
        """Return the fields of a warning that a vulnerability suppression silences nothing for the pinned version.

        The directive is the caller's, since each of these messages judges one form of the `vulnerable` scope and
        the forms beside it may silence plenty.
        """
        return cls._reference_fields(reference, directive=directive, version=reference.current_version, **extra)

    @_warning(_redundant_directive("version %(version)s has no such vulnerability"))
    def redundant_vulnerable_advisory(self, reference: Reference, marker: Marker) -> dict[str, object]:
        """Warn that the advisory the marker names silences nothing."""
        return self._redundant_suppression_fields(reference, marker.advisory_directives)

    _SAME_VULNERABILITIES = "report the same vulnerabilities in version %(version)s"

    @_warning(
        _redundant_directive(
            f"the risk level %(level)s and the default level %(default_level)s {_SAME_VULNERABILITIES}"
        )
    )
    @_warning(
        _redundant_directive(
            f"the risk level %(level)s and --vulnerability-level %(run_level)s {_SAME_VULNERABILITIES}"
        )
    )
    def redundant_vulnerable_level(
        self, reference: Reference, marker: Marker, level: str, run_level: str
    ) -> dict[str, object]:
        """Warn that the marker's risk level reports the same vulnerabilities as the run's level does."""
        return self._redundant_suppression_fields(
            reference,
            marker.vulnerable.directive,
            level=level,
            **_run_setting_field("level", VULNERABILITY_LEVEL, run_level),
        )

    @_warning(_redundant_directive("%(reason)s"))
    def redundant_directive(self, reference: Reference, directive: str, reason: RedundancyReason) -> dict[str, object]:
        """Warn that a directive the marker carries decides nothing."""
        return self._reference_fields(reference, directive=directive, reason=reason)

    @_debug(
        "Reference %(dependency)s in %(location)s was left as it is: the repository does not tag a release to update to"
    )
    def no_release_tagged(self, reference: Reference) -> dict[str, object]:
        """Log that a reference's repository does not tag a release."""
        return self._reference_fields(reference)

    @_error("Could not fetch commit SHA for %(dependency)s %(version)s (%(reason)s): %(url)s")
    def no_commit_sha(self, dependency: str, version: str, reason: str, url: str) -> dict[str, object]:
        """Log that the commit SHA of a release could not be fetched."""
        return {"dependency": dependency, "version": version, "reason": reason, "url": url}

    @_error(
        "Could not fetch from PyPI the metadata of %(dependency)s %(candidate)s (%(reason)s), so its yank state and "
        f"publication date are unknown; {_stays_on('versions')}"
    )
    def no_release_metadata(self, dependency: str, candidate: str, current: str, reason: str) -> dict[str, object]:
        """Log that PyPI did not serve a release's metadata."""
        return self._stopped_walk_fields(dependency, candidate, current, reason)

    @_error(_undated("Docker Hub", ":%(candidate)s was pushed", "tags"))
    def undated_push(self, dependency: str, candidate: str, current: str, reason: str) -> dict[str, object]:
        """Log that Docker Hub could not date an image tag."""
        return self._stopped_walk_fields(dependency, candidate, current, reason)

    @_error(_undated("GitHub", " %(candidate)s was committed", "versions"))
    def undated_commit(self, dependency: str, candidate: str, current: str, reason: str) -> dict[str, object]:
        """Log that GitHub could not date the commit a version tag points at."""
        return self._stopped_walk_fields(dependency, candidate, current, reason)

    @staticmethod
    def _stopped_walk_fields(dependency: str, candidate: str, current: str, reason: str) -> dict[str, object]:
        """Return the fields of a message saying why a candidate stopped the walk, and which version stays."""
        return {"dependency": dependency, "candidate": candidate, "current": current, "reason": reason}

    @_error(
        "Could not determine from GitHub the date of commit %(new_sha)s that %(dependency)s%(tag)s in %(location)s "
        "now points at (%(reason)s), so the cooldown can't be verified; not adopting the drift"
    )
    def no_commit_date(self, kind: Drift, drifted: DriftedPin, reason: str) -> dict[str, object]:
        """Log that GitHub could not date the new commit of a drifted pin."""
        return self._drifted_reference_fields(kind, drifted, new_sha=drifted.new_sha, reason=reason)

    @_error(
        "Could not determine from Docker Hub when %(dependency)s%(tag)s in %(location)s was pushed (%(reason)s), "
        "so the cooldown can't be verified; not adopting the drift"
    )
    def no_push_date(self, kind: Drift, drifted: DriftedPin, reason: str) -> dict[str, object]:
        """Log that Docker Hub could not date the re-pushed tag of a drifted digest."""
        return self._drifted_reference_fields(kind, drifted, reason=reason)

    @_warning(
        "Could not read the publication date '%(date)s' that Maven Central lists for %(dependency)s %(version)s, "
        "so the cooldown holds this version back"
    )
    def unreadable_publication_date(self, dependency: str, version: str, date: str) -> dict[str, object]:
        """Warn that a date Maven Central lists does not parse."""
        return {"dependency": dependency, "version": version, "date": date}

    @_warning(
        "Could not resolve the integrity hash for %(dependency)s %(version)s (%(filename)s), leaving it unchanged"
    )
    def no_integrity_hash(self, dependency: str, version: str, filename: str) -> dict[str, object]:
        """Warn that a jsDelivr file's integrity hash could not be resolved."""
        return {"dependency": dependency, "version": version, "filename": filename}

    @_warning("Could not find where %(location)s declares %(dependency)s, leaving it unchanged")
    def no_entry(self, dependency: str, path: Path) -> dict[str, object]:
        """Warn that a file declares the dependency but the entry declaring it cannot be found in the file."""
        return self._file_fields(path, dependency=dependency)

    @_warning(
        "Invalid %(bracket_item)r in the update-time marker for %(dependency)s in %(location)s; "
        "leaving the reference unchanged"
    )
    def invalid_bracket_item(self, dependency: str, item: str, location: Location) -> dict[str, object]:
        """Warn that a marker carries an invalid bracket item."""
        return {"bracket_item": item, "dependency": dependency, "location": location}

    def report_marker(
        self, dependency: str, marker: Marker, location: Location, *, holds_the_update_back: bool
    ) -> Marker:
        """Report a reference's marker, and return the directives Update-time acts on."""
        for directive in marker.default_directives:
            self.redundant_directive(Reference(dependency, "", location), directive, Reason.ASKS_FOR_THE_DEFAULT)
        if marker.invalid_item is not None:
            self.invalid_bracket_item(dependency, marker.invalid_item, location)
            return marker.effective_directives
        self.recognised_marker(dependency, marker, location)
        if holds_the_update_back:
            self.ignored(dependency, marker, location)
        return marker

    @_warning(_redundant_directive("this bound %(redundancy)s at version %(version)s"))
    def warn_if_redundant_bound(self, reference: Reference, marker: Marker) -> dict[str, object] | None:
        """Warn when the marker's version bound is redundant for the current version.

        The bound is redundant when it never has an effect or blocks every update (see `VersionBound.redundancy`).
        """
        if (bound := marker.version_bound) == NO_BOUND:
            return None
        current_version = reference.current_version
        if (redundancy := bound.redundancy(current_version)) is None:
            return None
        return self._reference_fields(reference, directive=spell(bound), version=current_version, redundancy=redundancy)

    @_debug("Holding back the versions %(range)s of %(dependency)s in %(location)s (update-time: %(directive)s)")
    def holding_back_range(self, reference: Reference, maven_range: str, directive: str) -> dict[str, object]:
        """Log the range of versions a bound holds the reference back from, as the package manager reads it."""
        return self._reference_fields(reference, range=maven_range, directive=directive)

    @_debug("Recognised update-time marker %(directives)s for %(dependency)s in %(location)s")
    def recognised_marker(self, dependency: str, marker: Marker, location: Location) -> dict[str, object] | None:
        """Log that a reference's marker was recognised.

        The marker's directives are echoed verbatim, the `raw` text the user wrote, so a user comparing the log
        line against their file sees their own marker.
        """
        if not marker.raw:
            return None
        return {"directives": marker, "dependency": dependency, "location": location}

    @_debug(_ignoring("updates"))
    def ignored(self, dependency: str, marker: Marker, location: Location) -> dict[str, object]:
        """Log that a marker held a reference's update back."""
        return self._ignored_fields(dependency, marker.bound_directive, location)

    # --- File scanning and selection ---

    @_debug("Checking if there are updates for %(location)s")
    def path(self, path: Path) -> dict[str, object]:
        """Log that the file is checked for updates."""
        return self._file_fields(path)

    @_debug("Excluding %(path)s from the scan (--exclude-path)")
    def excluded_path(self, path: Path) -> dict[str, object]:
        """Log a directory that `--exclude-path` excludes."""
        return {"path": path}

    @_warning("Path %(path)s passed to --exclude-path does not exist")
    def missing_excluded_path(self, path: Path) -> dict[str, object]:
        """Warn that a directory passed to `--exclude-path` does not exist."""
        return {"path": path}

    @_debug(
        "Skipping %(location)s: a compiled or hash-pinned requirements file that does not name a requirements "
        "file as its source"
    )
    def compiled_file_without_a_source(self, path: Path) -> dict[str, object]:
        """Log that a compiled requirements file is skipped, since it does not name a requirements file to update."""
        return self._file_fields(path)

    @_warning("Skipping %(location)s: its source %(source)s is itself compiled, which Update-time does not support")
    def compiled_source_unsupported(self, path: Path, source: Path) -> dict[str, object]:
        """Warn that a compiled requirements file is skipped, since a source it names is itself compiled."""
        return self._file_fields(path, source=Location(source))

    @_info(
        "Leaving %(location)s as it is: Update-time updates the pins in %(sources)s, but does not recompile it, as "
        "%(reason)s"
    )
    def compiled_file_left_as_it_is(self, path: Path, sources: Sequence[Path], reason: str) -> dict[str, object]:
        """Log that a compiled requirements file is left as it is, while its sources are updated, and why."""
        return self._file_fields(path, sources=tuple(Location(source) for source in sources), reason=reason)

    @_debug("Leaving %(location)s and its source %(source)s as they are: the scan leaves %(source)s out")
    def source_outside_the_scan(self, path: Path, source: Path) -> dict[str, object]:
        """Log that the source of a compiled requirements file lies outside the scan, so neither file is updated."""
        return self._file_fields(path, source=Location(source))

    @_warning("Could not find %(source)s, a source %(location)s names; leaving %(location)s as it is")
    def source_not_found(self, path: Path, source: Path) -> dict[str, object]:
        """Log that a source the compiled requirements file names does not exist."""
        return self._file_fields(path, source=Location(source))

    # --- Updater-specific diagnostics ---

    @_info("Set uv exclude-newer to %(cooldown)r in %(location)s to apply the cooldown")
    def configured_uv_cooldown(self, path: Path, cooldown: str) -> dict[str, object]:
        """Log that Update-time wrote its cooldown into the project's uv configuration."""
        return self._file_fields(path, cooldown=cooldown)

    @_info("Set %(setting)s to %(cooldown)r for %(compiled)s, and recorded in %(location)s that Update-time manages it")
    def managed_by_comment_written(
        self, source: Path, compiled: Path, cooldown: str, setting: str
    ) -> dict[str, object]:
        """Log that Update-time passed its cooldown to a recompile, and wrote the managed-by comment into the source."""
        return self._file_fields(source, compiled=Location(compiled), cooldown=cooldown, setting=setting)

    @_warning(
        "Could not set uv exclude-newer in %(location)s to apply the cooldown: its tool, or the uv in it, is not a "
        "table"
    )
    def uv_cooldown_not_set(self, path: Path) -> dict[str, object]:
        """Warn that the project's uv configuration cannot hold the cooldown."""
        return self._file_fields(path)

    @_warning(
        "Could not read from uv's settings whether the project sets an exclude-newer for %(location)s; applying the "
        "cooldown"
    )
    def uv_settings_unreadable(self, path: Path) -> dict[str, object]:
        """Warn that uv's settings do not say whether the project sets an `exclude-newer` for the file."""
        return self._file_fields(path)

    @_warning(
        "Could not read from pip's settings whether they set an uploaded-prior-to for %(location)s (%(reason)s); "
        "applying the cooldown"
    )
    def pip_settings_unreadable(self, path: Path, reason: str) -> dict[str, object]:
        """Warn that pip does not list its settings, which say whether they set an `uploaded-prior-to` for the file."""
        return self._file_fields(path, reason=reason)

    @_error("Could not update the dependencies in %(location)s: uv tree failed")
    def uv_dependencies_not_updated(self, path: Path) -> dict[str, object]:
        """Log that `uv tree` failed for a file."""
        return self._file_fields(path)

    @_warning("Could not read %(version)r, the version %(location)s pins %(dependency)s to; not upgrading it")
    def pinned_version_unreadable(self, path: Path, dependency: str, version: str) -> dict[str, object]:
        """Warn that a compiled requirements file pins a package to a version that is not PEP 440."""
        return self._file_fields(path, dependency=dependency, version=version)

    @_error("Could not recompile %(location)s: %(tool)s failed")
    def compiled_file_not_recompiled(self, path: Path, tool: str) -> dict[str, object]:
        """Log that the tool failed to recompile a compiled requirements file."""
        return self._file_fields(path, tool=tool)

    @_warning("Skipping %(location)s: %(manager)s is not supported, only %(supported)s")
    def unsupported_package_manager(self, path: Path, manager: str, supported: str) -> dict[str, object]:
        """Warn that a file's package manager is not supported."""
        return self._file_fields(path, manager=manager, supported=supported)

    @_debug("Not running %(manager)s for %(location)s: it does not declare dependencies for %(manager)s to %(verb)s")
    def no_dependencies(self, path: Path, manager: str, verb: str) -> dict[str, object]:
        """Log that the package manager does not run for a file that declares nothing for it to install or resolve."""
        return self._file_fields(path, manager=manager, verb=verb)

    @_warning("Skipping %(location)s: it is not valid %(format)s")
    def invalid_file(self, path: Path, file_format: str) -> dict[str, object]:
        """Warn that a file does not parse."""
        return self._file_fields(path, format=file_format)

    @_warning("Could not read %(location)s to find the lines declaring its dependencies (%(error)s)")
    def unreadable_package_json(self, path: Path, error: OSError | ValueError) -> dict[str, object]:
        """Warn that a package.json can't be read to find the lines declaring its dependencies."""
        return self._file_fields(path, error=str(error))

    @_warning("Could not read %(location)s to find the package manager it names (%(error)s)")
    def unreadable_manager_declaration(self, path: Path, error: OSError | ValueError) -> dict[str, object]:
        """Warn that a package.json can't be read, so the package manager it may name goes unread."""
        return self._file_fields(path, error=str(error))

    @_warning("Could not read %(location)s to find the Node engine it declares (%(error)s)")
    def unreadable_node_engine(self, path: Path, error: OSError | ValueError) -> dict[str, object]:
        """Warn that a package.json can't be read, so the Node engine it may declare goes unread."""
        return self._file_fields(path, error=str(error))

    @_warning("Could not read the pom at %(url)s: it is not valid XML")
    def invalid_pom(self, url: str) -> dict[str, object]:
        """Warn that a pom a repository served does not parse."""
        return {"url": url}

    @_error("Could not read %(location)s after updating it: it is not valid XML")
    def invalid_xml_after_update(self, path: Path) -> dict[str, object]:
        """Report that the file an updater rewrote cannot be parsed, so its update failed rather than being skipped."""
        return self._file_fields(path)

    @_warning(
        _effective_pom_unused(
            "Could not read the effective pom of %(location)s",
            "perhaps a --quiet in the project's Maven config hid where Maven wrote it",
        )
    )
    def effective_pom_unreadable(self, path: Path) -> dict[str, object]:
        """Warn that the effective pom cannot be read, so the checks and the cooldown miss what it resolves."""
        return self._file_fields(path)

    @_warning(
        _effective_pom_unused(
            "The effective pom of %(location)s lacks the input locations Update-time reads",
            "the project's help plugin configuration sets verbose to false",
        )
    )
    def effective_pom_without_input_locations(self, path: Path) -> dict[str, object]:
        """Warn that the effective pom lacks input locations, so it resolves nothing."""
        return self._file_fields(path)

    @_error(
        "Could not tell what changed in %(location)s: it declared %(before)s dependencies and plugins before the "
        "update and %(after)s after it"
    )
    def declarations_changed(self, path: Path, before: int, after: int) -> dict[str, object]:
        """Report that the file an updater rewrote declares more or fewer dependencies and plugins than before."""
        return self._file_fields(path, before=before, after=after)

    @_debug(
        "Not reporting a new version for %(dependency)s in %(location)s: Maven moved the property %(property)s to "
        "%(version)s for the other artefacts naming it, and Maven Central does not serve this artefact"
    )
    def new_version_not_reported(self, reference: Reference, version_property: str, version: str) -> dict[str, object]:
        """Log that Maven moved the property the reference names, though Maven Central does not serve its artefact."""
        return self._reference_fields(reference, property=version_property, version=version)

    @_error(_CANNOT_READ_THE_REACTOR + _POMS_LEFT)
    def unreadable_reactor(self, root: Path, poms_left: int) -> dict[str, object]:
        """Report that Maven cannot read the reactor in the root directory."""
        return self._file_fields(root, poms=_poms(poms_left))

    @_error(_CANNOT_READ_THE_REACTOR + _AS_FOUND + _OTHER_POMS)
    def unreadable_reactor_as_found(self, root: Path, poms_left: int) -> dict[str, object]:
        """Report that Maven cannot read the reactor as the scan found it."""
        return self._file_fields(root, poms=_poms(poms_left, "other"))

    @_error(_CANNOT_READ_THE_REACTOR + _SINCE_UPDATE + _POMS_LEFT + _FIX)
    def unreadable_reactor_since_update(self, root: Path, updated: Path, poms_left: int) -> dict[str, object]:
        """Report that Maven cannot read the reactor since Update-time updated a pom."""
        return self._file_fields(root, updated=Location(updated), poms=_poms(poms_left))

    @_error(_CANNOT_READ_THE_REACTOR + _SINCE_UPDATE + _FIX)
    def unreadable_reactor_since_update_without_poms_left(self, root: Path, updated: Path) -> dict[str, object]:
        """Report that Maven cannot read the reactor since Update-time updated its last pom."""
        return self._file_fields(root, updated=Location(updated))

    @_error(_UPDATE_TIME_CANNOT_READ_THE_REACTOR + _SINCE_UNPARSABLE + _OTHER_POMS)
    def unparsable_reactor(self, root: Path, unparsable: Path, other_poms: int) -> dict[str, object]:
        """Report that a pom of the reactor does not parse."""
        return self._file_fields(root, unparsable=Location(unparsable), poms=_poms(other_poms, "other"))

    @_debug("Not running Maven over %(location)s: an earlier Maven run could not read the reactor this pom is part of")
    def maven_not_run(self, path: Path) -> dict[str, object]:
        """Log that Maven does not run over the pom, because it cannot read the pom's reactor."""
        return self._file_fields(path)

    # --- HTTP fetching ---

    @_warning("Could not fetch %(url)s: HTTP %(status)s %(reason)s")
    def response(self, response: Response) -> dict[str, object]:
        """Log that the source answered a request with an error."""
        return self._response_fields(response)

    @_debug("Could not fetch the changelog at %(url)s: HTTP %(status)s %(reason)s")
    def unserved_changelog(self, response: Response) -> dict[str, object]:
        """Log that a changelog URL a project publishes is not served."""
        return self._response_fields(response)

    @_debug("Could not fetch the version listing at %(url)s: HTTP %(status)s %(reason)s")
    def unserved_listing(self, response: Response) -> dict[str, object]:
        """Log that a repository does not serve a version listing for an artefact."""
        return self._response_fields(response)

    @staticmethod
    def _response_fields(response: Response) -> dict[str, object]:
        """Return the response's URL, status code, and reason phrase, as the fields of a message about it."""
        return {"url": response.url, "status": response.status_code, "reason": response.reason}

    @_warning("Could not fetch %(url)s: the request timed out")
    def timeout(self, url: str) -> dict[str, object]:
        """Warn that a request timed out."""
        return {"url": url}

    @_warning("Could not fetch %(url)s: %(error)s")
    def request_error(self, url: str, error: Exception) -> dict[str, object]:
        """Log a network error (connection failure, too many redirects, ...) while fetching a URL."""
        return {"url": url, "error": error}

    @_error("GitHub's API rate limit of %(limit)s requests is used up until %(reset)s; set GITHUB_TOKEN to raise it")
    def github_rate_limit_used_up(self, limit: int, reset: datetime) -> dict[str, object]:
        """Report that the anonymous GitHub rate limit is used up."""
        return {"limit": limit, "reset": utc_date_and_time(reset)}

    @_error("GitHub's API rate limit of %(limit)s requests for GITHUB_TOKEN is used up until %(reset)s")
    def github_token_rate_limit_used_up(self, limit: int, reset: datetime) -> dict[str, object]:
        """Report that the token's GitHub rate limit is used up."""
        return {"limit": limit, "reset": utc_date_and_time(reset)}

    @_debug("Could not read GitHub's rate-limit headers: %(error)s")
    def unreadable_github_rate_limit(self, error: KeyError | ValueError) -> dict[str, object]:
        """Log that GitHub's rate-limit headers could not be read."""
        return {"error": str(error)}

    @_warning(
        "Could not read whether GitHub archived %(repository)s: its repository endpoint answered without the "
        "archived flag"
    )
    def unreadable_archival(self, repository: str) -> dict[str, object]:
        """Warn that GitHub answered about the repository without an archived flag to read."""
        return {"repository": repository}

    # --- External commands ---

    @_debug("Version of %(executable)s in %(directory)s: %(version)s")
    def tool_version(self, executable: str, directory: Path, version: str) -> dict[str, object]:
        """Log the version an executable reports in a directory."""
        return {"executable": executable, "directory": Location(directory), "version": version}

    @_warning("Could not determine the version of %(executable)s in %(directory)s: %(reason)s")
    def tool_version_unknown(self, executable: str, directory: Path, reason: str) -> dict[str, object]:
        """Warn that an executable reports no version in a directory."""
        return {"executable": executable, "directory": Location(directory), "reason": reason}

    @_error("Could not run %(command)s: is %(executable)s installed?")
    def command_not_found(self, command: Command) -> dict[str, object]:
        """Log that a command's executable is not installed."""
        return {"command": command, "executable": command.executable}

    @_error("Could not run %(command)s: %(error)s")
    def command_error(self, command: Command, error: OSError) -> dict[str, object]:
        """Log that the system refused to run a command."""
        return {"command": command, "error": error}

    @_error("%(command)s failed:\n%(output)s")
    def command_failed(self, command: Command, output: str) -> dict[str, object]:
        """Log that a command exited non-zero."""
        return {"command": command, "output": output}

    @_warning("%(command)s wrote to stderr:\n%(stderr)s")
    def command_stderr(self, command: Command, stderr: str) -> dict[str, object]:
        """Log that a command wrote to stderr.

        The message stays neutral about severity because the tool decides that: its stderr may be an `[ERROR]`, a
        `[WARN]`, or just a notice (e.g. a pnpm deprecation).
        """
        return {"command": command, "stderr": stderr}


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
    """Return a new logger, configuring the root logger on the first call."""
    if not logging.getLogger().handlers:
        _configure_root_logger()
    return Logger(name)

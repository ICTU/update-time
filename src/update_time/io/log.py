"""The logger Update-time reports through, the messages it logs, and the configuration of the root logger."""

import inspect
import logging
import re
import shlex
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from functools import wraps
from logging import DEBUG, ERROR, INFO, WARNING
from signal import SIGINT, SIGQUIT, Signals
from types import MappingProxyType
from typing import TYPE_CHECKING, Concatenate, Final, Protocol, cast

from requests.exceptions import Timeout
from rich.console import Console

from update_time.domain.cooldown import cooldown_end
from update_time.domain.dependency import tag_of
from update_time.domain.marker import Scope
from update_time.domain.reference import DriftKind
from update_time.domain.staleness import STALE_AFTER
from update_time.domain.vulnerability import VULNERABILITY_LEVEL
from update_time.primitives.environment import EnvVar
from update_time.primitives.location import Location
from update_time.primitives.text import prose_list
from update_time.primitives.timestamp import days_since, utc_date_and_time

from .console import CHANGES, LOG_THEME, MESSAGE_NAME, NOTE, configure_logging, delimit_dependency, delimit_location

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from requests import Response

    from update_time.domain.bound import BoundRedundancy
    from update_time.domain.dependency import (
        Archival,
        DependencyVersion,
        FloatingPin,
        ImageNotLookedUp,
        Release,
        Unserved,
        VersionString,
        Yank,
    )
    from update_time.domain.directive import RedundancyReason
    from update_time.domain.marker import Marker, Threshold
    from update_time.domain.reference import DriftedPin, Reference, RefKind, ResolvedReference
    from update_time.domain.vulnerability import Vulnerability
    from update_time.primitives.command import Command


# The log levels that can be selected on the command line.
LOG_LEVELS: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "ERROR")
# Private channel that passes the log level from the CLI to the updater subprocesses.
LOG_LEVEL: Final[EnvVar[str]] = EnvVar("_UPDATE_TIME_LOG_LEVEL", default="INFO", parse=str)

# The log names the key beside the signal a terminal sends for it.
_KEYS: Final[MappingProxyType[Signals, str]] = MappingProxyType({SIGINT: "Ctrl-C", SIGQUIT: "Ctrl-\\"})


_HOLES_PATTERN: Final[re.Pattern[str]] = re.compile(r"%\((\w+)\)")


@dataclass(frozen=True, slots=True, kw_only=True)
class _LogMessage:
    """A log message, with the level it is logged at, its format string, and its name."""

    level: int
    format: str
    name: str

    def __str__(self) -> str:
        """Return the format string, so logging can interpolate the arguments into it."""
        return self.format

    def __repr__(self) -> str:
        """Return the format string's repr and the name, so a failing assertion shows which message it is."""
        return f"{self.format!r} ({self.name})"

    @property
    def holes(self) -> set[str]:
        """Return the names of the fields the format string interpolates."""
        return set(_HOLES_PATTERN.findall(self.format))


# A value a log message interpolates, or that the console renders below its line.
type _FieldValue = (
    str | int | Location | tuple[Location, ...] | Path | Marker | Command | Exception | Yank | RedundancyReason | None
)
type _Fields = dict[str, _FieldValue]
type _FieldsMethod[**P] = Callable[Concatenate[Logger, P], _Fields]
type _LogMethod[**P] = Callable[Concatenate[Logger, P], None]

# The console renders these fields below a record's line, rather than in its message.
_EXTRAS: Final[tuple[str, ...]] = (CHANGES, NOTE)


class _HoldsMessage(Protocol):
    """A log method, holding the message it logs."""

    message: _LogMessage


def _debug[**P](format_string: str, name: str) -> Callable[[_FieldsMethod[P]], _LogMethod[P]]:
    """Return a decorator adding the message with the format and the name at debug level to a log method."""
    return _logs_at(DEBUG, format_string, name)


def _info[**P](format_string: str, name: str) -> Callable[[_FieldsMethod[P]], _LogMethod[P]]:
    """Return a decorator adding the message with the format and the name at info level to a log method."""
    return _logs_at(INFO, format_string, name)


def _warning[**P](format_string: str, name: str) -> Callable[[_FieldsMethod[P]], _LogMethod[P]]:
    """Return a decorator adding the message with the format and the name at warning level to a log method."""
    return _logs_at(WARNING, format_string, name)


def _error[**P](format_string: str, name: str) -> Callable[[_FieldsMethod[P]], _LogMethod[P]]:
    """Return a decorator adding the message with the format and the name at error level to a log method."""
    return _logs_at(ERROR, format_string, name)


def _logs_at[**P](level: int, format_string: str, name: str) -> Callable[[_FieldsMethod[P]], _LogMethod[P]]:
    """Return a decorator making a log method of a method, which logs the message with the fields the method returns.

    The message has the format and the name, at the level.
    """
    message = _LogMessage(level=level, format=format_string, name=name)

    def decorate(fields_method: _FieldsMethod[P]) -> _LogMethod[P]:
        @wraps(fields_method)
        def log_method(self: Logger, *args: P.args, **kwargs: P.kwargs) -> None:
            fields = fields_method(self, *args, **kwargs)
            extra = {name: value for name, value in fields.items() if name in _EXTRAS}
            self._emit(message, {name: value for name, value in fields.items() if name not in _EXTRAS}, extra)

        cast(_HoldsMessage, log_method).message = message
        return log_method

    return decorate


@dataclass(frozen=True, slots=True, kw_only=True)
class _DriftMove:
    """What a warning about drift says: what moved, how it moved, and what to verify before updating the pin."""

    mover: str
    moved: str
    advice: str


# How each kind of hash pin drifts: a re-pushed image, or a moved ref.
_DRIFT_MOVES: Final[dict[DriftKind, _DriftMove]] = {
    DriftKind.DIGEST: _DriftMove(mover="registry", moved="now serves", advice="the change is expected"),
    DriftKind.TAG: _DriftMove(mover="tag", moved="now points at", advice="the tag was moved deliberately"),
    # A branch's drift names its newest commit older than the cooldown, which the branch may have moved on from already.
    DriftKind.BRANCH: _DriftMove(
        mover="branch", moved="has moved on to", advice="the branch moved to a commit you trust"
    ),
    DriftKind.REF: _DriftMove(mover="ref", moved="now points at", advice="the ref moved to a commit you trust"),
}


def _effective_pom_unused(problem: str, cause: str) -> str:
    """Return a message saying why the effective pom went unused, what the run missed, and what may cause it."""
    return (
        f"{problem}, so the cooldown and the checks passed over any group or artifact the pom leaves to its parent, "
        f"and the vulnerability check passed over any version the pom leaves to another pom or to Maven; {cause}"
    )


# How the report that a marker silenced a warning names that warning, by the scope that steers it.
_WARNINGS: Final[MappingProxyType[Scope, str]] = MappingProxyType(
    {
        Scope.STALE: "staleness",
        Scope.YANKED: "yank",
        Scope.ARCHIVED: "archival",
        Scope.VULNERABLE: "{advisory} vulnerability",
    }
)

# What an `ignore` that silences nothing finds in place of what it would silence, by its scope.
_FINDINGS: Final[MappingProxyType[Scope, str]] = MappingProxyType(
    {
        Scope.YANKED: "version {} was not yanked",
        Scope.VULNERABLE: "version {} has no such vulnerability",
        Scope.ARCHIVED: "the {} is not archived",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Unanswered:
    """What a source did not answer about a newer version, as the error words it."""

    asked: str
    separator: str
    event: str
    unknown: str
    in_between: str


_COOLDOWN_UNVERIFIED: Final[str] = "the cooldown can't be verified"
# What each source was asked about a candidate, by the kind of candidate.
_UNDATED_TAG: Final[_Unanswered] = _Unanswered(
    asked="determine from Docker Hub when",
    separator=":",
    event=" was pushed",
    unknown=_COOLDOWN_UNVERIFIED,
    in_between="tags",
)
_UNDATED_COMMIT: Final[_Unanswered] = _Unanswered(
    asked="determine from GitHub when",
    separator=" ",
    event=" was committed",
    unknown=_COOLDOWN_UNVERIFIED,
    in_between="versions",
)
_UNFETCHED_METADATA: Final[_Unanswered] = _Unanswered(
    asked="fetch from PyPI the metadata of",
    separator=" ",
    event="",
    unknown="its yank state and publication date are unknown",
    in_between="versions",
)


class ToolTask(StrEnum):
    """What Update-time runs a tool to do for a file, as the error that the tool could not do it names it."""

    UPDATE = "update the dependencies in"
    RECOMPILE = "recompile"


class ToolFailure(StrEnum):
    """How a tool failed to do its task for a file, as the error that it could not do it names it."""

    FAILED = "failed"
    NOT_ON_PATH = "is not on the path"


class ReactorCause(StrEnum):
    """Why Maven cannot read a reactor, as the error names it after the reactor's directory."""

    READ_EARLIER = ""
    AS_FOUND = " as the scan found it"
    UPDATED_POM = " since Update-time updated %(pom)s"
    UNPARSABLE_POM = " since %(pom)s does not parse"


class NotRunCause(StrEnum):
    """Why a package manager does not run for a file, as the log names it after the file."""

    NO_DEPENDENCIES_TO_INSTALL = "it does not declare dependencies for %(manager)s to install"
    NO_DEPENDENCIES_TO_RESOLVE = "it does not declare dependencies for %(manager)s to resolve"
    NO_LOCK_FILE = "it does not have a lock file, and %(manager)s would write %(file)s"
    NOT_A_UV_PROJECT = "it does not have a %(table)s table, and %(file)s does not exist"
    MARKED_UNMANAGED = "the %(table)s table of %(file)s sets managed = false"
    WORKSPACE_ROOT_OUTSIDE_THE_SCAN = "its %(manager)s workspace root %(file)s lies outside the scan root"
    LEFT_WITH_THE_WORKSPACE_ROOT = "the warning about its workspace root %(file)s says why"
    REACTOR_UNREADABLE = "Maven cannot read the reactor this pom is part of"


class FileContent(StrEnum):
    """What Update-time looks for in a file it reads, as the warning that it cannot read the file names it."""

    LOCKED_VERSIONS = "the versions it locks"
    CATALOGS = "the catalogs it holds"
    BUN_COOLDOWN = "whether it sets a cooldown for bun"
    DEPENDENCY_LINES = "the lines declaring its dependencies"
    PACKAGE_MANAGER = "the package manager it names"
    NODE_ENGINE = "the Node engine it declares"
    INLINE_SCRIPT_METADATA = "the inline script metadata it carries"
    REQUIRES_PYTHON = "the requires-python it declares"


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

    @classmethod
    def messages(cls) -> tuple[_LogMessage, ...]:
        """Return the messages the log methods hold."""
        methods = [value for value in vars(cls).values() if inspect.isfunction(value)]
        return tuple(message for method in methods if (message := getattr(method, "message", None)) is not None)

    def _emit(self, message: _LogMessage, fields: _Fields, extra: _Fields) -> None:
        """Emit a log record at the message's own level, carrying the message's name and any extra beside its fields."""
        self.log.log(message.level, message, self._rendered(fields), extra={**extra, MESSAGE_NAME: message.name})

    @classmethod
    def _rendered(cls, fields: _Fields) -> _Fields:
        """Return the fields with the ones the highlighter styles wrapped in their delimiter."""
        return {name: cls._render_field(name, value) for name, value in fields.items()}

    @classmethod
    def _render_field(cls, name: str, value: _FieldValue) -> _FieldValue:
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

    @_debug("Running Update-time v%(version)s on %(platform)s with Python %(python)s", "run-started")
    def running(self, version: str, platform: str, python: str) -> _Fields:
        """Log the version of Update-time, and the OS and Python version it runs on."""
        return {"version": version, "platform": platform, "python": python}

    @_debug("Command-line arguments: %(arguments)s", "command-line-arguments")
    def arguments(self, arguments: Sequence[str]) -> _Fields:
        """Log the command-line arguments, quoted as a shell reads them."""
        return {"arguments": shlex.join(arguments) or "none"}

    @_debug("Credentials: %(credentials)s", "credentials")
    def credentials(self, credentials: Mapping[str, bool]) -> _Fields:
        """Log which of the credential environment variables are set."""
        listed = ", ".join(f"{name} {'set' if is_set else 'not set'}" for name, is_set in credentials.items())
        return {"credentials": listed}

    @_debug("Network settings: %(settings)s", "network-settings")
    def network_settings(self, proxies: Mapping[str, str], no_proxy: str, ca_bundle: str) -> _Fields:
        """Log the proxies, the hosts they skip, and the CA bundle."""
        if not (listed := ", ".join(f"{scheme}={proxy}" for scheme, proxy in proxies.items())):
            connections = "direct connections"
        elif no_proxy:
            connections = f"proxies {listed}, except for {no_proxy}"
        else:
            connections = f"proxies {listed}"
        certificates = f"CA bundle {ca_bundle}" if ca_bundle else "the default CA bundle"
        return {"settings": f"{connections}; {certificates}"}

    @_error(
        "%(signal)s stopped Update-time before it finished; review the files it changed, since a file it was writing "
        "may be incomplete",
        "run-stopped",
    )
    def stopped(self, signal: Signals) -> _Fields:
        """Log that a signal stopped the run."""
        return {"signal": (f"{signal.name} ({key})" if (key := _KEYS.get(signal)) else signal.name)}

    @_error("Could not finish updating %(dependency_type)s: %(signal)s stopped it", "update-stopped")
    def update_stopped(self, dependency_type: str, signal: Signals) -> _Fields:
        """Log that a signal stopped the update of a dependency type."""
        return {"dependency_type": dependency_type, "signal": signal.name}

    @_warning(
        "Running outside a git repository (%(path)s) because --force was given; changes are made in place and cannot "
        "be reverted",
        "forced-outside-git-repository",
    )
    def forced_outside_git_repository(self, path: Path) -> _Fields:
        """Warn that --force overrode the refusal to run outside a git repository."""
        return {"path": path}

    _SUPPRESSING_CHANGELOG = "Suppressing changelog already shown, see above"
    _NO_CHANGELOG = "No changelog available!"

    @_info("New version available for %(dependency)s in %(location)s: %(version)s", "new-version")
    def new_version(self, reference: Reference, version: DependencyVersion) -> _Fields:
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

    @staticmethod
    def _reference_fields(reference: Reference, **extra: _FieldValue) -> _Fields:
        """Return the fields every message about a reference carries, plus the ones the message reporting it adds."""
        return {"dependency": reference.dependency, "location": reference.location, **extra}

    @_info(
        "%(manager)s rewrote the range of %(dependency)s in %(location)s from %(range)s to %(new_range)s; %(locked)s",
        "range-rewritten",
    )
    def range_rewrite(self, manager: str, reference: Reference, new_range: str, version: VersionString) -> _Fields:
        """Log that a package manager rewrote the range a reference declares, with the version locked before if any."""
        return self._reference_fields(
            reference,
            manager=manager,
            range=reference.current_version,
            new_range=new_range,
            locked=_locked_version(version),
        )

    @_info(
        "%(manager)s moved %(dependency)s in %(location)s back from %(version)s to %(new_version)s",
        "version-moved-back",
    )
    def version_moved_back(self, manager: str, reference: Reference, new_version: VersionString) -> _Fields:
        """Log that a package manager moved the version of a reference back to an older one."""
        return self._reference_fields(
            reference, manager=manager, version=reference.current_version, new_version=new_version
        )

    @_debug(
        "Holding back version %(version)s of %(dependency)s in %(location)s: published %(published)s, within the "
        "cooldown of %(days)d days until %(until)s%(set_by)s",
        "held-back-by-cooldown",
    )
    def held_back_by_cooldown(self, reference: Reference, fresh: Release, days: int, directive: str) -> _Fields:
        """Log that the cooldown holds a release back, naming the directive that set the cooldown, if any."""
        return self._reference_fields(
            reference,
            version=fresh.version,
            published=utc_date_and_time(fresh.published),
            days=days,
            until=utc_date_and_time(cooldown_end(fresh.published, days)),
            set_by=f" (update-time: {directive})" if directive else "",
        )

    @_info("Pinned %(dependency)s in %(location)s to %(version)s%(hash)s", "pinned")
    def pinned(self, reference: Reference, version: DependencyVersion) -> _Fields:
        """Log that a reference was pinned to the version, and to its hash where the version carries one."""
        return self._reference_fields(reference, version=version.version, hash=f"@{version.sha}" if version.sha else "")

    @_debug(
        "Keeping the %(pin)s %(dependency)s%(name)s in %(location)s: it resolves to %(resolved)s@%(sha)s "
        "(update-time: %(directive)s)",
        "floating-pin-kept",
    )
    def keeping_floating_pin(
        self, reference: Reference, resolved: DependencyVersion, directive: str, ref: RefKind | None = None
    ) -> _Fields:
        """Log that a floating pin was kept floating: a floating image tag, or else a ref of the given kind."""
        pin, separator = ("floating tag", ":") if ref is None else (ref, "@")
        fields = self._reference_fields(reference, pin=pin, name=tag_of(reference.current_version, separator))
        return {**fields, "resolved": resolved.version, "sha": resolved.sha, "directive": directive}

    @_error(
        "Could not fetch the commit of the %(ref)s %(dependency)s@%(name)s in %(location)s, so it was left as it is "
        "(%(reason)s)",
        "ref-commit-not-fetched",
    )
    def unpinned_ref(self, reference: Reference, reason: str, kind: RefKind) -> _Fields:
        """Log that the commit of a ref or a short commit SHA could not be fetched."""
        return self._reference_fields(reference, ref=kind, name=reference.current_version, reason=reason)

    @_error(
        "Could not check branch drift for %(dependency)s@%(name)s in %(location)s: GitHub did not compare the pinned "
        "commit with %(new_sha)s, the branch's newest commit older than the cooldown (%(reason)s); the pin was left "
        "unchanged",
        "branch-drift-not-checked",
    )
    def unchecked_branch_drift(self, reference: Reference, new_sha: str, reason: str) -> _Fields:
        """Log that a branch's drift could not be checked."""
        return self._reference_fields(reference, name=reference.current_version, new_sha=new_sha, reason=reason)

    def comment_naming_no_ref(self, reference: Reference) -> None:
        """Log that the comment beside a commit SHA does not name a branch or tag."""
        self.reference_left_as_is(
            replace(reference, current_version=""), _unnamed_ref_reason(reference.current_version)
        )

    @_debug("Reference %(dependency)s%(tag)s in %(location)s was left as it is: %(reason)s", "reference-left-as-is")
    def reference_left_as_is(self, reference: Reference, reason: ImageNotLookedUp | Unserved | str) -> _Fields:
        """Log why a reference was left as it is."""
        return {**self._tagged_fields(reference), "reason": reason}

    @classmethod
    def _tagged_fields(cls, reference: Reference) -> _Fields:
        """Return the fields a report names a reference by when it names the tag attached to it as well."""
        return cls._reference_fields(reference, tag=tag_of(reference.current_version))

    def unpinned_floating_tag(self, reference: Reference, resolved: DependencyVersion, reason: FloatingPin) -> None:
        """Log that a floating tag was left as it is.

        A reference that does not name a tag is named by its image alone, as the file writes it. The reason names
        the tag the source looked up, which is the version it reports back.
        """
        self.reference_left_as_is(reference, reason.explained(resolved.version))

    @_debug(
        "Reference %(dependency)s%(tag)s in %(location)s is %(substituted)s with its build arguments substituted; it "
        "does not get a digest, since a digest would overrule a --build-arg",
        "build-arguments-substituted",
    )
    def substituted_reference(self, reference: Reference, substituted: str) -> _Fields:
        """Log a reference naming build arguments as Docker builds on it."""
        return {**self._tagged_fields(reference), "substituted": substituted}

    @_info(
        "Cannot pin %(dependency)s in %(location)s: the URL is declared as a bare string, so it has no attribute "
        "dictionary to hold an integrity hash",
        "bare-url-not-pinned",
    )
    def cannot_pin(self, reference: Reference) -> _Fields:
        """Log that a URL declared as a bare string cannot be pinned."""
        return self._reference_fields(reference)

    @_warning(
        "%(Drift)s drift for %(dependency)s%(tag)s in %(location)s: pinned to %(pinned)s%(current_sha)s but the "
        "%(mover)s %(moved)s %(new_sha)s; the pin was left unchanged, verify %(advice)s before updating the pin",
        "hash-drift",
    )
    def drift(self, drifted: DriftedPin) -> _Fields:
        """Warn that a hash pin drifted."""
        return self._drift_warning_fields(drifted)

    @classmethod
    def _drift_warning_fields(cls, drifted: DriftedPin) -> _Fields:
        """Return the fields of the warning that a hash pin drifted, with the words naming how it moved."""
        move = _DRIFT_MOVES[drifted.kind]
        pinned = "" if drifted.kind is DriftKind.DIGEST else "commit "
        return cls._drift_fields(
            drifted,
            Drift=drifted.kind.noun.capitalize(),
            pinned=pinned,
            mover=move.mover,
            moved=move.moved,
            advice=move.advice,
        )

    @classmethod
    def _drift_fields(cls, drifted: DriftedPin, **extra: _FieldValue) -> _Fields:
        """Return the fields every drift message carries, plus the ones the message reporting it adds."""
        return cls._drifted_reference_fields(drifted, current_sha=drifted.current_sha, new_sha=drifted.new_sha, **extra)

    @classmethod
    def _drifted_reference_fields(cls, drifted: DriftedPin, **extra: _FieldValue) -> _Fields:
        """Return the fields naming the drifted reference, plus the ones the message reporting it adds."""
        return cls._reference_fields(drifted, tag=tag_of(drifted.current_version, drifted.kind.separator), **extra)

    @_info(
        "Adopted %(drift)s drift for %(dependency)s%(tag)s in %(location)s: "
        "re-pinned from %(current_sha)s to %(new_sha)s (update-time: %(directive)s)",
        "drift-adopted",
    )
    def adopted_drift(self, drifted: DriftedPin, directive: str) -> _Fields:
        """Log that a reference adopted the drift of its hash pin."""
        return self._adopted_drift_fields(drifted, directive)

    @classmethod
    def _adopted_drift_fields(cls, drifted: DriftedPin, directive: str) -> _Fields:
        """Return the fields of the report that a reference adopted its drift, naming what drifted."""
        return cls._drift_fields(drifted, directive=directive, drift=drifted.kind.noun)

    @_warning(
        "Integrity hash mismatch for %(dependency)s@%(version)s in %(location)s: declares %(declared_hash)s but "
        "jsDelivr serves %(served_hash)s; the hash was left unchanged, and since npm does not republish a version "
        "it is probably the declared hash that is wrong",
        "integrity-hash-mismatch",
    )
    def hash_mismatch(self, reference: Reference, declared_hash: str, served_hash: str) -> _Fields:
        """Warn that a declared integrity hash differs from the one jsDelivr serves."""
        return self._reference_fields(
            reference, version=reference.current_version, declared_hash=declared_hash, served_hash=served_hash
        )

    @_warning(
        "Stale dependency %(dependency)s in %(location)s: newest release %(version)s was published "
        "%(days)d days ago (> %(threshold)d)",
        "stale-dependency",
    )
    def stale_dependency(self, reference: Reference, newest: Release, threshold: int) -> _Fields:
        """Warn that the reference's newest release is older than the threshold."""
        return self._reference_fields(
            reference, version=newest.version, days=days_since(newest.published), threshold=threshold
        )

    def redundant_stale_threshold(
        self, reference: Reference, threshold: Threshold[int], newest: Release, run_threshold: int
    ) -> None:
        """Warn that the marker's staleness threshold judges the newest release as the run's threshold does."""
        reason = _stale_threshold_reason(newest.version, days_since(newest.published), threshold.value, run_threshold)
        self.redundant_directive(reference, threshold.directive, reason)

    @_warning(
        "Redundant update-time directive %(directive)s for %(dependency)s in %(location)s: %(reason)s",
        "redundant-directive",
    )
    def redundant_directive(self, reference: Reference, directive: str, reason: RedundancyReason | str) -> _Fields:
        """Warn that a directive the marker carries decides nothing."""
        return self._reference_fields(reference, directive=directive, reason=reason)

    def ignored_warning(self, reference: Reference, scope: Scope, directive: str, *, advisory: str = "") -> None:
        """Log that the marker silenced the warning that the scope steers, about the advisory for a vulnerability."""
        self.silenced_warning(reference, _WARNINGS[scope].format(advisory=advisory), f"update-time: {directive}")

    @_debug("Ignoring the %(warning)s warning for %(dependency)s in %(location)s (%(cause)s)", "warning-ignored")
    def silenced_warning(self, reference: Reference, warning: str, cause: str) -> _Fields:
        """Log that a warning was silenced, naming the directive or the option that silenced it."""
        return self._reference_fields(reference, warning=warning, cause=cause)

    @_warning(
        "Yanked dependency %(dependency)s in %(location)s: version %(version)s was yanked (%(reason)s)",
        "yanked-dependency",
    )
    def yanked_dependency(self, resolved: ResolvedReference) -> _Fields:
        """Warn that the version the run leaves the reference on was yanked.

        The reason is the yank itself, which renders as the maintainer's words where they gave any.
        """
        return self._reference_fields(resolved, version=resolved.version.version, reason=resolved.version.yank)

    def redundant_ignore(self, reference: Reference, scope: Scope, directive: str, judged: str) -> None:
        """Warn that the marker's `ignore` of the scope silences nothing for what it judged (see `_finding`)."""
        self.redundant_directive(reference, directive, _finding(scope, judged))

    @_warning(
        "Archived dependency %(dependency)s in %(location)s: the %(subject)s was archived%(reason)s",
        "archived-dependency",
    )
    def archived_dependency(self, reference: Reference, archival: Archival) -> _Fields:
        """Warn that the source archived the reference's project or repository, quoting the reason it gave, if any."""
        reason = f' ("{archival.reason}")' if archival.reason else ""
        return self._reference_fields(reference, subject=archival.subject, reason=reason)

    @_warning(
        "Could not score the CVSS vector of advisory %(advisory)s (%(error)s), so its risk level is unknown",
        "cvss-vector-unreadable",
    )
    def malformed_cvss_vector(self, advisory: str, error: Exception) -> _Fields:
        """Warn that an advisory's CVSS vector could not be scored."""
        return {"advisory": advisory, "error": error}

    @_warning(
        "Could not read the affected versions of advisory %(advisory)s (%(error)s), so the vulnerability check "
        "passes over that part of the advisory",
        "affected-versions-unreadable",
    )
    def unreadable_affected_versions(self, advisory: str, error: Exception) -> _Fields:
        """Warn that the affected versions an advisory states do not all parse."""
        return {"advisory": advisory, "error": error}

    @_warning(
        "Vulnerable dependency %(dependency)s in %(location)s: version %(version)s has %(vulnerability)s "
        "(%(advisory)s, %(url)s)",
        "vulnerable-dependency",
    )
    def vulnerable_dependency(self, reference: Reference, vulnerability: Vulnerability) -> _Fields:
        """Warn that the pinned version has a known vulnerability."""
        return self._vulnerability_fields(reference, vulnerability)

    @classmethod
    def _vulnerability_fields(cls, reference: Reference, vulnerability: Vulnerability) -> _Fields:
        """Return the fields the vulnerability warning carries."""
        return cls._reference_fields(
            reference,
            version=reference.current_version,
            vulnerability=str(vulnerability),
            advisory=vulnerability.advisory,
            url=vulnerability.url,
        )

    def globally_ignored_vulnerability(
        self, reference: Reference, vulnerability: Vulnerability, passed: frozenset[str]
    ) -> None:
        """Log that `--ignore-vulnerability` silenced a vulnerability warning.

        The option takes its identifiers comma-separated, so several of them read back as a value it accepts.
        """
        warning = _WARNINGS[Scope.VULNERABLE].format(advisory=vulnerability.advisory)
        self.silenced_warning(reference, warning, f"--ignore-vulnerability {','.join(sorted(passed))}")

    def redundant_vulnerable_level(self, reference: Reference, run_level: str) -> None:
        """Warn that the marker's risk level reports the same vulnerabilities as the run's level does.

        The directive is the level's alone, since the `ignore[vulnerable=…]` items beside it may silence plenty.
        """
        level = reference.marker.vulnerable
        reason = _risk_level_reason(level.value, run_level, reference.current_version)
        self.redundant_directive(reference, level.directive, reason)

    @_error(
        "Could not fetch the commit of the release %(dependency)s@%(version)s, so a reference cannot be pinned to it "
        "(%(reason)s): %(url)s",
        "commit-sha-not-fetched",
    )
    def no_commit_sha(self, dependency: str, version: str, reason: str, url: str) -> _Fields:
        """Log that the commit SHA of a release could not be fetched."""
        return {"dependency": dependency, "version": version, "reason": reason, "url": url}

    def no_release_metadata(self, dependency: str, candidate: str, current: str, reason: str) -> None:
        """Log that PyPI did not serve a release's metadata."""
        self.unknown_version_date(_UNFETCHED_METADATA, dependency, candidate, current, reason)

    @_error(
        "Could not %(asked)s %(dependency)s%(separator)s%(candidate)s%(event)s (%(reason)s), so %(unknown)s; "
        "%(dependency)s stays on %(current)s rather than trying the %(in_between)s in between",
        "version-date-unknown",
    )
    def unknown_version_date(
        self, unanswered: _Unanswered, dependency: str, candidate: str, current: str, reason: str
    ) -> _Fields:
        """Log that a source did not answer for a candidate, so the reference stays on its version."""
        fields = {"dependency": dependency, "candidate": candidate, "current": current, "reason": reason}
        return {**fields, **asdict(unanswered)}

    def undated_push(self, dependency: str, candidate: str, current: str, reason: str) -> None:
        """Log that Docker Hub could not date an image tag."""
        self.unknown_version_date(_UNDATED_TAG, dependency, candidate, current, reason)

    def undated_commit(self, dependency: str, candidate: str, current: str, reason: str) -> None:
        """Log that GitHub could not date the commit a version tag points at."""
        self.unknown_version_date(_UNDATED_COMMIT, dependency, candidate, current, reason)

    @_error(
        "Could not determine from %(source)s %(before)s%(dependency)s%(tag)s in %(location)s%(after)s (%(reason)s), "
        "so the cooldown can't be verified; not adopting the drift",
        "drift-date-unknown",
    )
    def undated_drift(self, drifted: DriftedPin, reason: str) -> _Fields:
        """Log that the source could not date what a drifted pin now points at, so its drift is not adopted."""
        return self._undated_drift_fields(drifted, reason)

    @classmethod
    def _undated_drift_fields(cls, drifted: DriftedPin, reason: str) -> _Fields:
        """Return the fields of the report that the source could not date what the drifted pin now points at.

        GitHub dates the new commit of a drifted ref, which the report names. Docker Hub dates the push of a drifted
        digest.
        """
        if drifted.kind is DriftKind.DIGEST:
            source, before, after = "Docker Hub", "when ", " was pushed"
        else:
            source, before, after = "GitHub", f"the date of commit {drifted.new_sha} that ", " now points at"
        return cls._drifted_reference_fields(drifted, reason=reason, source=source, before=before, after=after)

    @_warning(
        "Could not read the publication date '%(date)s' that Maven Central lists for %(dependency)s %(version)s, "
        "so the cooldown holds this version back",
        "publication-date-unreadable",
    )
    def unreadable_publication_date(self, dependency: str, version: str, date: str) -> _Fields:
        """Warn that a date Maven Central lists does not parse."""
        return {"dependency": dependency, "version": version, "date": date}

    @_warning(
        "Could not resolve the integrity hash for %(dependency)s %(version)s (%(filename)s), leaving it unchanged",
        "integrity-hash-not-resolved",
    )
    def no_integrity_hash(self, dependency: str, version: str, filename: str) -> _Fields:
        """Warn that a jsDelivr file's integrity hash could not be resolved."""
        return {"dependency": dependency, "version": version, "filename": filename}

    @_warning(
        "Could not find where %(location)s declares %(dependency)s, leaving it unchanged", "declaration-not-found"
    )
    def no_entry(self, dependency: str, path: Path) -> _Fields:
        """Warn that a file declares the dependency but the entry declaring it cannot be found in the file."""
        return self._file_fields(path, dependency=dependency)

    @staticmethod
    def _file_fields(path: Path, **fields: _FieldValue) -> _Fields:
        """Return the fields of a message about a file the scan found, plus the ones the message adds.

        Wrapping the path in a `Location` is what makes it one of those files, reported relative to the working
        directory and styled as a single token. A path the user named on the command line stays a plain `Path` instead
        (see `_render_field`).
        """
        return {"location": Location(path), **fields}

    @_warning(
        "Invalid %(bracket_item)r in the update-time marker for %(dependency)s in %(location)s", "invalid-marker-item"
    )
    def invalid_bracket_item(self, reference: Reference, item: str) -> _Fields:
        """Warn that the reference's marker carries an invalid bracket item."""
        return self._reference_fields(reference, bracket_item=item)

    def redundant_bound(self, reference: Reference, redundancy: BoundRedundancy) -> None:
        """Warn that the marker's version bound is redundant at the reference's current version."""
        reason = _bound_reason(redundancy, reference.current_version)
        self.redundant_directive(reference, reference.marker.version_bound_directive, reason)

    def holding_back_range(self, reference: Reference, maven_range: str, directive: str) -> None:
        """Log the range of versions a bound holds the reference back from, as the package manager reads it."""
        self.held_back(reference, f"the versions {maven_range}", directive)

    @_debug(
        "Holding back %(versions)s of %(dependency)s in %(location)s (update-time: %(directive)s)", "update-held-back"
    )
    def held_back(self, reference: Reference, versions: str, directive: str) -> _Fields:
        """Log that a marker holds the versions back from the reference, naming the directive."""
        return self._reference_fields(reference, versions=versions, directive=directive)

    @_debug(
        "Holding back the versions of %(dependency)s in %(location)s that the requires-python %(requires_python)s "
        "of %(pyproject_toml)s excludes",
        "versions-held-back-by-requires-python",
    )
    def held_back_by_requires_python(self, reference: Reference, requires_python: Reference) -> _Fields:
        """Log that the project's `requires-python` holds the reference back from the versions it excludes."""
        return self._reference_fields(
            reference, requires_python=requires_python.current_version, pyproject_toml=requires_python.location
        )

    @_warning("Ignoring the requires-python of %(location)s: it is not a version specifier", "requires-python-ignored")
    def invalid_requires_python(self, requires_python: Reference) -> _Fields:
        """Warn that the project's `requires-python` is not a version specifier, so it does not bound the update."""
        return {"location": requires_python.location}

    @_warning(
        "Incompatible Python version %(dependency)s in %(location)s: version %(version)s falls outside the "
        "requires-python %(requires_python)s of %(pyproject_toml)s, so uv refuses to run for the project",
        "incompatible-python-version",
    )
    def python_version_outside_requires_python(self, reference: Reference, requires_python: Reference) -> _Fields:
        """Warn that the reference names a Python version the project's `requires-python` excludes."""
        return self._reference_fields(
            reference,
            version=reference.current_version,
            requires_python=requires_python.current_version,
            pyproject_toml=requires_python.location,
        )

    @_debug("Recognised update-time marker %(directives)s for %(dependency)s in %(location)s", "marker-recognised")
    def recognised_marker(self, reference: Reference) -> _Fields:
        """Log that a reference's marker was recognised.

        The marker is echoed as the file spells it, so a user comparing the log line against their file sees their
        own marker.
        """
        return {"directives": reference.marker, "dependency": reference.dependency, "location": reference.location}

    def ignored(self, reference: Reference) -> None:
        """Log that a marker held a reference's update back."""
        self.held_back(reference, "every version", reference.marker.bound_directive)

    @_debug("Checking if there are updates for %(location)s", "file-checked")
    def path(self, path: Path) -> _Fields:
        """Log that the file is checked for updates."""
        return self._file_fields(path)

    @_debug("Excluding %(path)s from the scan (--exclude-path)", "path-excluded")
    def excluded_path(self, path: Path) -> _Fields:
        """Log a directory that `--exclude-path` excludes."""
        return {"path": path}

    @_warning("Path %(path)s passed to --exclude-path does not exist", "excluded-path-not-found")
    def missing_excluded_path(self, path: Path) -> _Fields:
        """Warn that a directory passed to `--exclude-path` does not exist."""
        return {"path": path}

    @_debug(
        "Skipping %(location)s: a compiled or hash-pinned requirements file that does not name a requirements "
        "file as its source",
        "compiled-file-without-source",
    )
    def compiled_file_without_a_source(self, path: Path) -> _Fields:
        """Log that a compiled requirements file is skipped, since it does not name a requirements file to update."""
        return self._file_fields(path)

    @_warning(
        "Skipping %(location)s: its source %(source)s is itself compiled, which Update-time does not support",
        "compiled-source-unsupported",
    )
    def compiled_source_unsupported(self, path: Path, source: Path) -> _Fields:
        """Warn that a compiled requirements file is skipped, since a source it names is itself compiled."""
        return self._file_fields(path, source=Location(source))

    @_info(
        "Leaving %(location)s as it is: Update-time updates the pins in %(sources)s, but does not recompile it, as "
        "%(reason)s",
        "compiled-file-left-as-is",
    )
    def compiled_file_left_as_it_is(self, path: Path, sources: Sequence[Path], reason: str) -> _Fields:
        """Log that a compiled requirements file is left as it is, while its sources are updated, and why."""
        return self._file_fields(path, sources=tuple(Location(source) for source in sources), reason=reason)

    @_debug(
        "Leaving %(location)s and its source %(source)s as they are: the scan leaves %(source)s out",
        "source-outside-scan",
    )
    def source_outside_the_scan(self, path: Path, source: Path) -> _Fields:
        """Log that the source of a compiled requirements file lies outside the scan, so neither file is updated."""
        return self._file_fields(path, source=Location(source))

    @_warning(
        "Could not find %(source)s, a source %(location)s names; leaving %(location)s as it is", "source-not-found"
    )
    def source_not_found(self, path: Path, source: Path) -> _Fields:
        """Log that a source the compiled requirements file names does not exist."""
        return self._file_fields(path, source=Location(source))

    @_debug(
        "Not applying the cooldown of %(days)d days to %(location)s: %(setting)s sets one", "cooldown-set-by-project"
    )
    def project_cooldown(self, path: Path, setting: str, days: int) -> _Fields:
        """Log that a package manager's own setting holds the cooldown of a file, rather than Update-time's cooldown."""
        return self._file_fields(path, setting=setting, days=days)

    @_info(
        "Set %(setting)s to %(cooldown)r for %(configured)s, and recorded in %(location)s that Update-time manages it",
        "cooldown-set",
    )
    def configured_cooldown(self, path: Path, recorded_in: Path, cooldown: str, setting: str) -> _Fields:
        """Log that Update-time set its cooldown for a file, and recorded in a file that it manages that cooldown."""
        return self._file_fields(recorded_in, configured=Location(path), cooldown=cooldown, setting=setting)

    @_warning(
        "Could not set uv exclude-newer in %(location)s to apply the cooldown: its tool, or the uv in it, is not a "
        "table",
        "uv-cooldown-not-set",
    )
    def uv_cooldown_not_set(self, path: Path) -> _Fields:
        """Warn that the project's uv configuration cannot hold the cooldown."""
        return self._file_fields(path)

    @_debug("The %(manager)s run for the workspace root %(root)s updates %(location)s", "updated-by-workspace-root")
    def updated_by_the_workspace_root(self, path: Path, manager: str, root: Path) -> _Fields:
        """Log that the package manager's run for the workspace root updates the member."""
        return self._file_fields(path, manager=manager, root=Location(root))

    def binary_lock(self, path: Path, lock: Path, text_lock: Path) -> None:
        """Warn that bun does not run for a file whose lock is binary, and say how to convert the lock."""
        self.manager_cannot_run(path, "bun", _binary_lock_reason(lock, text_lock))

    @_warning("Not running %(manager)s for %(location)s: %(reason)s", "manager-cannot-run")
    def manager_cannot_run(self, path: Path, manager: str, reason: str) -> _Fields:
        """Warn that Update-time cannot run the package manager for the file until the reason is fixed."""
        return self._file_fields(path, manager=manager, reason=reason)

    def yarn_without_a_cooldown(self, path: Path, directory: Path, version: str) -> None:
        """Warn that yarn does not run for a file whose workspace root runs a yarn that cannot apply a cooldown."""
        self.manager_cannot_run(path, "yarn", _old_yarn_reason(directory, version))

    def files_of_more_than_one_manager(self, path: Path, files: Iterable[Path]) -> None:
        """Warn that no package manager runs for a file whose project holds the files of more than one."""
        self.manager_cannot_run(path, "a package manager", _node_managers_reason(files))

    @_warning("Could not read %(location)s to find %(content)s (%(error)s)", "file-unreadable")
    def unreadable_file(self, path: Path, error: OSError | ValueError, content: FileContent) -> _Fields:
        """Warn that a file cannot be read, so Update-time goes on without what it looked for in the file."""
        return self._file_fields(path, content=content, error=str(error))

    @_warning(
        "Could not read from %(tool)s's settings whether they set an %(setting)s for %(location)s (%(reason)s); "
        "applying the cooldown",
        "tool-settings-unreadable",
    )
    def settings_unreadable(self, path: Path, tool: str, setting: str, reason: str) -> _Fields:
        """Warn that a tool does not report its settings, which say whether they set a cooldown for the file."""
        return self._file_fields(path, tool=tool, setting=setting, reason=reason)

    @_error("Could not %(task)s %(location)s: %(tool)s %(failure)s", "tool-failed")
    def tool_failed(self, path: Path, task: ToolTask, tool: str, failure: ToolFailure) -> _Fields:
        """Log how the tool failed to do its task for a file."""
        return self._file_fields(path, task=task, tool=tool, failure=failure)

    @_error(
        "Could not update the dependencies in %(location)s: they move together with %(failed)s, which uv could not "
        "lock",
        "dependencies-not-updated-together",
    )
    def rolled_back_with_cycle(self, path: Path, failed: Iterable[Path]) -> _Fields:
        """Log that a file got its text back, since uv could not lock a file in its cycle of path sources."""
        return self._file_fields(path, failed=tuple(Location(failed_path) for failed_path in failed))

    @_error("Could not give %(location)s back the text it held before the run (%(error)s)", "file-not-restored")
    def file_not_restored(self, path: Path, error: OSError) -> _Fields:
        """Log that a file the run rewrote cannot be given back its text."""
        return self._file_fields(path, error=str(error))

    @_error("Could not read %(location)s (%(error)s), so the dependencies it locks are not updated", "lock-not-read")
    def lock_not_read(self, path: Path, error: OSError | UnicodeDecodeError) -> _Fields:
        """Log that a lock cannot be read, so the files it locks keep the dependencies they had."""
        return self._file_fields(path, error=str(error))

    @_error("Could not remove %(location)s, which the run created (%(error)s)", "lock-not-removed")
    def lock_not_removed(self, path: Path, error: OSError) -> _Fields:
        """Log that a lock the run created cannot be removed."""
        return self._file_fields(path, error=str(error))

    @_info(
        "Leaving %(dependency)s in %(location)s as it is: Update-time does not update a pin with an environment marker",
        "pin-with-environment-marker-left-as-is",
    )
    def pin_with_environment_marker_left_as_is(self, reference: Reference) -> _Fields:
        """Log that Update-time leaves an exact pin with an environment marker, such as `; python_version < '3.13'`."""
        return self._reference_fields(reference)

    @_error(
        "Could not write %(location)s (%(error)s), so the dependencies uv locks together with it are not updated",
        "file-not-written",
    )
    def file_not_written(self, path: Path, error: OSError) -> _Fields:
        """Log that a file uv locks cannot be written, so the files uv locks together keep the dependencies they had."""
        return self._file_fields(path, error=str(error))

    @_warning(
        "Could not read %(version)r, the version %(location)s pins %(dependency)s to; not upgrading it",
        "pinned-version-unreadable",
    )
    def pinned_version_unreadable(self, path: Path, dependency: str, version: str) -> _Fields:
        """Warn that a compiled requirements file pins a package to a version that is not PEP 440."""
        return self._file_fields(path, dependency=dependency, version=version)

    def unsupported_package_manager(self, path: Path, manager: str, supported: str) -> None:
        """Warn that a file's package manager is not supported, so its dependencies are not updated."""
        self.manager_cannot_run(path, manager, _unsupported_manager_reason(supported))

    def signals_of_more_than_one_manager(self, path: Path, lock_files: Iterable[Path], tables: Iterable[str]) -> None:
        """Warn that no package manager runs for a pyproject.toml whose lock files, or else tables, name several."""
        self.manager_cannot_run(path, "uv", _python_managers_reason(lock_files, tables))

    @_debug("Not running %(manager)s for %(location)s: %(reason)s", "manager-not-run")
    def manager_not_run(self, path: Path, manager: str, cause: NotRunCause, file: Path | None = None) -> _Fields:
        """Log why the package manager does not run for the file, naming the file the cause turns on, if any."""
        return self._file_fields(path, manager=manager, reason=_not_run_reason(cause, manager, file))

    @_debug(
        "Updating the uv workspaces of %(workspace_roots)s together: their projects source each other by path",
        "workspaces-updated-together",
    )
    def path_source_cycle(self, workspace_roots: Iterable[Path]) -> _Fields:
        """Log that the workspaces whose projects source each other by path are updated together."""
        return {"workspace_roots": tuple(Location(root) for root in workspace_roots)}

    @_warning("Skipping %(location)s: it is not valid %(format)s", "invalid-file")
    def invalid_file(self, path: Path, file_format: str) -> _Fields:
        """Warn that a file does not parse."""
        return self._file_fields(path, format=file_format)

    @_error("Could not read %(location)s after updating it: it is not valid XML", "invalid-xml-after-update")
    def invalid_xml_after_update(self, path: Path) -> _Fields:
        """Report that the file an updater rewrote cannot be parsed, so its update failed rather than being skipped."""
        return self._file_fields(path)

    @_warning(
        _effective_pom_unused(
            "Could not read the effective pom of %(location)s",
            "perhaps a --quiet in the project's Maven config hid where Maven wrote it",
        ),
        "effective-pom-unreadable",
    )
    def effective_pom_unreadable(self, path: Path) -> _Fields:
        """Warn that the effective pom cannot be read, so the checks and the cooldown miss what it resolves."""
        return self._file_fields(path)

    @_warning(
        _effective_pom_unused(
            "The effective pom of %(location)s lacks the input locations Update-time reads",
            "the project's help plugin configuration sets verbose to false",
        ),
        "effective-pom-without-input-locations",
    )
    def effective_pom_without_input_locations(self, path: Path) -> _Fields:
        """Warn that the effective pom lacks input locations, so it resolves nothing."""
        return self._file_fields(path)

    @_error(
        "Could not tell what changed in %(location)s: it declared %(before)s dependencies and plugins before the "
        "update and %(after)s after it",
        "declarations-changed",
    )
    def declarations_changed(self, path: Path, before: int, after: int) -> _Fields:
        """Report that the file an updater rewrote declares more or fewer dependencies and plugins than before."""
        return self._file_fields(path, before=before, after=after)

    @_debug(
        "The versions plugin's %(setting)s%(set_at)s wins over Update-time for %(location)s, so %(consequence)s",
        "versions-plugin-setting-wins",
    )
    def option_overridden_by_the_pom(self, path: Path, setting: str, consequence: str, pom: str, line: int) -> _Fields:
        """Log that a setting of the pom's versions plugin configuration wins over Update-time.

        Where the effective pom locates the setting, the message names the pom and the line.
        """
        set_at = f", set on line {line} of {pom}," if pom else ""
        return self._file_fields(path, setting=setting, consequence=consequence, set_at=set_at)

    @_debug(
        "Not reporting a new version for %(dependency)s in %(location)s: Maven moved the property %(property)s to "
        "%(version)s for the other artefacts naming it, and Maven Central does not serve this artefact",
        "new-version-not-reported",
    )
    def new_version_not_reported(self, reference: Reference, version_property: str, version: str) -> _Fields:
        """Log that Maven moved the property the reference names, though Maven Central does not serve its artefact."""
        return self._reference_fields(reference, property=version_property, version=version)

    @_error("Maven cannot read the reactor in %(location)s%(cause)s%(poms)s%(fix)s", "reactor-unreadable")
    def unreadable_reactor(self, root: Path, cause: ReactorCause, poms: int, pom: Path | None = None) -> _Fields:
        """Report why Maven cannot read the reactor in the root directory, and how many poms Update-time leaves."""
        return self._file_fields(root, **_reactor_clauses(cause, poms, pom))

    @_warning("Could not fetch %(url)s: %(failure)s", "request-failed")
    def request_failed(self, url: str, failure: Response | Exception) -> _Fields:
        """Warn that a request failed, naming why (see `_failure`)."""
        return {"url": url, "failure": _failure(failure)}

    @_debug("Could not fetch the changelog at %(url)s: HTTP %(status)s %(reason)s", "changelog-not-served")
    def unserved_changelog(self, response: Response) -> _Fields:
        """Log that a changelog URL a project publishes is not served."""
        return {"url": response.url, "status": response.status_code, "reason": response.reason}

    @_debug("Could not fetch the version listing at %(url)s (%(reason)s)", "version-listing-not-served")
    def unserved_listing(self, url: str, reason: str) -> _Fields:
        """Log that a repository does not serve a version listing for an artefact."""
        return {"url": url, "reason": reason}

    @_warning("Could not read %(url)s: it is not valid %(format)s", "invalid-response")
    def invalid_response(self, url: str, response_format: str) -> _Fields:
        """Warn that a source answered a request with a body that does not parse as the format it should have."""
        return {"url": url, "format": response_format}

    @_error(
        "GitHub's API rate limit of %(limit)s requests%(holder)s is used up until %(reset)s%(advice)s",
        "github-rate-limit-used-up",
    )
    def github_rate_limit_used_up(self, limit: int, reset: datetime, *, authenticated: bool) -> _Fields:
        """Report that GitHub's rate limit is used up, advising a token where the requests carried none."""
        holder, advice = (" for GITHUB_TOKEN", "") if authenticated else ("", "; set GITHUB_TOKEN to raise it")
        return {"limit": limit, "reset": utc_date_and_time(reset), "holder": holder, "advice": advice}

    @_debug("Could not read GitHub's rate-limit headers: %(error)s", "github-rate-limit-headers-unreadable")
    def unreadable_github_rate_limit(self, error: KeyError | ValueError) -> _Fields:
        """Log that GitHub's rate-limit headers could not be read."""
        return {"error": str(error)}

    @_warning(
        "Could not read whether GitHub archived %(repository)s: its repository endpoint answered without the "
        "archived flag",
        "archived-flag-missing",
    )
    def unreadable_archival(self, repository: str) -> _Fields:
        """Warn that GitHub answered about the repository without an archived flag to read."""
        return {"repository": repository}

    @_debug("Version of %(executable)s in %(directory)s: %(version)s", "tool-version")
    def tool_version(self, executable: str, directory: Path, version: str) -> _Fields:
        """Log the version an executable reports in a directory."""
        return {"executable": executable, "directory": Location(directory), "version": version}

    @_debug("Running %(command)s in %(directory)s", "command-run")
    def command_run(self, command: Command, directory: Path, environment: Mapping[str, str]) -> _Fields:
        """Log a command, with the variables it adds to the environment, and the directory it runs in."""
        variables = [f"{name}={value}" for name, value in environment.items()]
        return {"command": " ".join((*variables, *command)), "directory": Location(directory)}

    @_warning("Could not determine the version of %(executable)s in %(directory)s: %(reason)s", "tool-version-unknown")
    def tool_version_unknown(self, executable: str, directory: Path, reason: str) -> _Fields:
        """Warn that an executable reports no version in a directory."""
        return {"executable": executable, "directory": Location(directory), "reason": reason}

    @_error("Could not run %(command)s: %(reason)s", "command-not-run")
    def command_not_run(self, command: Command, reason: str) -> _Fields:
        """Log why the system could not run a command."""
        return {"command": command, "reason": reason}

    @_error("%(command)s failed:\n%(output)s", "command-failed")
    def command_failed(self, command: Command, output: str) -> _Fields:
        """Log that a command exited non-zero."""
        return {"command": command, "output": output}

    @_warning("%(command)s wrote to stderr:\n%(stderr)s", "command-stderr")
    def command_stderr(self, command: Command, stderr: str) -> _Fields:
        """Log that a command wrote to stderr.

        The message stays neutral about severity because the tool decides that: its stderr may be an `[ERROR]`, a
        `[WARN]`, or just a notice (e.g. a pnpm deprecation).
        """
        return {"command": command, "stderr": stderr}


def _locked_version(version: VersionString) -> str:
    """Return what the report that a range was rewritten says about the version the lock file held before."""
    if version:
        return f"the version in the lock file is unchanged at {version}"
    return "the lock file did not record a version before"


def _unnamed_ref_reason(comment: str) -> str:
    """Return why a commit SHA is left as it is: the comment beside it does not name a branch or tag."""
    return f"its comment {comment} does not name a branch or tag of the repository"


def _stale_threshold_reason(version: str, days: int, threshold: int | None, run_threshold: int) -> str:
    """Return why a staleness threshold is redundant: it judges the newest release as the run's threshold does."""
    run_setting = _run_setting(STALE_AFTER, run_threshold, "--stale-after", "the default of {} days")
    return (
        f"newest release {version} was published {days} days ago, which the threshold of {threshold} days and "
        f"{run_setting} judge alike"
    )


def _run_setting[T](setting: EnvVar[T], value: T, option: str, default: str) -> str:
    """Return how a message names the run's value: in the default's wording, or as the option that set it."""
    return default.format(value) if value == setting.default else f"{option} {value}"


def _finding(scope: Scope, judged: str) -> str:
    """Return what an `ignore` of the scope finds at what it judged: a version, or the subject a source archives."""
    return _FINDINGS[scope].format(judged)


def _risk_level_reason(level: str | None, run_level: str, version: str) -> str:
    """Return why a risk level is redundant: it reports the vulnerabilities in the version the run's level does."""
    run_setting = _run_setting(VULNERABILITY_LEVEL, run_level, "--vulnerability-level", "the default level {}")
    return f"the risk level {level} and {run_setting} report the same vulnerabilities in version {version}"


def _bound_reason(redundancy: BoundRedundancy, version: str) -> str:
    """Return why a bound is redundant at the version: it never has an effect there, or blocks every update."""
    return f"this bound {redundancy} at version {version}"


def _binary_lock_reason(lock: Path, text_lock: Path) -> str:
    """Return why bun cannot run for a file whose lock is binary, and how to convert the lock."""
    return (
        f"it locks its dependencies in the binary {_delimited(lock)}, which Update-time cannot read; run bun install "
        f"--save-text-lockfile to convert it to {_delimited(text_lock)}"
    )


def _delimited(path: Path) -> str:
    """Return the path as a reason names it, marked for the highlighter as a location."""
    return delimit_location(Location(path))


def _old_yarn_reason(directory: Path, version: str) -> str:
    """Return why a yarn that cannot apply a cooldown does not run, and how to switch to one that can."""
    return (
        f"the yarn that runs in {_delimited(directory)} is version {version}, which cannot apply a cooldown; run yarn "
        "set version stable to switch the project to a yarn that can"
    )


def _node_managers_reason(files: Iterable[Path]) -> str:
    """Return why no package manager runs where the files name different ones, and how to choose."""
    named = prose_list([_delimited(file) for file in files])
    return f"{named} name different ones; set the packageManager field to the one to run"


def _unsupported_manager_reason(supported: str) -> str:
    """Return why Update-time does not run a package manager it does not support."""
    return f"Update-time supports only {supported}"


def _python_managers_reason(lock_files: Iterable[Path], tables: Iterable[str]) -> str:
    """Return why uv does not run where the lock files, or else the tables, name different package managers."""
    signals = prose_list([_delimited(lock_file) for lock_file in lock_files] or [f"[tool.{table}]" for table in tables])
    return (
        f"{signals} name different package managers; remove the lock file or the table of the one the project no "
        "longer uses"
    )


def _not_run_reason(cause: NotRunCause, manager: str, file: Path | None) -> str:
    """Return why the package manager does not run for a file, naming the file the cause turns on, if any."""
    return cause % {"manager": manager, "table": "[tool.uv]", "file": _delimited(file) if file else ""}


def _reactor_clauses(cause: ReactorCause, poms: int, pom: Path | None) -> dict[str, str]:
    """Return the clauses of the error about a reactor Maven cannot read: the cause, the poms left, and any fix.

    A reactor unreadable as the scan found it counts its other poms. Any other reactor counts the poms left in it.
    """
    counted = (
        f"its {_poms(poms, 'other')}"
        if cause in {ReactorCause.AS_FOUND, ReactorCause.UNPARSABLE_POM}
        else f"the {_poms(poms)} left in it"
    )
    fix = "; revert that pom and hold the update that broke the reactor back with a marker, or fix what Maven reports"
    return {
        "cause": cause % {"pom": delimit_location(Location(pom))} if pom else str(cause),
        "poms": f", so Update-time does not run Maven over {counted}" if poms else "",
        "fix": fix if cause is ReactorCause.UPDATED_POM else "",
    }


def _poms(count: int, *qualifiers: str) -> str:
    """Return the count of poms, with the qualifiers between, such as `1 pom` or `2 other poms`."""
    return " ".join((str(count), *qualifiers, "pom" if count == 1 else "poms"))


def _failure(failure: Response | Exception) -> str:
    """Return how the warning that a request failed words the failure: the HTTP error, a timeout, or the error."""
    if isinstance(failure, Timeout):
        return "the request timed out"
    if isinstance(failure, Exception):
        return str(failure)
    return f"HTTP {failure.status_code} {failure.reason}"


def set_log_level(level: str) -> None:
    """Log at the level from now on, in this process and in the processes it starts."""
    LOG_LEVEL.set(level)
    _configure_root_logger()


def _configure_root_logger() -> None:
    """Send all diagnostics to stderr, at the run's log level.

    Update-time's real output is the files it rewrites in place; everything it logs — the new-version report as much
    as the warnings and errors — is diagnostics about the run, so it all goes to stderr. That keeps stdout clean for
    the argparse-handled `--version`/`--help` output, so e.g. `v=$(update-time -V)` isn't polluted with log lines.
    """
    configure_logging(Console(stderr=True, theme=LOG_THEME), LOG_LEVEL.get())


def get_logger(name: str) -> Logger:
    """Return a new logger, configuring the root logger on the first call."""
    if not logging.getLogger().handlers:
        _configure_root_logger()
    return Logger(name)

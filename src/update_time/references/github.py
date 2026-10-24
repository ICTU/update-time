"""The decision of which commit a GitHub Action `uses:` or a pre-commit hook `rev:` is pinned to."""

import re
from dataclasses import dataclass, replace
from enum import Enum, auto
from functools import partial, wraps
from typing import TYPE_CHECKING

from packaging.version import Version

from update_time.domain.dependency import DependencyVersion, Unserved, is_valid
from update_time.domain.reference import DriftedPin, Reference, RefKind, hash_drifted
from update_time.domain.vulnerability import reports_vulnerabilities, vulnerability_reporting
from update_time.io.log import Logger
from update_time.markers.cooldown import cooldown_days
from update_time.markers.directive import Reason
from update_time.markers.floating import floating_pin_cause
from update_time.markers.marker import Scope
from update_time.primitives.digest import COMMIT_SHA, SHORT_COMMIT_SHA
from update_time.primitives.lookup import DeferredLookup
from update_time.primitives.text import replace_match
from update_time.references.drift import adopts_drift
from update_time.references.match import matched_dependency
from update_time.references.resolve import (
    latest_version,
    report_directives_that_set_nothing,
    report_project_checks,
    warn_if_the_opt_in_is_redundant,
)
from update_time.sources.github import (
    commit_date,
    full_sha,
    get_latest_version,
    is_tag,
    moved_on,
    newest_commit_past_cooldown,
    pinned_ref,
    project,
    version_at_tag,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from update_time.domain.bound import NewVersionGetter, VersionBound
    from update_time.domain.dependency import PinnedDependency
    from update_time.io.log import Drift
    from update_time.markers.marker import Marker
    from update_time.markers.opt_in import OptInCause
    from update_time.primitives.location import Location
    from update_time.primitives.lookup import LookedUp, Lookup


def github_reference(match: re.Match[str], location: Location, dependency: str) -> Reference:
    """Return the GitHub reference the match captured."""
    current_sha = match.group("sha") or ""
    version = match.group("version") if current_sha else match.group("tag")
    return Reference(dependency, version, location, current_sha)


class _CommitSha(Enum):
    """How a ref names a commit: by its full SHA, by an abbreviated one, or not at all, as a branch or tag does."""

    FULL = auto()
    SHORT = auto()
    NONE = auto()

    @property
    def names_a_commit(self) -> bool:
        """Return whether the ref names a commit by its SHA."""
        return self is not _CommitSha.NONE


def _names_a_short_sha(reference: Reference) -> bool:
    """Return whether the reference names an abbreviated commit SHA, which can parse as a version, rather than a tag."""
    ref = reference.current_version
    # A reference pinned to a full SHA names a version, a tag, or a branch in its comment, never a short SHA.
    if reference.current_sha or not re.fullmatch(SHORT_COMMIT_SHA, ref):
        return False
    sha = full_sha(reference.dependency, ref)
    # A ref whose commit can't be fetched can't be pinned either, so its hex digits alone decide.
    return sha is None or sha.startswith(ref)


def _commit_sha(reference: Reference, marker: Marker) -> _CommitSha:
    """Return how the reference names a commit, leaving GitHub unasked where the marker holds everything back."""
    if re.fullmatch(COMMIT_SHA, reference.current_version):
        return _CommitSha.FULL
    if not marker.holds_everything_back and _names_a_short_sha(reference):
        return _CommitSha.SHORT
    return _CommitSha.NONE


def _reads_as_a_version(reference: Reference, commit_sha: _CommitSha) -> bool:
    """Return whether the reference names a version: one that parses, and that is not an abbreviated commit SHA."""
    return is_valid(reference.current_version) and commit_sha is not _CommitSha.SHORT


def names_a_version(reference: Reference, marker: Marker) -> bool:
    """Return whether the reference names a version, rather than a branch, a tag such as `stable-2024`, or a commit."""
    return _reads_as_a_version(reference, _commit_sha(reference, marker))


def _ref_kind(reference: Reference) -> RefKind:
    """Return the kind of ref the reference names: a listed tag, a branch, or a ref when the tags can't be listed."""
    match is_tag(reference.dependency, reference.current_version):
        case None:
            return RefKind.REF
        case True:
            return RefKind.TAG
        case _:
            return RefKind.BRANCH


# Map each kind of ref to the drift it reports when it moves.
_DRIFT = {drift.ref: drift for drift in (Logger.TAG_DRIFT, Logger.BRANCH_DRIFT, Logger.REF_DRIFT)}


@vulnerability_reporting
@wraps(get_latest_version, assigned=())
def _get_latest_action_version(
    pinned: PinnedDependency, version_bound: VersionBound, days: int, /, *, check_archival: bool
) -> DependencyVersion:
    """Return the latest version of the action, as `get_latest_version` does.

    The getter carries the capabilities of `get_latest_version`, and reports vulnerabilities on top of them, since OSV
    holds advisories for actions.
    """
    return get_latest_version(pinned, version_bound, days, check_archival=check_archival)


@dataclass(frozen=True)
class _PinResolver:
    """Decide what a GitHub reference is pinned to, as its marker steers, reporting to the logger."""

    marker: Marker
    log: Logger
    source: NewVersionGetter

    def latest_pin(self, reference: Reference) -> DependencyVersion | None:
        """Return the version or ref to pin the GitHub reference to, or None to leave the reference as it is."""
        current_version, current_sha = reference.current_version, reference.current_sha
        commit_sha = _commit_sha(reference, self.marker)
        if not _reads_as_a_version(reference, commit_sha):
            return self._unversioned_pin(reference, commit_sha)
        # The reference uses the commit its tag points at, named by the highest version tag at it.
        if not current_sha and not self.marker.holds_back_source_checks:
            reference = replace(reference, current_version=version_at_tag(reference.dependency, current_version))
        latest = latest_version(reference, self.source, self.marker, self.log)
        warn_if_the_opt_in_is_redundant(
            reference, self.marker, self.log, Scope.HASH_DRIFT, None, update_held_back=self.marker.ignores(Scope.UPDATE)
        )
        if latest is not None and latest.unserved is Unserved.RELEASES:
            self.log.no_release_tagged(reference)
            return None
        if latest is None or not latest.sha:
            return None
        if not current_sha:
            self.log.pinned(reference, latest)
        elif Version(latest.version) != Version(current_version):
            self.log.new_version(reference, latest)
        elif not hash_drifted(latest.sha, current_sha):
            return None  # Already pinned and up to date
        else:
            # The drift names the normalised version, so a rev frozen as v4.5.0 is reported as 4.5.0.
            moved = replace(reference, current_version=latest.version)
            committed = DeferredLookup(partial(commit_date, reference.dependency, latest.sha))
            return self._drifted_pin(Logger.TAG_DRIFT, moved, latest, publication=committed)
        return latest

    def _unversioned_pin(self, reference: Reference, commit_sha: _CommitSha) -> DependencyVersion | None:
        """Return the pin for a reference naming a branch, a tag, or a commit SHA, or None to leave it as it is."""
        names_a_commit = commit_sha.names_a_commit or self._comment_names_no_ref(reference)
        self._report_what_the_marker_decides(reference, names_a_commit=names_a_commit)
        pin = None if commit_sha is _CommitSha.FULL else self._ref_pin(reference, commit_sha)
        self._report_redundant_directives(reference, pin, names_a_commit=names_a_commit)
        if pin is not None and commit_sha is _CommitSha.SHORT and not pin.tag_name:
            return replace(pin, version="")  # The full SHA names the commit, so the pin does not need a comment
        return pin

    def _report_what_the_marker_decides(self, reference: Reference, *, names_a_commit: bool) -> None:
        """Report the directives that set nothing, the project checks, and a floating-pin opt-in that keeps nothing."""
        marker, log = self.marker, self.log
        report_directives_that_set_nothing(marker, self.source, reference, log)
        report_project_checks(reference, marker, log, project)
        pin_does_not_float = Reason.PIN_NOT_FLOATING if names_a_commit else None
        warn_if_the_opt_in_is_redundant(
            reference,
            marker,
            log,
            Scope.FLOATING_PIN,
            pin_does_not_float,
            update_held_back=marker.ignores(Scope.UPDATE),
        )

    def _comment_names_no_ref(self, reference: Reference) -> bool:
        """Return whether the comment beside a commit SHA does not name a tag or branch, which leaves the SHA bare."""
        if not reference.current_sha or self.marker.ignores(Scope.UPDATE):  # A held-back reference asks GitHub nothing
            return False
        return self._ref_commit(reference).absent

    def _report_redundant_directives(
        self, reference: Reference, pin: DependencyVersion | None, *, names_a_commit: bool
    ) -> None:
        """Report as redundant the directives that decide nothing for a ref that does not name a version.

        Those are a cooldown on a commit SHA, a bound, a hash-drift opt-in that adopts nothing, and a `vulnerable`
        scope where the source reports vulnerabilities. A pin to a version tag has its bound and its `vulnerable`
        scope judged at that version instead.
        """
        marker, log = self.marker, self.log
        if pin is not None and pin.tag_name:
            log.warn_if_redundant_bound(replace(reference, current_version=pin.version), marker)
            return
        if names_a_commit and (cooldown := marker.as_written.directive_for(Scope.COOLDOWN)):
            log.redundant_directive(reference, cooldown, Reason.NO_COOLDOWN_FOR_A_COMMIT)
        no_drift = Reason.NO_DRIFT_FOR_A_COMMIT if names_a_commit else None
        warn_if_the_opt_in_is_redundant(
            reference, marker, log, Scope.HASH_DRIFT, no_drift, update_held_back=marker.ignores(Scope.UPDATE)
        )
        if bound := marker.as_written.version_bound_directive:
            reason = Reason.PINS_A_COMMIT if names_a_commit else Reason.FOLLOWS_A_BRANCH
            log.redundant_directive(reference, bound, reason)
        if reports_vulnerabilities(self.source, reference.pinned) and (
            vulnerable := marker.as_written.directive_for(Scope.VULNERABLE)
        ):
            log.redundant_directive(reference, vulnerable, Reason.NO_NAMED_VERSION_TO_CHECK_FOR_A_VULNERABILITY)

    def _ref_pin(self, reference: Reference, commit_sha: _CommitSha) -> DependencyVersion | None:
        """Return the ref's commit, named by a version tag or by the ref, or None to leave the reference as it is."""
        ref = reference.current_version
        if self.marker.ignores(Scope.UPDATE):
            return None
        found = self._ref_commit(reference)
        if (pinned := found.value) is None:
            if found.reason:  # A branch without a commit older than the cooldown is held back without a report
                self._report_unfetched_commit(reference, found, commit_sha)
            return None
        kept_floating_by = None if commit_sha.names_a_commit else floating_pin_cause(self.marker)
        if reference.current_sha and hash_drifted(pinned.sha, reference.current_sha):
            return self._moved_ref_pin(reference, pinned, kept_floating_by)
        if kept_floating_by is not None:
            self.log.keeping_ref(reference, pinned, kept_floating_by, _ref_kind(reference))
            return None
        if reference.current_sha and pinned.version == ref:
            return None  # Still pinned to the ref's commit
        self.log.pinned(reference, pinned)
        return pinned

    def _ref_commit(self, reference: Reference) -> LookedUp[DependencyVersion]:
        """Look up the commit to pin: a pinned branch's newest commit older than the cooldown, or the ref's commit."""
        dependency, ref = reference.dependency, reference.current_version
        if reference.current_sha and _ref_kind(reference) is RefKind.BRANCH:
            return newest_commit_past_cooldown(dependency, ref, cooldown_days(self.marker))
        return pinned_ref(dependency, ref)

    def _moved_ref_pin(
        self, reference: Reference, moved: DependencyVersion, kept_floating_by: OptInCause | None
    ) -> DependencyVersion | None:
        """Return the commit the ref moved to, if the reference adopts it, or None to leave the reference as it is.

        A branch never moves back, a tag moves to whichever commit it now points at.
        """
        kind = _ref_kind(reference)
        if kind is RefKind.BRANCH and not self._branch_moved_on(reference, moved):
            return None
        # A ref kept floating keeps following the ref.
        kept = DependencyVersion(reference.current_version, sha=moved.sha)
        pin = moved if kept_floating_by is None else kept
        adopted = self._drifted_pin(_DRIFT[kind], reference, pin, publication=moved.publication)
        if adopted is not None and adopted.tag_name:  # The reference comes to name a version rather than the ref
            self.log.pinned(reference, adopted)
        return adopted

    def _branch_moved_on(self, reference: Reference, moved: DependencyVersion) -> bool:
        """Return whether the branch moved on from its pin to the commit, reporting why when GitHub can't tell."""
        compared = moved_on(reference.dependency, reference.current_sha, moved.sha)
        if compared.reason:
            self.log.unchecked_branch_drift(reference, moved.sha, compared.reason)
        return bool(compared.value)

    def _report_unfetched_commit(
        self, reference: Reference, found: LookedUp[DependencyVersion], commit_sha: _CommitSha
    ) -> None:
        """Report why the ref's commit could not be fetched, or that a commit SHA's comment names a missing ref."""
        if reference.current_sha and found.absent:
            self.log.comment_naming_no_ref(reference)
        else:
            kind = RefKind.SHORT_COMMIT_SHA if commit_sha is _CommitSha.SHORT else _ref_kind(reference)
            self.log.unpinned_ref(reference, found.reason, kind)

    def _drifted_pin(
        self, kind: Drift, reference: Reference, pin: DependencyVersion, *, publication: Lookup[datetime]
    ) -> DependencyVersion | None:
        """Return the pin to re-pin the reference to now that its tag or branch moved to another commit, or None.

        The new commit is adopted only once it is older than the cooldown.
        """
        drifted = DriftedPin.from_reference(reference, new_sha=pin.sha)
        adopted = adopts_drift(
            kind, drifted, self.marker, self.log, publication=publication, report_undated=self.log.no_commit_date
        )
        return pin if adopted else None


@dataclass(frozen=True)
class PinUpdater:
    """How one kind of GitHub reference is spelled, whether it is checked for vulnerabilities, and its logger."""

    spell: Callable[[Reference, str, str], str]  # The reference, the commit SHA, and the comment naming it, if any
    logger: Logger
    checks_vulnerabilities: bool = False

    @property
    def _source(self) -> NewVersionGetter:
        """Return the getter this kind of reference resolves through."""
        return _get_latest_action_version if self.checks_vulnerabilities else get_latest_version

    def update_line(self, match: re.Match[str], location: Location, marker: Marker, dependency: str = "") -> str:
        """Return the line with the reference (re)pinned, or the line as it is when the reference stays put."""
        reference = github_reference(match, location, matched_dependency(match, dependency))
        latest = _PinResolver(marker, self.logger, self._source).latest_pin(reference)
        if latest is None:
            return match.string
        return replace_match(match, self.spell(reference, latest.sha, latest.tag_name or latest.version))

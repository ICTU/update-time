"""The decision of which commit a GitHub Action `uses:` or a pre-commit hook `rev:` is pinned to."""

import re
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from packaging.version import Version

from update_time.domain.cooldown import within_cooldown
from update_time.domain.dependency import DependencyVersion, is_valid
from update_time.domain.reference import DriftedPin, Reference, hash_drifted
from update_time.io.log import Logger
from update_time.markers.drift import report_drift
from update_time.markers.floating import floating_pin_cause
from update_time.markers.marker import Scope
from update_time.primitives.digest import COMMIT_SHA
from update_time.primitives.text import replace_match
from update_time.references.match import matched_dependency
from update_time.references.resolve import (
    cooldown_days,
    floating_pin_redundancy,
    latest_version,
    report_directives_that_set_nothing,
    report_project_checks,
)
from update_time.sources.github import commit_date, get_latest_version, pinned_branch, project

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.io.log import Drift
    from update_time.markers.marker import Marker
    from update_time.primitives.location import Location


def _github_reference(match: re.Match[str], location: Location, dependency: str) -> Reference:
    """Return the GitHub reference the match captured."""
    current_sha = match.group("sha") or ""
    version = match.group("version") if current_sha else match.group("tag")
    return Reference(dependency, version, location, current_sha)


def _latest_pin(reference: Reference, marker: Marker, log: Logger) -> DependencyVersion | None:
    """Return the version or branch to pin the GitHub reference to, or None to leave the reference as it is."""
    current_version, current_sha = reference.current_version, reference.current_sha
    if not is_valid(current_version):
        report_directives_that_set_nothing(marker, get_latest_version, reference, log)
        report_project_checks(reference, marker, log, project)
        is_branch = not re.fullmatch(COMMIT_SHA, current_version)
        if (reason := floating_pin_redundancy(marker, floats=is_branch)) is not None:
            log.redundant_directive(reference, marker.allow_directive(Scope.FLOATING_PIN), reason)
        return _branch_pin(reference, marker, log) if is_branch else None
    latest = latest_version(reference, get_latest_version, marker, log)
    if latest is None or not latest.sha:
        return None
    if not current_sha:
        log.pinned(reference, latest)
    elif Version(latest.version) != Version(current_version):
        log.new_version(reference, latest)
    elif not hash_drifted(latest.sha, current_sha):
        return None  # Already pinned and up to date
    else:
        # The reference names the version as the source spells it, so a rev frozen as v4.5.0 is reported as 4.5.0.
        # It then names the version the pin does, so an adopted move logs as adopted drift rather than as a pin.
        moved = replace(reference, current_version=latest.version)
        return _drifted_pin(Logger.TAG_DRIFT, moved, latest, marker, log)
    return latest


def _branch_pin(reference: Reference, marker: Marker, log: Logger) -> DependencyVersion | None:
    """Return the branch's commit, named by a version tag or by the branch, or None to leave the reference as it is."""
    branch = reference.current_version
    if marker.ignores(Scope.UPDATE):
        return None
    pinned, reason = pinned_branch(reference.dependency, branch)
    if pinned is None:
        log.unpinned_branch(reference, reason)
        return None
    if (cause := floating_pin_cause(marker)) is not None:
        if reference.current_sha and hash_drifted(pinned.sha, reference.current_sha):
            kept = DependencyVersion(branch, sha=pinned.sha)  # A branch kept floating keeps following the branch.
            return _drifted_pin(Logger.BRANCH_DRIFT, reference, kept, marker, log)
        log.keeping_branch(reference, pinned, cause)
        return None
    if reference.current_sha:
        return _repinned_branch(reference, pinned, marker, log)
    log.pinned(reference, pinned)
    return pinned


def _repinned_branch(
    reference: Reference, pinned: DependencyVersion, marker: Marker, log: Logger
) -> DependencyVersion | None:
    """Return the new pin of a branch already pinned to a commit, or None when the pin still holds."""
    if hash_drifted(pinned.sha, reference.current_sha):
        return _drifted_pin(Logger.BRANCH_DRIFT, reference, pinned, marker, log)
    if pinned.version == reference.current_version:
        return None  # Still pinned to the commit the branch points at
    log.pinned(reference, pinned)
    return pinned


def _drifted_pin(
    kind: Drift, reference: Reference, pin: DependencyVersion, marker: Marker, log: Logger
) -> DependencyVersion | None:
    """Return the pin to re-pin the reference to now that its tag or branch moved to another commit, or None.

    The new commit is adopted only once it is past the cooldown. Adopting a branch's move onto a commit a version
    tag names logs a pin, since the reference then names that version rather than the branch.
    """
    renamed = pin.version != reference.current_version
    drifted = DriftedPin.from_reference(reference, new_sha=pin.sha)
    warn = partial(log.drift, kind, drifted)

    def adopt(cause: str) -> None:
        """Log a pin when the reference comes to name another version, and adopted drift otherwise."""
        if renamed:
            log.pinned(reference, pin)
        else:
            log.adopted_drift(kind, drifted, cause)

    def past_cooldown() -> bool:
        """Return whether the commit the pin moved to is dated, and dated before the cooldown."""
        # The branch's commit was fetched by the branch's name, so dating it by that name reuses that response.
        commit_ref = reference.current_version if kind is Logger.BRANCH_DRIFT else pin.sha
        committed, reason = commit_date(reference.dependency, commit_ref)
        if committed is None:
            log.no_commit_date(reference.dependency, pin.sha, reason)
            return False
        return not within_cooldown(committed, cooldown_days(marker))

    return pin if report_drift(marker, warn, adopt, past_cooldown) else None


@dataclass(frozen=True)
class PinUpdater:
    """How one kind of GitHub reference, a `uses:` or a `rev:`, is spelled, and the logger it reports to."""

    spell: Callable[[Reference, DependencyVersion], str]
    logger: Logger

    def update_line(self, match: re.Match[str], location: Location, marker: Marker, dependency: str = "") -> str:
        """Return the line with the reference (re)pinned, or the line as it is when the reference stays put."""
        reference = _github_reference(match, location, matched_dependency(match, dependency))
        latest = _latest_pin(reference, marker, self.logger)
        if latest is None:
            return match.string
        return replace_match(match, self.spell(reference, latest))

"""Shared decision for updating a GitHub reference pinned to a commit SHA with a version comment.

A GitHub Action `uses:` pinned as `<sha> # v4.1.1` and a pre-commit hook `rev:` pinned as `<sha> # frozen: v4.5.0`
are the same kind of reference: a GitHub repository pinned to a commit SHA, with the human-readable version travelling
in a trailing comment. Both are (re)pinned to the latest version's commit SHA the same way; only the surrounding
syntax — and so how the new reference text is spelled — differs. This module owns what the two share: reading the
reference from a match, deciding the version to pin it to, and rewriting the line. Each updater supplies only how its
own reference is spelled.
"""

from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from packaging.version import Version

from update_time.domain.dependency import is_valid
from update_time.domain.drift import DriftedPin, hash_drifted, report_drift
from update_time.domain.reference import Reference
from update_time.primitives.text import replace_match
from update_time.references.resolve import latest_version
from update_time.references.rewrite import matched_dependency
from update_time.sources.github import get_latest_version

if TYPE_CHECKING:
    import re
    from collections.abc import Callable

    from update_time.domain.dependency import DependencyVersion
    from update_time.domain.marker import Marker
    from update_time.io.log import Logger
    from update_time.primitives.location import Location


def _sha_pinned_reference(match: re.Match[str], location: Location, dependency: str) -> Reference:
    """Return the SHA-pinned GitHub reference the match captured.

    A pinned reference carries its version in the trailing comment's `version` group, an unpinned one in its `tag`
    group, so which group holds the version follows from whether `sha` matched.
    """
    current_sha = match.group("sha") or ""
    version = match.group("version") if current_sha else match.group("tag")
    return Reference(dependency, version, location, current_sha)


def _latest_pin(reference: Reference, marker: Marker, log: Logger) -> DependencyVersion | None:
    """Return the latest version to (re)pin the GitHub reference to, or None to leave it unchanged.

    Which version to update to is `latest_version`'s decision, resolving through `sources.github`; layered on top
    here is what is specific to a SHA-pinned reference. Returns None — leave the reference as it is — when the
    marker holds the update back, when no commit SHA is available to pin to, or when the reference is already up
    to date. It returns None for an invalid current version too, such as a branch name or a bare SHA without a
    version comment. A reference that stays on its version is handed to `_drifted_pin`, since its tag may have moved.
    Otherwise it logs the change (a pin for a previously unpinned reference, a new version for an already-pinned one)
    and returns the resolved version for the caller to format into its own syntax.
    """
    current_version, current_sha = reference.current_version, reference.current_sha
    if not is_valid(current_version):
        return None
    latest = latest_version(reference, get_latest_version, marker, log)
    if latest is None or not latest.sha:
        return None
    if not current_sha:
        log.pinned(reference, latest)
    elif Version(latest.version) != Version(current_version):
        log.new_version(reference, latest)
    else:
        return _drifted_pin(reference, latest, marker, log)
    return latest


def _drifted_pin(
    reference: Reference, latest: DependencyVersion, marker: Marker, log: Logger
) -> DependencyVersion | None:
    """Return the version to re-pin the reference to when its tag has moved, or None to leave its pin as it is.

    A reference that stays on its version can still have had that version's tag moved onto another commit. Whether
    the commit it moved to is adopted or only warned about is `report_drift`'s decision, and the version is returned
    for re-pinning only when it is adopted.
    """
    if not hash_drifted(latest.sha, reference.current_sha):
        return None  # Already pinned and up to date
    # The version is reported as the source spells it, which the reference's own spelling need only equal, not match.
    drifted = DriftedPin(**vars(replace(reference, current_version=latest.version)), new_sha=latest.sha)
    adopted = report_drift(marker, partial(log.tag_drift, drifted), partial(log.adopted_tag_drift, drifted))
    return latest if adopted else None


@dataclass(frozen=True)
class PinUpdater:
    """Everything needed to update one kind of GitHub-SHA-pinned reference: how it is spelled, and where it reports.

    `spell` turns the reference and the version it is being pinned to into that reference's own syntax — a `uses:`
    for a GitHub Action, a `rev:` for a pre-commit hook.
    """

    spell: Callable[[Reference, DependencyVersion], str]
    logger: Logger

    def update_line(self, match: re.Match[str], location: Location, marker: Marker, dependency: str = "") -> str:
        """Return the line with the reference (re)pinned to the latest version, or unchanged when it stays put.

        Unchanged covers each case `_latest_pin` declines: an invalid current version, a marker holding the update
        back, no commit SHA to pin to, and a reference already pinned and up to date. The dependency comes from the
        regexp's `dependency` group; a `rev:` takes it from the `repo:` above, so it names it in `dependency` instead.
        """
        reference = _sha_pinned_reference(match, location, matched_dependency(match, dependency))
        latest = _latest_pin(reference, marker, self.logger)
        if latest is None:
            return match.string
        return replace_match(match, self.spell(reference, latest))

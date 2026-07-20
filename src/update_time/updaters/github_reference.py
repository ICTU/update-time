"""Shared decision for updating a GitHub reference pinned to a commit SHA with a version comment.

A GitHub Action `uses:` pinned as `<sha> # v4.1.1` and a pre-commit hook `rev:` pinned as `<sha> # frozen: v4.5.0`
are the same kind of reference: a GitHub repository pinned to a commit SHA, with the human-readable version travelling
in a trailing comment. Both are (re)pinned to the latest version's commit SHA the same way; only the surrounding
syntax — and so how the new reference text is spelled — differs. This module owns that shared decision, leaving each
updater to parse its own version group and spell its own output.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from packaging.version import Version

from update_time.domain.version import is_valid
from update_time.sources.github import get_latest_version

if TYPE_CHECKING:
    from pathlib import Path

    from update_time.domain.marker import Marker
    from update_time.domain.version import DependencyVersion
    from update_time.io.log import Logger


@dataclass(frozen=True)
class GitHubReference:
    """A GitHub repository reference as a file pins it: the `owner/repository`, its version, and its commit SHA.

    `current_sha` is the pinned commit SHA, or None when the reference is still an unpinned version tag. The two
    updaters extract these from differently named regex groups (an action's `uses:`, a hook's `rev:`), then hand a
    uniform reference to `latest_pin`.
    """

    dependency: str
    current_version: str
    current_sha: str | None


def latest_pin(reference: GitHubReference, marker: Marker, path: Path, log: Logger) -> DependencyVersion | None:
    """Return the latest version to (re)pin the GitHub reference to, or None to leave it unchanged.

    Mirrors the decision `io.rewrite._Rewriter.update_line` makes for image references, minus the digest-drift
    handling that only applies to a re-pushed image tag: validate the current version, warn about a redundant bound
    and about staleness (unless held back), then resolve the latest version. Returns None — leave the reference as it
    is — when the version is invalid (a branch name, or a bare SHA without a version comment), the marker holds the
    update back, no commit SHA is available to pin to, or the reference is already pinned and up to date. Otherwise it
    logs the change (a pin for a previously unpinned reference, a new version for an already-pinned one) and returns
    the resolved version for the caller to format into its own syntax.
    """
    dependency, current_version, current_sha = reference.dependency, reference.current_version, reference.current_sha
    if not is_valid(current_version):
        return None
    log.warn_if_redundant_bound(dependency, marker, current_version, path)
    latest = get_latest_version(dependency, current_version, marker.version_bound)
    if not marker.ignore_stale:
        log.warn_if_stale(dependency, latest, path)
    if marker.ignore_update or not latest.sha:
        return None
    if current_sha is None:
        log.pinned(dependency, latest, path)
    elif Version(latest.version) != Version(current_version):
        log.new_version(dependency, latest, path)
    else:
        return None  # Already pinned and up to date
    return latest

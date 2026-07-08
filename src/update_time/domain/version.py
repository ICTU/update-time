"""Dependency version class."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime

# The name of a dependency as it appears in a manifest: a Docker image (`cimg/python`), a GitHub action
# (`actions/checkout`), a PyPI or npm package, etc. A version string is an arbitrary version or tag (`3.14.2`, `v1`).
type DependencyName = str
type VersionString = str
# The contract every updater binds and every source implements: given a dependency and its current version, return
# the latest version to use. Implemented by e.g. sources.oci.get_latest_tag and sources.pypi.get_latest_version.
type NewVersionGetter = Callable[[DependencyName, VersionString], DependencyVersion]


def is_valid(version: VersionString) -> bool:
    """Return whether the version is valid."""
    try:
        Version(version)
    except InvalidVersion:
        return False
    return True


@dataclass(frozen=True)
class DependencyVersion:
    """A version of a dependency."""

    version: VersionString  # Arbitrary version string as returned by a source (PyPI, Docker Hub, GitHub releases, ...)
    changes: str = ""  # Changelog for this version, empty when none could be found
    sha: str = ""
    published: datetime | None = None  # Publication date of this (candidate) version, when known
    newest_published: datetime | None = None  # Publication date of the dependency's newest release, for staleness

    def digest_differs_from(self, sha: str) -> bool:
        """Return whether this version resolved a digest that differs from an already-pinned one.

        Used to detect a re-pushed tag: when the version is unchanged, a differing digest means the tag was rebuilt
        under the same name. A version without a resolved digest never counts as differing (nothing to compare).
        """
        return bool(self.sha) and self.sha != sha


def first_eligible[Candidate](
    candidates: Iterable[Candidate],
    resolve: Callable[[Candidate], DependencyVersion | None],
    current_version: VersionString,
) -> DependencyVersion:
    """Return the first candidate that resolves to an eligible version, or the current version unchanged.

    Every source (PyPI, OCI, jsDelivr) picks a new version the same way: order the candidate versions newest-first,
    then walk them until one is eligible. Eligibility can only be decided after fetching the candidate's metadata
    (its publication date for the cooldown, a digest or integrity hash to pin, whether it was yanked), so `resolve`
    does that fetch and returns the resulting `DependencyVersion`, or None to skip the candidate and try the next
    (older) one. When no candidate is eligible the current version is returned unchanged.
    """
    for candidate in candidates:
        if (version := resolve(candidate)) is not None:
            return version
    return DependencyVersion(version=current_version)

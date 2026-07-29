"""Dependency version primitives."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime

    from _typeshed import SupportsRichComparison

# The name of a dependency as it appears in a manifest: a Docker image (`cimg/python`), a GitHub action
# (`actions/checkout`), a PyPI or npm package, etc. A version string is an arbitrary version or tag (`3.14.2`, `v1`).
type DependencyName = str
type VersionString = str

# A `sha256:` digest as it appears in image references (`python:3.14@sha256:…`) and log messages: the `sha256:`
# prefix plus the digest's 64 hexadecimal characters. Shared by the image-reference pattern in `sources/oci.py` and
# the log highlighter in `io/log.py`, so the two always agree on what a digest looks like.
SHA256_HEX_CHARS = 64
SHA256_DIGEST = rf"sha256:[0-9a-f]{{{SHA256_HEX_CHARS}}}"


def is_valid(version: VersionString) -> bool:
    """Return whether the version is valid."""
    try:
        Version(version)
    except InvalidVersion:
        return False
    return True


@dataclass(frozen=True)
class Yank:
    """A version's withdrawal state: whether it was withdrawn — yanked on PyPI, deprecated on npm — and why."""

    yanked: bool = False
    reason: str = ""  # The maintainer's reason, empty when none was given


@dataclass(frozen=True)
class DependencyVersion:
    """A version of a dependency."""

    version: VersionString  # Arbitrary version string as returned by a source (PyPI, Docker Hub, GitHub releases, ...)
    changes: str = ""  # Changelog for this version, empty when none could be found
    sha: str = ""
    published: datetime | None = None  # Publication date of this (candidate) version, when known
    newest_published: datetime | None = None  # Publication date of the dependency's newest release, for staleness
    yank: Yank = Yank()  # The version's withdrawal state (yanked on PyPI, deprecated on npm)


@dataclass(frozen=True)
class Reference:
    """A pinned reference as a file records it: a dependency and the version it is currently pinned to.

    `current_sha` is the commit SHA the reference is pinned to when it carries one in a `<sha> # <version>` form (a
    GitHub Action `uses:` or pre-commit hook `rev:`); it is empty for a reference that is still an unpinned version
    tag or that records no SHA of its own.
    """

    dependency: DependencyName
    current_version: VersionString
    current_sha: str = ""


def first_eligible[Candidate: SupportsRichComparison](
    candidates: Iterable[Candidate],
    resolve: Callable[[Candidate], DependencyVersion | None],
    current_version: VersionString,
) -> DependencyVersion:
    """Return the highest candidate that resolves to an eligible version, or the current version unchanged.

    Every source (PyPI, OCI, jsDelivr, GitHub) picks a new version the same way: order the candidates newest-first,
    then walk them until one is eligible. `first_eligible` owns that ordering — the candidates are comparable, so it
    sorts them itself (newest first) and sources need only supply the set. Eligibility can only be decided after
    fetching the candidate's metadata (its publication date for the cooldown, a digest or integrity hash to pin,
    whether it was yanked), so `resolve` does that fetch and returns the resulting `DependencyVersion`, or None to
    skip the candidate and try the next (older) one. When no candidate is eligible the current version is returned
    unchanged.
    """
    for candidate in sorted(candidates, reverse=True):
        if (version := resolve(candidate)) is not None:
            return version
    return DependencyVersion(version=current_version)

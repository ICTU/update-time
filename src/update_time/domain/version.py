"""Dependency version class."""

import enum
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime

    from _typeshed import SupportsRichComparison

# The name of a dependency as it appears in a manifest: a Docker image (`cimg/python`), a GitHub action
# (`actions/checkout`), a PyPI or npm package, etc. A version string is an arbitrary version or tag (`3.14.2`, `v1`).
type DependencyName = str
type VersionString = str
# The contract every updater binds and every source implements: given a dependency, its current version, and a
# version filter (NO_BOUND when the reference carries no bound), return the latest version to use. Implemented by
# e.g. sources.oci.get_latest_tag and sources.pypi.get_latest_version.
type NewVersionGetter = Callable[[DependencyName, VersionString, VersionFilter], DependencyVersion]

# A `sha256:` digest as it appears in image references (`python:3.14@sha256:…`) and log messages: the `sha256:`
# prefix plus the digest's 64 hexadecimal characters. Shared by the image-reference pattern in `sources/oci.py` and
# the log highlighter in `io/log.py`, so the two always agree on what a digest looks like.
SHA256_HEX_CHARS = 64
SHA256_DIGEST = rf"sha256:[0-9a-f]{{{SHA256_HEX_CHARS}}}"

# A version higher than any real one, used to probe whether a bound caps anything above the current version.
_SENTINEL_VERSION = Version("9" * 18)


def is_valid(version: VersionString) -> bool:
    """Return whether the version is valid."""
    try:
        Version(version)
    except InvalidVersion:
        return False
    return True


# The main version component of a version string that is not a version on its own, such as an image tag: the first
# run of characters that starts with a digit and stops before a `-` (`python3.12-bookworm-slim` -> `3.12`). This
# mirrors the tag grammar in `sources/oci.py`, so a bound on an image reference is classified against the same main
# version it filters on.
_MAIN_VERSION_COMPONENT = re.compile(r"\d[^-]*")


def _parse_version(version: VersionString) -> Version | None:
    """Parse the version, falling back to its main version component (e.g. for an image tag), or None."""
    if is_valid(version):
        return Version(version)
    component = _MAIN_VERSION_COMPONENT.search(version)
    if component is not None and is_valid(component.group()):
        return Version(component.group())
    return None


class Redundancy(enum.Enum):
    """The two ways a version bound can be redundant for the current version (see `VersionFilter.redundancy`)."""

    NO_EFFECT = "never has an effect"  # The bound admits the current version and everything above it.
    BLOCKS_ALL = "blocks every update"  # The bound admits neither the current version nor anything above it.


@dataclass(frozen=True)
class VersionFilter:
    """A version bound from an `allow[update…]` / `ignore[update…]` marker: keep or drop matching update candidates.

    `allow` True keeps only candidates whose version satisfies `specifier`; False drops them (the `ignore` framing).
    Frozen and hashable (a `SpecifierSet` is hashable) so it threads through the `@cache`d source lookups.
    """

    specifier: SpecifierSet
    allow: bool

    def keeps(self, version: Version) -> bool:
        """Return whether a candidate version survives the bound."""
        return self.specifier.contains(version, prereleases=True) == self.allow

    def redundancy(self, current_version: VersionString) -> Redundancy | None:
        """Classify the bound for the current version, or None when it is live (bounds some updates but not all).

        A bound that keeps the current version and every version above it caps nothing (`NO_EFFECT`); one that keeps
        no version above the current one blocks every update (`BLOCKS_ALL`); anything in between is a genuine ceiling
        or floor that will bite, and is left alone. "Every version above" is tested by sampling each region above the
        current version (see `_probe_versions`), so a bounded range that sits entirely above the current version —
        `allow[update>=3.13,<3.15]` on a `3.12` pin — is correctly seen as live rather than blocking. An image tag
        with a label prefix or variant suffix (`python3.12-bookworm-slim`) is classified by its main version
        component, the same component the bound filters on.
        """
        if not self.specifier:
            return None  # An empty (keep-all) bound is the no-op default, not a real bound to report on.
        if (current := _parse_version(current_version)) is None:
            return None
        keeps_above = [self.keeps(version) for version in self._probe_versions(current)]
        if self.keeps(current) and all(keeps_above):
            return Redundancy.NO_EFFECT
        if not any(keeps_above):
            return Redundancy.BLOCKS_ALL
        return None

    def _probe_versions(self, current: Version) -> set[Version]:
        """Return versions that sample every region strictly above `current`, so `keeps` can be evaluated across it.

        `keeps` only changes value at the specifier's boundary versions, so the probes are each boundary itself
        (`current` and an arbitrarily large sentinel included) plus two probes derived from its base release: the
        base itself and a hair above it, to catch a region opened by a `>` bound or a `.*`/`~=` interval. The extra
        probes are derived from the base release (epoch and release segments) because appending to that is valid for
        any boundary, whereas appending to a version with a pre/post/dev segment (`4.15.0.post1`) would not parse.
        The cost is that regions between sub-release boundaries are sampled at release granularity, which matches
        the sources: they never select pre-release candidates anyway.
        """
        boundaries = {current, _SENTINEL_VERSION}
        for specifier in self.specifier:
            try:
                boundaries.add(Version(specifier.version.removesuffix(".*")))
            except InvalidVersion:
                continue  # A non-PEP 440 boundary (e.g. an arbitrary-equality `===` clause) has no version to probe.
        probes = set(boundaries)
        for boundary in boundaries:
            base = f"{boundary.epoch}!{'.'.join(str(part) for part in boundary.release)}"
            probes.update((Version(base), Version(f"{base}.0.0.0.1")))
        return {version for version in probes if version > current}


def parse_version_filter(specifier: str, *, allow: bool) -> VersionFilter | None:
    """Parse a PEP 440 specifier into a VersionFilter, or None when it is not a valid specifier."""
    try:
        return VersionFilter(SpecifierSet(specifier), allow=allow)
    except InvalidSpecifier:
        return None


# The absence of a version bound, represented as a keep-all filter (an empty specifier matches every version) so that
# `version_filter` is never None: sources apply it uniformly and it is the default for an unmarked reference.
NO_BOUND = VersionFilter(SpecifierSet(""), allow=True)


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

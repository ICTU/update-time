"""Dependency version class."""

import enum
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from packaging.specifiers import SpecifierSet
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


class Verb(enum.StrEnum):
    """The two verbs of the marker language, exact complements: `allow` keeps what it names, `ignore` drops it.

    A `StrEnum` whose members format to the verb tokens `allow` and `ignore` as they appear in a directive.
    """

    ALLOW = "allow"
    IGNORE = "ignore"


class UpdateLevel(enum.IntEnum):
    """The version components a level-based bound can name, each valued with the component's position.

    A major update changes the first component, a minor update the second, and a patch update the third (or a later
    one). The names are positional, not semantic: a project may ship breaking changes in releases that bump the
    second component (CPython, calver), making "no breaking updates" `ignore[minor-update]`, not
    `ignore[major-update]`.
    """

    MAJOR = 0
    MINOR = 1
    PATCH = 2

    def __str__(self) -> str:
        """Return the level as it appears in a `<level>-update` marker item, so parsing and rendering agree."""
        return self.name.lower()


@dataclass(frozen=True)
class VersionFilter:
    """A version bound from an `allow[…]` / `ignore[…]` marker directive: keep or drop matching update candidates.

    The filter holds exactly one of two parsed forms, recognised and constructed by `parse_bound`. A `specifier`
    is an absolute bound (`update<3.13`): with `Verb.ALLOW` the filter keeps only candidates whose version
    satisfies it, with `Verb.IGNORE` it drops them. A `level` is a level-based bound (`minor-update`) that limits
    how far an update may move relative to whatever version is currently pinned (see `UpdateLevel` for the levels
    and `_resolve` for the anchoring). The `item` is the bracket item as its creator spelled it, kept for rendering
    (see `__str__`) and excluded from equality, so bounds that mean the same thing compare (and cache) as equal
    however they are spelled. Frozen and hashable so it threads through the `@cache`d source lookups.
    """

    verb: Verb
    specifier: SpecifierSet | None = None
    level: UpdateLevel | None = None
    item: str = field(compare=False, default="")

    def __str__(self) -> str:
        """Return the filter as the marker directive that expresses it, e.g. `allow[update<3.13]`.

        The item renders exactly as its creator spelled it. For a bound parsed from a marker that is the user's own
        spelling, so a log message never shows a bound they did not enter.
        """
        return f"{self.verb}[{self.item}]"

    def keeps(self, version: Version, current_version: VersionString) -> bool:
        """Return whether a candidate version survives the bound for a reference currently at `current_version`.

        An absolute bound is unaffected by the current version; a level-based bound is anchored to it first.
        """
        if (specifier := self.specifier) is None:
            return self._resolve(current_version).keeps(version, current_version)
        return specifier.contains(version, prereleases=True) == (self.verb is Verb.ALLOW)

    def _resolve(self, current_version: VersionString) -> VersionFilter:
        """Return a level-based bound anchored to the current version: its concrete, absolute equivalent.

        A level-based bound may not change the current version's leading components: those before the named level
        for `allow`, those up to and including it for `ignore`. The fixed components are pinned with a
        `==<prefix>.*` specifier, so `ignore[minor-update]` on `3.12.1` resolves to `==3.12.*` — and to `==3.13.*`
        once the reference has migrated to `3.13`. A component the current version is missing counts as zero,
        matching how version comparison pads it, so `ignore[minor-update]` on `22` resolves to `==22.0.*`. With
        every component fixed the current release is pinned exactly, blocking every update. A bound that fixes
        nothing, or whose current version has no parsable version to anchor to, resolves to the keep-all `NO_BOUND`.
        """
        level = cast("UpdateLevel", self.level)  # `keeps` and `redundancy` only anchor level-based bounds.
        fixed = level + (0 if self.verb is Verb.ALLOW else 1)
        if fixed == 0 or (current := _parse_version(current_version)) is None:
            return NO_BOUND
        release = current.release + (0,) * (fixed - len(current.release))
        if fixed == len(UpdateLevel):  # Every component is fixed: pin the current release exactly.
            pinned, wildcard = release, ""
        else:
            pinned, wildcard = release[:fixed], ".*"
        epoch = f"{current.epoch}!" if current.epoch else ""
        specifier = f"=={epoch}{'.'.join(str(component) for component in pinned)}{wildcard}"
        return VersionFilter(Verb.ALLOW, SpecifierSet(specifier))

    def redundancy(self, current_version: VersionString) -> Redundancy | None:
        """Classify the bound for the current version, or None when it is live (bounds some updates but not all).

        A level-based bound is classified by its anchored equivalent, so `allow[major-update]` comes out as never
        having an effect and `ignore[patch-update]` as blocking every update. A bound that keeps the current
        version and every version above it caps nothing (`NO_EFFECT`) — the empty (keep-all) bound trivially so;
        whether an empty bound is worth reporting on is the caller's call, since it may be the no-op default of an
        unmarked reference. A bound that keeps no version above the current one blocks every update (`BLOCKS_ALL`);
        anything in between is a genuine ceiling or floor that will bite, and is left alone. "Every version above"
        is tested by sampling each region above the current version (see `_probe_versions`), so a bounded range
        that sits entirely above the current version — `allow[update>=3.13,<3.15]` on a `3.12` pin — is correctly
        seen as live rather than blocking.
        """
        if self.level is not None:
            return self._resolve(current_version).redundancy(current_version)
        if (current := _parse_version(current_version)) is None:
            return None
        keeps_above = [self.keeps(version, current_version) for version in self._probe_versions(current)]
        if self.keeps(current, current_version) and all(keeps_above):
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
        for specifier in self.specifier or ():
            try:
                boundaries.add(Version(specifier.version.removesuffix(".*")))
            except InvalidVersion:
                continue  # A non-PEP 440 boundary (e.g. an arbitrary-equality `===` clause) has no version to probe.
        probes = set(boundaries)
        for boundary in boundaries:
            base = f"{boundary.epoch}!{'.'.join(str(part) for part in boundary.release)}"
            probes.update((Version(base), Version(f"{base}.0.0.0.1")))
        return {version for version in probes if version > current}


def parse_bound(verb: Verb, item: str) -> VersionFilter | None:
    """Parse a marker item into a version filter, or None when the item is not a bound.

    An `update` bound whose specifier is unparsable raises `InvalidSpecifier` rather than returning None, so a
    caller can tell a malformed bound (which it should report) from an item that is simply not a bound. This is the
    sole classification of the item — the marker parser reads the verdict off the return value and the exception
    type instead of re-testing the item's shape — and, apart from the module's own `NO_BOUND` and `_resolve`, the
    sole construction of filters, so a filter always holds exactly one of its two parsed forms.
    """
    if (level := next((level for level in UpdateLevel if item == f"{level}-update"), None)) is not None:
        return VersionFilter(verb, level=level, item=item)
    if item.startswith("update"):
        return VersionFilter(verb, SpecifierSet(item.removeprefix("update")), item=item)
    return None


# The absence of a version bound, represented as a keep-all filter (an empty specifier matches every version) so that
# `version_filter` is never None: sources apply it uniformly and it is the default for an unmarked reference.
NO_BOUND = VersionFilter(Verb.ALLOW, SpecifierSet(""), item="update")


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

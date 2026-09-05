"""The bound on which versions a reference may update to, and the contract every source implements to resolve one."""

import enum
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from update_time.domain.dependency import (
    MAIN_VERSION,
    DependencyName,
    DependencyVersion,
    VersionString,
    is_valid,
)

if TYPE_CHECKING:
    from typing import Protocol

    class NewVersionGetter(Protocol):
        """The contract every updater binds and every source implements, resolving the version to update to."""

        def __call__(
            self,
            dependency: DependencyName,
            current_version: VersionString,
            version_bound: VersionBound,
            cooldown_days: int,
            /,
            *,
            check_archival: bool,
        ) -> DependencyVersion:
            """Return the version to update the dependency to.

            The bound and the cooldown are the two constraints on the candidates, by version and by age.
            `check_archival` is the run's archival setting, so a source that pays a request to answer it can skip
            that request rather than reading the setting itself. A source that reports no archival ignores it.
            """


# A version higher than any real one, used to probe whether a bound caps anything above the current version.
_SENTINEL_VERSION = Version("9" * 18)

_MAIN_VERSION_COMPONENT = re.compile(MAIN_VERSION)


def _parse_version(version: VersionString) -> Version | None:
    """Parse the version, or its main version component (e.g. for an image tag), or None if neither parses."""
    if is_valid(version):
        return Version(version)
    component = _MAIN_VERSION_COMPONENT.search(version)
    if component is not None and is_valid(component.group()):
        return Version(component.group())
    return None


class Redundancy(enum.StrEnum):
    """The two ways a version bound can be redundant for the current version (see `VersionBound.redundancy`)."""

    NO_EFFECT = "never has an effect"  # The bound admits the current version and everything above it.
    BLOCKS_ALL = "blocks every update"  # The bound admits neither the current version nor anything above it.


class Verb(enum.StrEnum):
    """The two verbs of the marker language, exact complements: `allow` keeps what it names, `ignore` drops it."""

    ALLOW = "allow"
    IGNORE = "ignore"


class UpdateLevel(enum.IntEnum):
    """The version components a level-based bound can name, each valued with the component's position."""

    MAJOR = 0
    MINOR = 1
    PATCH = 2

    def __str__(self) -> str:
        """Return the level as it appears in a `<level>-update` marker item, so parsing and rendering agree."""
        return self.name.lower()


@dataclass(frozen=True)
class VersionBound:
    """A constraint on the versions a reference may update to: keep the candidates it matches, or drop them.

    The bound holds exactly one of two forms: a `specifier` is absolute (`update<3.13`), a `level` is relative to
    whatever version is currently pinned (`minor-update`, anchored by `_resolve`). The `item` is the text its
    creator spelled, compared as no part of the bound, so two spellings of one bound are equal and cache alike.
    """

    verb: Verb
    specifier: SpecifierSet | None = None
    level: UpdateLevel | None = None
    item: str = field(compare=False, default="")

    def keeps(self, version: Version, current_version: VersionString) -> bool:
        """Return whether a candidate version survives the bound for a reference currently at `current_version`.

        An absolute bound is unaffected by the current version; a level-based bound is anchored to it first.
        """
        if (specifier := self.specifier) is None:
            return self._resolve(current_version).keeps(version, current_version)
        return specifier.contains(version, prereleases=True) == (self.verb is Verb.ALLOW)

    def _resolve(self, current_version: VersionString) -> VersionBound:
        """Return a level-based bound anchored to the current version: its concrete, absolute equivalent.

        The bound may not change the components before the named level, and for `ignore` the level's own component
        too. Those are pinned with a `==<prefix>.*` specifier, so `ignore[minor-update]` on `3.12.1` resolves to
        `==3.12.*`, and on `3.13.0` to `==3.13.*`. A missing component counts as zero, so `22` resolves to
        `==22.0.*`. A bound that fixes nothing, or a current version that will not parse, resolves to `NO_BOUND`.
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
        return VersionBound(Verb.ALLOW, SpecifierSet(specifier))

    def redundancy(self, current_version: VersionString) -> Redundancy | None:
        """Classify the bound for the current version, or None when it is live (bounds some updates but not all).

        A level-based bound is classified by its anchored equivalent, so `allow[major-update]` never has an effect
        and `ignore[patch-update]` blocks every update. "Every version above" is sampled per region rather than
        enumerated (see `_probe_versions`), so `allow[update>=3.13,<3.15]` on a `3.12` pin comes out live rather
        than blocking.
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

        `keeps` only changes value at the specifier's boundary versions, so each boundary is a probe. A region
        opened by a `>` bound or a `.*`/`~=` interval needs two more, taken from the boundary's base release
        because appending to a pre/post/dev segment (`4.15.0.post1`) would not parse. The cost is that sub-release
        regions are sampled at release granularity.
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


# The absence of a bound, as a keep-all bound (an empty specifier matches every version), so `version_bound` is
# never None. It is the default for an unmarked reference.
NO_BOUND = VersionBound(Verb.ALLOW, SpecifierSet(""), item="update")

# The bound an `ignore[update]` expresses: the complement of `NO_BOUND`, dropping every version. A source given it
# finds no candidate and keeps the current version, so a yanked pin on a frozen reference is still detected.
BLOCK_ALL_UPDATES = VersionBound(Verb.IGNORE, SpecifierSet(""), item="update")

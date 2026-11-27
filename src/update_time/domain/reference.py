"""References as a file records them, what a source resolved for them, and whether a hash pin drifted."""

from dataclasses import dataclass, field, fields, replace
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Final, Self

from .cooldown import COOLDOWN
from .dependency import NO_PROJECT, PinnedDependency
from .marker import NO_MARKER, Marker, Threshold
from .staleness import STALE_AFTER
from .vulnerability import VULNERABILITY_LEVEL

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    from update_time.primitives.location import Location

    from .dependency import DependencyName, DependencyVersion, Project, VersionString

# A variable or a template standing in for part of a reference, whose value Update-time does not know: a `$VAR` or
# `${VAR}`, a Helm `{{ .Values.tag }}`, or a CircleCI `<< parameters.tag >>`. A `$VAR` takes every word character
# after its `$` and gives none back. Otherwise the regex tries every split between a variable and the characters
# around it, which takes seconds on a line that holds several variables and does not match.
VARIABLE: Final[str] = r"\$\w++|\$\{[^}]*\}|\{\{[^}]*\}\}|<<[^>]*>>"


@dataclass(frozen=True)
class Reference:
    """A reference holds what a file records: a dependency, the version it is pinned to, its line, and its marker.

    The line is part of the reference because every report about one names where to edit it, so the two are never
    apart for long.

    `current_sha` is the hash pin the reference records: the commit SHA of a GitHub Action `uses:` or a pre-commit
    hook `rev:`, or the digest of an image. It is empty for a reference that does not record one.
    `can_hold_a_hash_pin` says whether the reference's syntax has a place for one. A subclass declares its own fields
    keyword-only, because these carry a default and Python refuses the class otherwise.

    The marker holds the `# update-time:` directives steering what happens to the reference. It defaults to a marker
    that holds nothing back. A reference carries that default where it does not have a line of its own to hold one.
    """

    dependency: DependencyName
    current_version: VersionString
    location: Location
    current_sha: str = ""
    can_hold_a_hash_pin: bool = False
    marker: Marker = field(default=NO_MARKER, kw_only=True)

    @classmethod
    def from_reference(cls, reference: Reference, **resolved: object) -> Self:
        """Return this kind of reference, carrying what it shares with the given one and what a source resolved."""
        shared = {attribute.name for attribute in fields(cls)} & {attribute.name for attribute in fields(reference)}
        values = {name: getattr(reference, name) for name in shared}
        values.update(resolved)
        return cls(**values)

    @property
    def pinned(self) -> PinnedDependency:
        """Return the dependency and the version this reference pins it to."""
        return PinnedDependency(self.dependency, self.current_version)

    @property
    def cooldown_days(self) -> int:
        """Return the number of days the reference holds back what was published: its own cooldown, or the run's."""
        return self.marker.cooldown.value_or(COOLDOWN.get())

    @property
    def stale_after(self) -> int:
        """Return the number of days the reference is checked for staleness against: its own, or the run's."""
        return self.marker.stale.value_or(STALE_AFTER.get())

    @property
    def vulnerability_level(self) -> str:
        """Return the risk level the reference is warned about vulnerabilities from: its own, or the run's."""
        return self.marker.vulnerable.value_or(VULNERABILITY_LEVEL.get())


@dataclass(frozen=True, kw_only=True)
class DriftedPin(Reference):
    """A hash pin that no longer matches what it points at: its kind, and the hash it now resolves to."""

    kind: DriftKind
    new_sha: str


# What a delegating updater hands its checks: a function asking its source about the references of one file.
type ReferenceResolver[ReferenceT: Reference, ResolvedT: ResolvedReference] = Callable[
    [Iterable[ReferenceT]], Iterable[ResolvedT]
]


@dataclass(frozen=True, kw_only=True)
class ResolvedReference(Reference):
    """A resolved reference carries the version and the project a source resolved for it.

    The version is the one the run leaves the reference on. That is the new version when the run updated the
    reference, and the current version otherwise.
    """

    version: DependencyVersion
    project: Project = NO_PROJECT


def resolved_references[ReferenceT: Reference, ResolvedT: ResolvedReference](
    declared: Iterable[Iterable[ReferenceT]], resolve: ReferenceResolver[ReferenceT, ResolvedT]
) -> Iterator[ResolvedT]:
    """Yield what the resolver answers for the references of each file."""
    for references in declared:
        yield from resolve(references)


def combined_marker(references: Sequence[Reference], current_version: VersionString) -> Marker:
    """Return the references' markers combined into one, for a reference at the current version (see `Marker.combined`).

    Where a marker sets a cooldown, the longest cooldown applies, the run's counting for a reference that sets none.
    """
    marker = Marker.combined([reference.marker for reference in references], current_version)
    if marker.cooldown.value is None:
        return marker
    longest = max(references, key=lambda reference: reference.cooldown_days)
    return replace(marker, cooldown=Threshold(longest.cooldown_days, longest.marker.cooldown.directive))


class RefKind(StrEnum):
    """A kind of git ref: a tag, a branch, an abbreviated commit SHA, or a ref of unknown kind."""

    TAG = "tag"
    BRANCH = "branch"
    SHORT_COMMIT_SHA = "short commit SHA"
    REF = "ref"


class DriftKind(Enum):
    """A kind of hash pin that can drift: a digest, or the commit that a tag, a branch, or a ref of unknown kind names.

    A commit drifts as the ref naming it moves, so its kind is the kind of that ref, and `DriftKind(ref)` looks it up.
    """

    DIGEST = None
    TAG = RefKind.TAG
    BRANCH = RefKind.BRANCH
    REF = RefKind.REF

    @property
    def separator(self) -> str:
        """Return the separator before the version that the drifted reference names."""
        return ":" if self is DriftKind.DIGEST else "@"

    @property
    def noun(self) -> str:
        """Return the word naming the kind of drift: the kind of ref that moved, or the digest."""
        return self.ref or "digest"

    @property
    def ref(self) -> RefKind | None:
        """Return the kind of the ref whose move drifts the pin, or None for a digest."""
        return self.value


def hash_drifted(resolved: str, pinned: str) -> bool:
    """Return whether a hash (resolved digest, commit SHA, or integrity hash) differs from the one already pinned."""
    return bool(resolved) and resolved != pinned

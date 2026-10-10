"""The Maven version ranges that hand a version bound to the versions plugin."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from packaging.version import InvalidVersion, Version

from update_time.domain.bound import Verb

if TYPE_CHECKING:
    from packaging.specifiers import Specifier

    from update_time.domain.bound import VersionBound
    from update_time.domain.dependency import VersionString

# Maven sorts a qualified version after the release it qualifies: `33.0.0-jre` after `33.0.0`. Appended to a version,
# this suffix gives a version that Maven sorts after every qualified form of it, and before the next release. A bound
# reads a version at the first numbers it holds, so a clause ending at a version ends after its qualified forms.
_PAST_QUALIFIERS = ".0.0.0.1"


@dataclass(frozen=True)
class _Boundary:
    """One end of an interval: a version, and whether the interval includes it."""

    version: Version
    included: bool


@dataclass(frozen=True)
class _Interval:
    """The versions between two boundaries, open-ended on a side without one."""

    lower: _Boundary | None = None
    upper: _Boundary | None = None

    def __and__(self, other: _Interval) -> _Interval:
        """Return the versions both intervals hold."""
        return _Interval(_tighter(self.lower, other.lower, after=True), _tighter(self.upper, other.upper, after=False))

    @property
    def empty(self) -> bool:
        """Return whether the interval does not hold any version."""
        if self.lower is None or self.upper is None:
            return False
        if self.lower.version == self.upper.version:
            return not (self.lower.included and self.upper.included)
        return self.lower.version > self.upper.version

    def __str__(self) -> str:
        """Return the interval as Maven spells a range."""
        if self.lower is not None and self.lower == self.upper:
            return f"[{self.lower.version}]"
        lower = "(" if self.lower is None else f"{'[' if self.lower.included else '('}{self.lower.version}"
        upper = ")" if self.upper is None else f"{self.upper.version}{']' if self.upper.included else ')'}"
        return f"{lower},{upper}"


def _tighter(boundary: _Boundary | None, other: _Boundary | None, *, after: bool) -> _Boundary | None:
    """Return the boundary that holds fewer versions: the later lower one, or the earlier upper one."""
    if boundary is None or other is None:
        return other if boundary is None else boundary
    if boundary.version != other.version:
        return boundary if (boundary.version > other.version) == after else other
    return other if boundary.included else boundary


def maven_range(bound: VersionBound, current_version: VersionString) -> str | None:
    """Return the Maven range of the versions the bound drops, or None where the rule set cannot express it.

    The rule set expresses a bound whose clauses each name a release, a level-based one once the current version
    anchors it. The range is empty where the bound does not drop any version.
    """
    if (anchored := bound.anchored(current_version)) is None:
        return None
    clauses = list(anchored.specifier or ())
    if not all(_names_a_release(clause) for clause in clauses):
        return None
    intervals = [_Interval()]
    for clause in clauses:
        intervals = [
            both for interval in intervals for piece in _pieces(clause) if not (both := interval & piece).empty
        ]
    if anchored.verb is Verb.ALLOW:
        intervals = _complement(intervals)
    return ",".join(str(interval) for interval in intervals)


def _complement(intervals: list[_Interval]) -> list[_Interval]:
    """Return the intervals of the versions outside the given intervals, which have to be sorted and disjoint."""
    gaps = []
    lower = None
    for interval in intervals:
        if interval.lower is not None:
            gaps.append(_Interval(lower, _flipped(interval.lower)))
        if interval.upper is None:
            return gaps
        lower = _flipped(interval.upper)
    return [*gaps, _Interval(lower)]


def _flipped(boundary: _Boundary) -> _Boundary:
    """Return the boundary at the same version with its inclusion reversed."""
    return _Boundary(boundary.version, included=not boundary.included)


def _names_a_release(clause: Specifier) -> bool:
    """Return whether the clause names a release: a version of numbers alone."""
    try:
        version = Version(clause.version.removesuffix(".*"))
    except InvalidVersion:
        return False
    return str(version) == ".".join(str(component) for component in version.release)


def _pieces(clause: Specifier) -> list[_Interval]:
    """Return the intervals of the versions the clause matches."""
    version = clause.version.removesuffix(".*")
    if clause.operator == "===":
        exactly = _Boundary(Version(version), included=True)
        return [_Interval(exactly, exactly)]
    if clause.operator == "~=":
        return [_between(version, _next_release(Version(version).release[:-1]))]
    wildcard = clause.version.endswith(".*")
    past = _next_release(Version(version).release) if wildcard else f"{version}{_PAST_QUALIFIERS}"
    return {
        "<": [_below(version)],
        "<=": [_below(past)],
        ">": [_from(past)],
        ">=": [_from(version)],
        "==": [_between(version, past)],
        "!=": [_below(version), _from(past)],
    }[clause.operator]


def _between(first: str, past: str) -> _Interval:
    """Return the versions from `first` up to `past`."""
    return _Interval(_from(first).lower, _below(past).upper)


def _below(past: str) -> _Interval:
    """Return the versions up to `past`."""
    return _Interval(upper=_Boundary(Version(past), included=False))


def _from(first: str) -> _Interval:
    """Return the versions from `first` on."""
    return _Interval(lower=_Boundary(Version(first), included=True))


def _next_release(release: tuple[int, ...]) -> str:
    """Return the release after the given one at its last component, such as `3.13` after `3.12`."""
    *head, last = release
    return ".".join(str(component) for component in (*head, last + 1))

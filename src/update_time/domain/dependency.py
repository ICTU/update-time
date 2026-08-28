"""A dependency's name and versions, and what a source resolves for one."""

from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum, auto
from functools import total_ordering
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


# The main version of a string that is not a version on its own, such as an image tag or a tag-like pin:
# the run starting at a digit and stopping before a `-`, so `python3.12-bookworm-slim` yields `3.12`. Possessive
# (`*+`) so the engine never backtracks into it, giving back characters to salvage a failing match.
MAIN_VERSION = r"\d[^-]*+"


def is_valid(version: VersionString) -> bool:
    """Return whether `packaging` parses the version as PEP 440.

    Anything that is not a version on its own reads as invalid: a branch such as `main`, a bare commit SHA, or an
    image tag such as `python3.12-bookworm-slim`.
    """
    try:
        Version(version)
    except InvalidVersion:
        return False
    return True


class FloatingPin(StrEnum):
    """What a source made of a reference with a floating pin and why, if the pin was not resolved."""

    RESOLVED = auto()
    NOT_LISTED = "its tag is not among the tags listed for the image"
    NO_MANIFEST = "the registry serves no manifest for its tag, so what that tag serves is unknown"
    NO_VERSION_TAG = "no tag naming a version serves the same image"
    NO_VERSION_TAG_EXAMINED = "no tag naming a version among the newest examined serves the same image"


@dataclass(frozen=True)
class Yank:
    """A version's withdrawal state: whether it was withdrawn — yanked on PyPI, deprecated on npm — and why."""

    yanked: bool = False
    reason: str = ""  # The maintainer's reason, empty when none was given

    def __str__(self) -> str:
        """Render the withdrawal as the maintainer's reason, quoted, or `reason not specified` when they gave none."""
        return f'"{self.reason}"' if self.reason else "reason not specified"


# A version lower than any real version, standing in for a version that reads as none, so any two versions compare
# and order uniformly and the one that reads as none sorts lowest.
LOWEST_VERSION = Version("0")


@total_ordering
@dataclass(frozen=True)
class Release:
    """A version a dependency's source published, and the date it was published on."""

    version: VersionString
    published: datetime

    def __lt__(self, other: Release) -> bool:
        """Order releases by publication date, and by version where two were published at the same moment.

        The tie-breaker keeps the order the same on every run, whatever order the source listed the releases in.
        """
        return (self.published, self._sortable_version) < (other.published, other._sortable_version)

    @property
    def _sortable_version(self) -> Version:
        """Return the version parsed, or the lowest there is when the source spells it as no version at all."""
        return Version(self.version) if is_valid(self.version) else LOWEST_VERSION

    @staticmethod
    def newest(releases: Iterable[Release]) -> Release | None:
        """Return the release published most recently, or None when the source published none."""
        return max(releases, default=None)


@dataclass(frozen=True)
class DependencyVersion:
    """A version of a dependency."""

    version: VersionString  # Arbitrary version string as returned by a source (PyPI, Docker Hub, GitHub releases, ...)
    changes: str = ""  # Changelog for this version, empty when none could be found
    sha: str = ""
    published: datetime | None = None  # Publication date of this (candidate) version, when known
    newest: Release | None = None  # The dependency's newest release, for staleness
    yank: Yank = Yank()  # The version's withdrawal state (yanked on PyPI, deprecated on npm)
    floating: FloatingPin | None = None  # What happened to the floating pin if the reference had one

    @classmethod
    def unpinned(cls, newest: Release) -> DependencyVersion:
        """Return the version for a reference that pins none, carrying the dependency's newest release."""
        return cls(version="", newest=newest)

    def __str__(self) -> str:
        """Render the version as its version string, followed by its publication date in UTC when that is known."""
        if self.published is None:
            return self.version
        return f"{self.version}, published: {self.published.astimezone(UTC):%Y-%m-%d %H:%M}"


def first_eligible[Candidate: SupportsRichComparison](
    candidates: Iterable[Candidate],
    resolve: Callable[[Candidate], DependencyVersion | None],
    current_version: VersionString,
) -> DependencyVersion:
    """Return the highest candidate that resolves to an eligible version, or the current version unchanged.

    The candidates are comparable, so they are walked newest-first. Eligibility can only be decided once the
    candidate's metadata has been fetched, so `resolve` does that and answers with the version, or None to skip
    the candidate and try the next.
    """
    for candidate in sorted(candidates, reverse=True):
        if (version := resolve(candidate)) is not None:
            return version
    return DependencyVersion(version=current_version)

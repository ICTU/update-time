"""A dependency's name and versions, and what a source resolves for one."""

import re
from dataclasses import dataclass
from enum import StrEnum, auto
from functools import total_ordering
from typing import TYPE_CHECKING, Self

from packaging.version import InvalidVersion, Version

from update_time.primitives.lookup import LookedUp
from update_time.primitives.timestamp import utc_date_and_time

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime
    from typing import Protocol

    from update_time.primitives.lookup import Lookup

    class ProjectGetter(Protocol):
        """The contract the project checks bind and a source implements, answering what it reports about a project."""

        def __call__(self, dependency: DependencyName, /) -> Project:
            """Return what the source reports about the dependency's project."""
            ...


# The name of a dependency as it appears in a manifest: a Docker image (`cimg/python`), a GitHub action
# (`actions/checkout`), a PyPI or npm package, etc. A version string is an arbitrary version or tag (`3.14.2`, `v1`).
type DependencyName = str
type VersionString = str


@dataclass(frozen=True)
class PinnedDependency:
    """A dependency and the version a file pins it to, which is what a source is asked about."""

    name: DependencyName
    version: VersionString


# The characters a Python distribution's name treats as one and the same separator (see `normalized_python_name`).
_NAME_SEPARATORS = re.compile(r"[-_.]+")


def normalized_python_name(name: DependencyName) -> DependencyName:
    """Return the name as a Python package index spells it, so a name is matched however a manifest spells it.

    An index names a distribution in lower case with each run of `-`, `_`, and `.` collapsed to a single `-`, as
    https://peps.python.org/pep-0503/#normalized-names prescribes, and uv reports a package by that name. So a
    `typing_extensions` pin and the `typing-extensions` uv reports for it are the same dependency.
    """
    return _NAME_SEPARATORS.sub("-", name).lower()


# The main version of a string that is not a version on its own, such as an image tag or a tag-like pin:
# the run starting at a digit and stopping before a `-`, so `python3.12-bookworm-slim` yields `3.12`. Possessive
# (`*+`) so the engine never backtracks into it, giving back characters to salvage a failing match.
MAIN_VERSION = r"\d[^-]*+"

# A run of version numbers: digits and the dots between them, such as `3.14`, or the `4.1.100` of `4.1.100.Final`.
VERSION_NUMBERS = r"\d+(?:\.\d+)*"

# A label after a snapshot's date, such as the `slim` of `trixie-20260803-slim`. A dash separates it from the date.
_LABEL_AFTER_A_DATE = r"(?:-|$)(?P<suffix>.*)$"

# A snapshot named by the date it was built, such as `bookworm-20260803`, after the label of its line.
DATED_SNAPSHOT = r"(?P<prefix>.+-)(?P<version>\d{8})" + _LABEL_AFTER_A_DATE

# A snapshot named by the date it was built, such as `2023-10-20`, or by the date and time, such as
# `2026-02-12-23-06-03`, after an optional label, such as the `x86_64-` of `x86_64-2023-10-20`. Its version is the
# date with the dashes dropped.
DASHED_SNAPSHOT = r"(?P<prefix>(?:.+-)?)(?P<version>\d{4}-\d{2}-\d{2}(?:-\d{2}-\d{2}-\d{2})?)" + _LABEL_AFTER_A_DATE


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


class Unserved(StrEnum):
    """What the source does not serve of the dependency a reference names."""

    VERSION = auto()  # The registry serves the repository, but not the tag the reference names
    REPOSITORY = auto()  # The registry does not serve the image's repository
    RELEASES = auto()  # The repository does not tag a release to update to


class FloatingPin(StrEnum):
    """What a source made of a reference with a floating pin and why, if the pin was not resolved."""

    RESOLVED = auto()
    NOT_LISTED = "%(tag)s is not among the tags listed for the image"
    NOT_AMONG_EXAMINED = "%(tag)s is not among the newest tags examined for the image"
    NO_MANIFEST = "the registry does not serve a manifest for %(tag)s, so what that tag serves is unknown"
    NO_VERSION_TAG = "no tag naming a version serves the same image"
    NO_VERSION_TAG_EXAMINED = "no tag naming a version among the newest examined serves the same image"

    def explained(self, tag: str) -> str:
        """Return the reason with the tag the source looked up filled in."""
        return self % {"tag": tag}


def tag_of(version: VersionString, separator: str = ":") -> str:
    """Return the version with the separator attaching it to a dependency's name, or nothing when it names none."""
    return f"{separator}{version}" if version else ""


class ImageNotLookedUp(StrEnum):
    """Why Update-time does not ask a registry about the image a reference names."""

    BUILT_IMAGE = "the Compose file builds this image"
    MACHINE_EXECUTOR_IMAGE = "it names a CircleCI machine executor rather than an image a registry serves"
    EMPTY_BASE_IMAGE = "it names the empty base image Docker builds from, which no registry serves"
    BUILD_STAGE = "it names one of the Dockerfile's own build stages"
    THROUGH_A_VARIABLE = "it names its image or tag through a variable or a template"
    VERSION_ACROSS_LINES = "it writes its version across more than one line of the Dockerfile"
    DIGEST_OVERRULES_BUILD_ARGUMENTS = "its digest overrules the build arguments it names"
    ARGUMENT_SHARED_WITH_ANOTHER_FROM_LINE = (
        "it shares its build argument with a FROM line that names another image or tag, or carries another marker"
    )


@dataclass(frozen=True)
class Yank:
    """A version's withdrawal state: whether it was withdrawn — yanked on PyPI, deprecated on npm — and why."""

    yanked: bool = False
    reason: str = ""  # The maintainer's reason, empty when none was given

    def __str__(self) -> str:
        """Render the withdrawal as the maintainer's reason, quoted, or `reason not specified` when they gave none."""
        return f'"{self.reason}"' if self.reason else "reason not specified"


class ArchivedSubject(StrEnum):
    """What a source archives: PyPI archives a project, GitHub a repository."""

    PROJECT = auto()
    REPOSITORY = auto()


@dataclass(frozen=True)
class Archival:
    """A dependency's archival state, as its source declares it."""

    archived: bool = False
    reason: str = ""  # The reason the source published, empty when it published none
    subject: ArchivedSubject = ArchivedSubject.PROJECT


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
class Project:
    """What a source reports about the project behind a dependency, rather than about one of its versions."""

    newest: Release | None = None
    archival: Archival | None = None  # None where the source does not say whether it archived the project


class Changes(str):
    """A version's changes as a changelog records them, with the markup they are in and the page they come from."""

    __slots__ = ("markdown", "url")

    markdown: bool
    url: str

    def __new__(cls, changes: str, *, markdown: bool, url: str) -> Self:
        """Create the changes."""
        instance = super().__new__(cls, changes)
        instance.markdown = markdown
        instance.url = url
        return instance


# The changes of a version no changelog records, which no markup is written in.
NO_CHANGES = Changes("", markdown=False, url="")

# What a version carries when its source does not date it.
UNDATED: LookedUp[datetime] = LookedUp(None)


@dataclass(frozen=True)
class DependencyVersion:
    """A version of a dependency."""

    # The version the source offers (PyPI, Docker Hub, GitHub releases, ...), or else the tag or branch the reference
    # names, such as `latest` or `main`, and nothing for a commit that only its SHA names
    version: VersionString
    changes: Changes = NO_CHANGES  # What the changelog records for this version
    sha: str = ""
    publication: Lookup[datetime] = UNDATED  # When the source says the version was published, or why it can't say
    yank: Yank | None = None  # The version's withdrawal state (yanked on PyPI, deprecated on npm), None if unknown
    project: Project = Project()  # What the source reports about the project behind the dependency
    floating: FloatingPin | None = None  # What happened to the floating pin if the reference had one
    unserved: Unserved | None = None  # What the source does not serve, if anything
    image_not_looked_up: ImageNotLookedUp | None = None  # Why Update-time does not ask a registry about the image
    tag_name: str = ""  # The version's tag as the repository spells it (`v6.0.0`), where the source reads tags
    fresh_release: Release | None = None  # The newest release inside the cooldown, if any

    @classmethod
    def unpinned(cls, project: Project) -> DependencyVersion:
        """Return the version for a reference that pins no version, carrying its source's report on the project."""
        return cls(version="", project=project)

    def __str__(self) -> str:
        """Render the version as its version string, followed by its publication date in UTC when that is known."""
        if (published := self.publication.value) is None:
            return self.version
        return f"{self.version}, published: {utc_date_and_time(published)}"

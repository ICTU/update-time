"""The version schemes that order the versions of an ecosystem, such as PEP 440 for PyPI and semver for npm."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from packaging.version import Version

from .dependency import VersionString, is_valid

if TYPE_CHECKING:
    from collections.abc import Callable

# A semver version.
_SEMVER: Final[re.Pattern[str]] = re.compile(
    r"""
    (?P<release>\d+(?:\.\d+)*)  # the release numbers
    (?:-(?P<prerelease>[0-9A-Za-z.-]+))?  # the pre-release identifiers, if any
    (?:\+[0-9A-Za-z.-]+)?  # the build metadata, if any
    """,
    re.VERBOSE,
)

# Semver orders a version by this key. It holds the release numbers, and then what tells a release from a pre-release.
type _SemverKey = tuple[tuple[int, ...], tuple[int] | tuple[int, tuple[tuple[int, int, str], ...]]]


@dataclass(frozen=True, slots=True, kw_only=True)
class _VersionScheme[Key: (Version, _SemverKey)]:
    """A scheme that orders versions, by the key it gives each version it reads."""

    order: Callable[[VersionString], Key | None]

    def is_downgrade(self, version: VersionString, new_version: VersionString) -> bool:
        """Return whether the scheme orders the new version below the version, where it reads both."""
        old, new = self.order(version), self.order(new_version)
        return old is not None and new is not None and bool(new < old)


def _pep_440_order(version: VersionString) -> Version | None:
    """Return the version as PEP 440 orders it, or None where it is not a PEP 440 version."""
    return Version(version) if is_valid(version) else None


def _semver_order(version: VersionString) -> _SemverKey | None:
    """Return the version as semver orders it, or None where it is not a semver version.

    A release orders after its pre-releases, and a numeric identifier before an alphanumeric one.
    """
    if (match := _SEMVER.fullmatch(version)) is None:
        return None
    release = tuple(int(number) for number in match["release"].split("."))
    if match["prerelease"] is None:
        return release, (1,)
    identifiers = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part) for part in match["prerelease"].split(".")
    )
    return release, (0, identifiers)


PEP_440: Final[_VersionScheme[Version]] = _VersionScheme(order=_pep_440_order)
SEMVER: Final[_VersionScheme[_SemverKey]] = _VersionScheme(order=_semver_order)

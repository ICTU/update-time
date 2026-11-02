"""The version schemes that order the versions of an ecosystem, such as PEP 440 for PyPI and semver for npm."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from packaging.version import Version

from update_time.domain.dependency import VersionString, is_valid

if TYPE_CHECKING:
    from collections.abc import Callable

    from _typeshed import SupportsDunderLT

# A semver version: its release numbers, its pre-release identifiers if any, and its build metadata if any.
_SEMVER = re.compile(r"(?P<release>\d+(?:\.\d+)*)(?:-(?P<prerelease>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")


@dataclass(frozen=True)
class _VersionScheme[Key: SupportsDunderLT[Any]]:
    """A scheme that orders versions, by the key it gives each version it reads."""

    order: Callable[[VersionString], Key | None]

    def is_downgrade(self, version: VersionString, new_version: VersionString) -> bool:
        """Return whether the scheme orders the new version below the version, where it reads both."""
        old, new = self.order(version), self.order(new_version)
        return old is not None and new is not None and bool(new < old)


def _pep_440_order(version: VersionString) -> Version | None:
    """Return the version as PEP 440 orders it, or None where it is not a PEP 440 version."""
    return Version(version) if is_valid(version) else None


def _semver_order(version: VersionString) -> tuple[object, ...] | None:
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


PEP_440 = _VersionScheme(_pep_440_order)
SEMVER = _VersionScheme(_semver_order)

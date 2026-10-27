"""Which sources can observe a yanked version, and what they report for one."""

from dataclasses import replace
from typing import TYPE_CHECKING

from update_time.domain.dependency import Yank
from update_time.primitives.capability import capability

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.dependency import DependencyVersion, VersionString

yank_reporting, reports_yanks = capability("reports_yanks")


def with_yank_state(
    latest: DependencyVersion, current_version: VersionString, yank_state: Callable[[VersionString], Yank | None]
) -> DependencyVersion:
    """Return the version carrying the withdrawal state of the version the run left the reference on.

    A source skips a withdrawn release when it picks a new version, so a version the run moved to was not withdrawn.
    A lookup costs a request, so only a version the reference stayed on is looked up.
    """
    if latest.version != current_version:
        return replace(latest, yank=Yank())
    return replace(latest, yank=yank_state(current_version))

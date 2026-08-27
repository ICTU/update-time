"""The version a runtime reference takes from the base image it follows.

A `.python-version` entry and a Node engine both follow the base image in the project's Dockerfile, so the runtime
a project develops against and the runtime it ships stay in step. The rule they share lives here: adopt the image's
version, unless the reference's own bound excludes it.
"""

from typing import TYPE_CHECKING

from packaging.version import Version

from update_time.domain.dependency import DependencyVersion, is_valid
from update_time.domain.downgrade import downgrading

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter, VersionBound
    from update_time.domain.dependency import DependencyName, VersionString


def image_version_getter(image_version: VersionString, *, allow_downgrade: bool) -> NewVersionGetter:
    """Return a getter offering the base image's version, unless the reference's own bound excludes it.

    The cooldown and the staleness check have already been applied to the image itself, so the version carries no
    dates and is neither held back nor flagged here.
    """

    def get_new_version(
        _dependency: DependencyName, current_version: VersionString, version_bound: VersionBound, _cooldown_days: int
    ) -> DependencyVersion:
        candidate = Version(image_version)
        adoptable = allow_downgrade or (is_valid(current_version) and candidate > Version(current_version))
        if adoptable and version_bound.keeps(candidate, current_version):
            return DependencyVersion(version=image_version)
        return DependencyVersion(version=current_version)

    return downgrading(get_new_version) if allow_downgrade else get_new_version

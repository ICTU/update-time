"""The version a runtime reference takes from the base image it follows.

A `.python-version` entry and a Node engine both follow the base image in the project's Dockerfile, so the runtime
a project develops against and the runtime it ships stay in step. They differ in which way they may move, so each
takes a getter of its own. Both offer the image's version only where the reference's own bound admits it, and both
need a version that parses, which the caller deriving it guarantees. The cooldown and the staleness check have
already been applied to the image, so the version carries no dates and is neither held back nor flagged here.
"""

from typing import TYPE_CHECKING

from packaging.version import Version

from update_time.domain.dependency import DependencyVersion, is_valid
from update_time.domain.downgrade import downgrading

if TYPE_CHECKING:
    from update_time.domain.bound import NewVersionGetter, VersionBound
    from update_time.domain.dependency import DependencyName, VersionString


def advancing_image_version_getter(image_version: VersionString) -> NewVersionGetter:
    """Return a getter offering the image's version, never below the one the reference already pins.

    A reference ahead of the base image is there deliberately, so it is left where it is.
    """

    def get_new_version(
        _dependency: DependencyName, current_version: VersionString, version_bound: VersionBound, _cooldown_days: int
    ) -> DependencyVersion:
        if is_valid(current_version) and Version(image_version) > Version(current_version):
            return _admitted_version(image_version, current_version, version_bound)
        return DependencyVersion(version=current_version)

    return get_new_version


def following_image_version_getter(image_version: VersionString) -> NewVersionGetter:
    """Return a getter offering the image's version, above or below the one the reference already pins.

    A Node engine declares the runtime the project ships, so it follows the base image down as well as up.
    """

    def get_new_version(
        _dependency: DependencyName, current_version: VersionString, version_bound: VersionBound, _cooldown_days: int
    ) -> DependencyVersion:
        return _admitted_version(image_version, current_version, version_bound)

    return downgrading(get_new_version)


def _admitted_version(
    image_version: VersionString, current_version: VersionString, version_bound: VersionBound
) -> DependencyVersion:
    """Return the image's version where the reference's bound admits it, and the current version otherwise."""
    if version_bound.keeps(Version(image_version), current_version):
        return DependencyVersion(version=image_version)
    return DependencyVersion(version=current_version)

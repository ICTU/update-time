"""What a source of releases reports about a dependency, gathered in one record."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from update_time.domain.dependency import NO_CHANGES, DependencyVersion, ImageNotLookedUp
from update_time.primitives.lookup import LookedUp

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.dependency import (
        ChangesGetter,
        DependencyName,
        PinnedDependency,
        ProjectGetter,
        PublicationDateGetter,
        VersionString,
        YankGetter,
    )
    from update_time.domain.vulnerability import Ecosystem


def every_dependency(_pinned: PinnedDependency) -> bool:
    """Return whether the source dates the versions of the dependency, which it does whichever dependency it is."""
    return True


def _no_dependency(_pinned: PinnedDependency) -> bool:
    """Return whether the source dates the versions of the dependency, which it does for none."""
    return False


def _looked_up(_pinned: PinnedDependency) -> ImageNotLookedUp | None:
    """Return why the source does not look the dependency's image up, which it does for every dependency."""
    return None


@dataclass(frozen=True)
class Source:
    """A source's getters of what it reports about a dependency, and the ecosystem of its advisories.

    Each getter is None where the source does not offer it. `dates_versions_of` tells the dependencies whose versions
    the source dates from the rest, and `why_image_not_looked_up` says why the source does not look a dependency's image
    up. `adopts_hash_drift` is False for a source whose drifted hashes Update-time never adopts.
    """

    project: ProjectGetter | None = None
    yank: YankGetter | None = None
    changes: ChangesGetter | None = None
    publication_date: PublicationDateGetter | None = None
    ecosystem: Ecosystem | None = None
    new_version: NewVersionGetter | None = None
    dates_versions_of: Callable[[PinnedDependency], bool] = _no_dependency
    why_image_not_looked_up: Callable[[PinnedDependency], ImageNotLookedUp | None] = _looked_up
    adopts_hash_drift: bool = True

    def reports_yanks(self, _pinned: PinnedDependency) -> bool:
        """Return whether the source reports the dependency's yanks, which it does where it has a yank getter."""
        return self.yank is not None

    def reports_archival(self, _pinned: PinnedDependency) -> bool:
        """Return whether the source declares the dependency's archival, which it does where its project getter does."""
        return self.project is not None and self.project.declares_archival

    def reports_vulnerabilities(self, _pinned: PinnedDependency) -> bool:
        """Return whether OSV holds advisories for the dependency, which it does where the source has an ecosystem."""
        return self.ecosystem is not None

    def reports_publication_dates(self, pinned: PinnedDependency) -> bool:
        """Return whether the source dates the dependency's versions, so a cooldown and a staleness check can apply."""
        return self.dates_versions_of(pinned)

    def why_never_updated(self, pinned: PinnedDependency) -> ImageNotLookedUp | None:
        """Return why the source never updates the dependency's image whatever its marker says, or None where it may.

        A build argument shared with a `FROM` line that carries another marker is left alone because of the markers.
        """
        why = self.why_image_not_looked_up(pinned)
        return None if why is ImageNotLookedUp.ARGUMENT_SHARED_WITH_ANOTHER_FROM_LINE else why

    def release(self, dependency: DependencyName, version: VersionString) -> DependencyVersion:
        """Return the version, with the changes and the publication date the source reports for it."""
        changes = NO_CHANGES if self.changes is None else self.changes(dependency, version)
        published = None if self.publication_date is None else self.publication_date(dependency, version)
        return DependencyVersion(version, changes, publication=LookedUp(published))

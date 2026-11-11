"""Why a getter does not look a reference's image up, recorded on the getter itself so a warning can name it."""

from typing import TYPE_CHECKING

from update_time.domain.dependency import ImageNotLookedUp

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.dependency import PinnedDependency

    type _WhyNotLookedUp = Callable[[PinnedDependency], ImageNotLookedUp | None]

_ATTRIBUTE = "why_image_not_looked_up"


def explaining_skipped_lookups[Getter](getter: Getter, why: _WhyNotLookedUp) -> Getter:
    """Record on the getter why it does not look a reference's image up, and return the getter."""
    setattr(getter, _ATTRIBUTE, why)
    return getter


def why_image_not_looked_up(getter: object, pinned: PinnedDependency) -> ImageNotLookedUp | None:
    """Return why the getter does not look the reference's image up, or None when it looks it up or does not say why."""
    why: _WhyNotLookedUp | None = getattr(getter, _ATTRIBUTE, None)
    return None if why is None else why(pinned)


def why_never_updated(getter: object, pinned: PinnedDependency) -> ImageNotLookedUp | None:
    """Return why the getter never updates the reference's image whatever its marker says, or None where it may.

    A build argument shared with a `FROM` line that carries another marker is left alone because of the markers.
    """
    why = why_image_not_looked_up(getter, pinned)
    return None if why is ImageNotLookedUp.ARGUMENT_SHARED_WITH_ANOTHER_FROM_LINE else why

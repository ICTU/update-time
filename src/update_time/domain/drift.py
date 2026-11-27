"""Whether a reference adopts what its drifted hash pin now points at, or only warns about the drift."""

from typing import TYPE_CHECKING

from .marker import Scope

if TYPE_CHECKING:
    from collections.abc import Callable

    from .marker import Marker


def report_drift(
    marker: Marker, warn: Callable[[], None], adopt: Callable[[str], None], past_cooldown: Callable[[], bool]
) -> bool:
    """Report a drifted hash pin and return whether the reference adopts what it now points at."""
    if not (directive := marker.allow_directive(Scope.HASH_DRIFT)):
        warn()
        return False
    if not past_cooldown():
        return False
    adopt(directive)
    return True

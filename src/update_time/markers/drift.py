"""Whether a reference adopts what its drifted hash pin now points at, or only warns about the drift."""

from typing import TYPE_CHECKING

from update_time.markers.marker import Scope
from update_time.markers.opt_in import RunWideOptIn
from update_time.primitives.environment import flag

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.markers.marker import Marker

# Private channel that passes --allow-hash-drift from the CLI to the updater subprocesses: whether a drifted pin —
# a re-pushed image digest, or the commit a moved version tag or branch now points at — should be adopted repo-wide.
ALLOW_HASH_DRIFT = flag("_UPDATE_TIME_ALLOW_HASH_DRIFT")

_HASH_DRIFT = RunWideOptIn(ALLOW_HASH_DRIFT, "--allow-hash-drift", Scope.HASH_DRIFT)


def report_drift(
    marker: Marker, warn: Callable[[], None], adopt: Callable[[str], None], past_cooldown: Callable[[], bool]
) -> bool:
    """Report a drifted hash pin and return whether the reference adopts what it now points at.

    The new value is adopted only when the reference opted in (see `RunWideOptIn`) and the new value is past the
    cooldown; otherwise it is warned about and left as it is. `adopt` is handed the opt-in, to name in its message.
    Callback-driven so `markers` stays free of I/O.
    """
    if (cause := _HASH_DRIFT.cause(marker)) is None or not past_cooldown():
        warn()
        return False
    adopt(cause)
    return True

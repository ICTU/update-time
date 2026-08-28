"""Whether a reference adopts what its drifted hash pin now points at, or only warns about the drift."""

from typing import TYPE_CHECKING

from update_time.markers.marker import Scope
from update_time.markers.opt_in import RunWideOptIn
from update_time.primitives.environment import flag

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.markers.marker import Marker

# Private channel that passes --allow-hash-drift from the CLI to the updater subprocesses: whether a drifted pin —
# a re-pushed image digest, or the commit a moved version tag now points at — should be adopted repo-wide.
ALLOW_HASH_DRIFT = flag("_UPDATE_TIME_ALLOW_HASH_DRIFT")

_HASH_DRIFT = RunWideOptIn(ALLOW_HASH_DRIFT, "--allow-hash-drift", Scope.HASH_DRIFT)


def report_drift(marker: Marker, warn: Callable[[], None], adopt: Callable[[str], None]) -> bool:
    """Report a drifted hash pin and return whether the reference adopts what it now points at.

    The rule every kind of hash pin follows: the new value is adopted only when the reference opted in (see
    `RunWideOptIn`), and is otherwise warned about and left as it is, so a changed target is never silently taken on.
    `warn` and `adopt` report the drift in the terms of the kind that drifted — a re-pushed image digest, or the
    commit a moved version tag now points at — and `adopt` is handed the opt-in that caused it, to name in its
    message. Callback-driven so `markers` stays free of I/O.
    """
    if (cause := _HASH_DRIFT.cause(marker)) is None:
        warn()
        return False
    adopt(cause)
    return True

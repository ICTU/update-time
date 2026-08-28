"""Whether a reference keeps its floating pin, and what opted it out of being pinned."""

from typing import TYPE_CHECKING

from update_time.markers.marker import Scope
from update_time.markers.opt_in import RunWideOptIn
from update_time.primitives.environment import flag

if TYPE_CHECKING:
    from update_time.markers.marker import Marker

# Private channel that passes --allow-floating-pin from the CLI to the updater subprocesses: whether every
# reference in the scan keeps its floating pin, rather than being pinned to the version that pin serves.
ALLOW_FLOATING_PIN = flag("_UPDATE_TIME_ALLOW_FLOATING_PIN")

_FLOATING_PIN = RunWideOptIn(ALLOW_FLOATING_PIN, "--allow-floating-pin", Scope.FLOATING_PIN)


def floating_pin_cause(marker: Marker) -> str | None:
    """Return what keeps the reference's pin floating (a marker or the run-wide flag), or None when nothing does."""
    return _FLOATING_PIN.cause(marker)

"""Whether a drifted hash pin is adopted, once its new value is older than the cooldown, or only warned about."""

from functools import partial
from typing import TYPE_CHECKING

from update_time.domain.cooldown import past_cooldown
from update_time.markers.cooldown import cooldown_days
from update_time.markers.drift import report_drift

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from update_time.domain.reference import DriftedPin
    from update_time.io.log import Drift, Logger
    from update_time.markers.marker import Marker
    from update_time.primitives.lookup import Lookup


def adopts_drift(  # noqa: PLR0913 — what dates the new value, and what reports it undated, differ per kind of hash pin
    kind: Drift,
    drifted: DriftedPin,
    marker: Marker,
    log: Logger,
    *,
    publication: Lookup[datetime],
    report_undated: Callable[[Drift, DriftedPin, str], None],
) -> bool:
    """Report the drift, and return whether the reference adopts the new value once older than the cooldown."""
    warn = partial(log.drift, kind, drifted)
    adopt = partial(log.adopted_drift, kind, drifted)
    past = partial(past_cooldown, cooldown_days(marker), publication, partial(report_undated, kind, drifted))
    return report_drift(marker, warn, adopt, past)

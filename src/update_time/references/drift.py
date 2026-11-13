"""Whether a drifted hash pin is adopted, once its new value is older than the cooldown, or only warned about."""

from functools import partial
from typing import TYPE_CHECKING

from update_time.domain.cooldown import past_cooldown
from update_time.domain.drift import report_drift
from update_time.domain.reference import DriftedPin

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from update_time.domain.reference import Reference
    from update_time.io.log import Drift, Logger
    from update_time.primitives.lookup import Lookup


def adopts_drift(  # noqa: PLR0913 — what dates the new value and what reports it undated differ per kind of hash pin
    kind: Drift,
    reference: Reference,
    new_sha: str,
    log: Logger,
    *,
    publication: Lookup[datetime],
    report_undated: Callable[[Drift, DriftedPin, str], None],
) -> bool:
    """Report the drift to the new hash, and return whether the reference adopts it once older than the cooldown."""
    drifted = DriftedPin.from_reference(reference, new_sha=new_sha)
    warn = partial(log.drift, kind, drifted)
    adopt = partial(log.adopted_drift, kind, drifted)
    past = partial(past_cooldown, reference.cooldown_days, publication, partial(report_undated, kind, drifted))
    return report_drift(reference.marker, warn, adopt, past)

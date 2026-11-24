"""Whether a drifted hash pin is adopted, once its new value is older than the cooldown, or only warned about."""

from functools import partial
from typing import TYPE_CHECKING

from update_time.domain.cooldown import past_cooldown
from update_time.domain.drift import report_drift
from update_time.domain.reference import DriftedPin

if TYPE_CHECKING:
    from datetime import datetime

    from update_time.domain.reference import DriftKind, Reference
    from update_time.io.log import Logger
    from update_time.primitives.lookup import Lookup


def adopts_drift(
    kind: DriftKind,
    reference: Reference,
    new_sha: str,
    log: Logger,
    *,
    publication: Lookup[datetime],
) -> bool:
    """Report the drift to the new hash, and return whether the reference adopts it once older than the cooldown."""
    drifted = DriftedPin.from_reference(reference, kind=kind, new_sha=new_sha)
    warn = partial(log.drift, drifted)
    adopt = partial(log.adopted_drift, drifted)
    past = partial(past_cooldown, reference.cooldown_days, publication, partial(log.undated_drift, drifted))
    return report_drift(reference.marker, warn, adopt, past)

"""Dependency update cooldown helpers."""

from datetime import UTC, datetime, timedelta

from update_time.domain.capability import capability
from update_time.primitives.environment import EnvVar
from update_time.primitives.timestamp import days_since

# Private channel that passes --cooldown from the CLI to the updater subprocesses.
COOLDOWN = EnvVar("_UPDATE_TIME_COOLDOWN_DAYS", default=7, parse=int)

# A source that dates its versions, so a cooldown can be measured against them, registers its getter with
# `cooldown_honouring`, naming the dependencies it dates where it dates only some. `honours_cooldown` reads that
# back for one dependency, so a `cooldown` item on a reference whose versions carry no date is reported as redundant.
cooldown_honouring, honours_cooldown = capability("honours_cooldown")


def within_cooldown(timestamp: datetime | None, cooldown_days: int) -> bool:
    """Return whether the timestamp falls within a cooldown period of the given number of days.

    Whole days are compared, so a cooldown of more days than a `timedelta` can hold is honoured rather than
    overflowing: the count comes from a marker in a file as well as from the command line.
    """
    if timestamp is None:
        return False
    return days_since(timestamp) < cooldown_days


def cooldown_cutoff() -> str:
    """Return the cooldown as an RFC 3339 cutoff timestamp: releases published after it are still too fresh to adopt.

    A cooldown reaching further back than a date can express is clamped to the earliest instant there is. Such a
    cooldown excludes every release anyway, so clamping keeps the meaning.
    """
    now = datetime.now(UTC)
    earliest = datetime.min.replace(tzinfo=UTC)
    if (cooldown_days := COOLDOWN.get()) > (now - earliest).days:
        return earliest.isoformat()
    return (now - timedelta(days=cooldown_days)).isoformat()

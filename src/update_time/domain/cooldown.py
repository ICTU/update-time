"""Dependency update cooldown helpers."""

import os
from datetime import UTC, datetime, timedelta

COOLDOWN_DAYS = 7  # Default cooldown period in days, used when --cooldown is not given
# Private channel that passes --cooldown from the CLI to the updater subprocesses; not a user-facing setting (use
# --cooldown instead). The leading underscore marks it internal.
COOLDOWN_DAYS_ENV_VAR = "_UPDATE_TIME_COOLDOWN_DAYS"


def cooldown_days() -> int:
    """Return the configured cooldown period in days."""
    return int(os.environ.get(COOLDOWN_DAYS_ENV_VAR, COOLDOWN_DAYS))


def within_cooldown(timestamp: datetime | None) -> bool:
    """Return whether the timestamp falls within the cooldown period."""
    if timestamp is None:
        return False
    return datetime.now(UTC) - timestamp < timedelta(days=cooldown_days())


def cooldown_cutoff() -> str:
    """Return the cooldown as an RFC 3339 cutoff timestamp: releases published after it are still too fresh to adopt.

    This is the cooldown expressed the way uv's `--exclude-newer` wants it — an absolute instant rather than a
    duration — for callers that pass the cooldown to uv on the command line (the inline-script-metadata updater,
    which has no lockfile to write `exclude-newer` into, unlike the pyproject.toml updater).
    """
    return (datetime.now(UTC) - timedelta(days=cooldown_days())).isoformat()

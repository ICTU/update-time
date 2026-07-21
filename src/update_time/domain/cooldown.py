"""Dependency update cooldown helpers."""

from datetime import UTC, datetime, timedelta

from update_time.primitives.environment import EnvVar

# Private channel that passes --cooldown from the CLI to the updater subprocesses.
COOLDOWN = EnvVar("_UPDATE_TIME_COOLDOWN_DAYS", default=7, parse=int)


def within_cooldown(timestamp: datetime | None) -> bool:
    """Return whether the timestamp falls within the cooldown period."""
    if timestamp is None:
        return False
    return datetime.now(UTC) - timestamp < timedelta(days=COOLDOWN.get())


def cooldown_cutoff() -> str:
    """Return the cooldown as an RFC 3339 cutoff timestamp: releases published after it are still too fresh to adopt.

    This is the cooldown expressed the way uv's `--exclude-newer` wants it — an absolute instant rather than a
    duration — for callers that pass the cooldown to uv on the command line (the inline-script-metadata updater,
    which has no lockfile to write `exclude-newer` into, unlike the pyproject.toml updater).
    """
    return (datetime.now(UTC) - timedelta(days=COOLDOWN.get())).isoformat()

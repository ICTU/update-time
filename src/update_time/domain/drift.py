"""Whether a hash pin has drifted, whether the reference adopts it or only warns, and how that is reported."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from update_time.domain.bound import Verb
from update_time.domain.reference import Reference
from update_time.primitives.environment import EnvVar

if TYPE_CHECKING:
    from collections.abc import Callable

    from update_time.domain.marker import Marker

# Private channel that passes --allow-hash-drift from the CLI to the updater subprocesses: whether a drifted pin —
# a re-pushed image digest, or the commit a moved version tag now points at — should be adopted repo-wide.
ALLOW_HASH_DRIFT = EnvVar(
    "_UPDATE_TIME_ALLOW_HASH_DRIFT",
    default=False,
    parse=lambda value: value == "1",
    serialize=lambda allow: "1" if allow else "0",
)


def hash_drifted(resolved: str, pinned: str) -> bool:
    """Return whether a hash (resolved digest, commit SHA, or integrity hash) differs from the one already pinned."""
    return bool(resolved) and resolved != pinned


@dataclass(frozen=True, kw_only=True)
class DriftedPin(Reference):
    """A hash pin that no longer matches what it points at, and the hash it now resolves to."""

    new_sha: str


def report_drift(marker: Marker, warn: Callable[[], None], adopt: Callable[[str], None]) -> bool:
    """Report a drifted hash pin and return whether the reference adopts what it now points at.

    The rule every kind of hash pin follows: the new value is adopted only when the reference opted in (see
    `_drift_cause`), and is otherwise warned about and left as it is, so a changed target is never silently taken on.
    `warn` and `adopt` report the drift in the terms of the kind that drifted — a re-pushed image digest, or the
    commit a moved version tag now points at — and `adopt` is handed the opt-in that caused it, to name in its
    message. Callback-driven so `domain` stays free of I/O.
    """
    if (cause := _drift_cause(marker)) is None:
        warn()
        return False
    adopt(cause)
    return True


def _drift_cause(marker: Marker) -> str | None:
    """Return what opts a reference into adopting a drifted pin (a marker or a CLI-flag), or None when nothing does."""
    if marker.allow_drift:
        return f"update-time: {marker.raw_directives(Verb.ALLOW)}"
    return "--allow-hash-drift" if ALLOW_HASH_DRIFT.get() else None

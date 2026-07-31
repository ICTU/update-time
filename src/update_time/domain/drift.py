"""Whether a hash pin has drifted, and whether the reference adopts it or only warns.

A hash pin can stop matching what it points at while the version stays put: an image tag re-pushed under the same
name serves a new digest, a mutable version tag can be moved onto another commit, and a declared integrity hash can
disagree with the one the CDN serves. Adopting a change silently would defeat the pin, so it is adopted only when
the reference opted in — per reference with an `# update-time: allow[hash-drift]` marker, or repo-wide with
`--allow-hash-drift`. Adoption covers a digest or a commit only: refusing content that doesn't match is the whole
point of a hash, so a mismatched one is always left to be corrected. Both rules live here so every kind of hash pin
answers them the same way; what a kind does with the answer is left to the module that rewrites it. Like the
cooldown and staleness helpers, the setting travels from the command line in an environment variable.
"""

from typing import TYPE_CHECKING

from update_time.domain.bound import Verb
from update_time.primitives.environment import EnvVar

if TYPE_CHECKING:
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
    """Return whether a resolved digest, commit SHA, or integrity hash differs from the one already pinned.

    The one comparison behind all three drifts, so an empty resolved value means the same everywhere: nothing was
    resolved, so there is nothing to compare and nothing has drifted.
    """
    return bool(resolved) and resolved != pinned


def drift_cause(marker: Marker) -> str | None:
    """Return what opts a reference into adopting a drifted pin, or None when nothing does.

    The per-reference marker is the more specific opt-in, so its `allow` directives are named verbatim as the cause
    when both it and the repo-wide flag apply; the hash-drift opt-in is among them. The cause is named for the log
    message reporting the adoption.
    """
    if marker.allow_drift:
        return f"update-time: {marker.raw_marker(Verb.ALLOW)}"
    return "--allow-hash-drift" if ALLOW_HASH_DRIFT.get() else None

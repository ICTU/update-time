"""Render the sample warnings the README quotes, by logging them through Update-time's own `Logger`.

The README shows what a drifted hash pin, a stale dependency, a yanked version, and a redundant marker are reported
as. Logging those samples here rather than transcribing them into the template is what keeps them true: a reworded
message rewrites the README on the next `just readme`, instead of leaving it quoting output the tool no longer
produces. The lines are rendered as the plain `LEVEL message` the README's `console` blocks show, so unlike the
screenshot (see `generate_log_svg`) they carry neither colour nor a timestamp.

An elided value stands in where a real one would be noise: a digest, a commit, and an integrity hash say only that
two of them differ, and their full length would wrap the line several times over.
"""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from update_time.domain.drift import DriftedPin
from update_time.domain.marker import Marker
from update_time.domain.version import DependencyVersion, Reference, Yank
from update_time.io.log import DEPENDENCY_DELIMITER, LOCATION_DELIMITER, Logger
from update_time.primitives.location import Location

_ELIDED = "…"
_ELIDED_DIGEST = f"sha256:{_ELIDED}"
_ELIDED_INTEGRITY_HASH = f"sha256-{_ELIDED}"
_STALE_DAYS = 512


class _Capture(logging.Handler):
    """Collect the records logged to it as the `LEVEL message` lines the README's console blocks show."""

    def __init__(self) -> None:
        """Start with nothing collected."""
        super().__init__()
        self._lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record and keep it, dropping the delimiters that only the highlighter reads."""
        delimiters = dict.fromkeys(ord(delimiter) for delimiter in (DEPENDENCY_DELIMITER, LOCATION_DELIMITER))
        self._lines.append(f"{record.levelname} {record.getMessage().translate(delimiters)}")

    def take(self) -> str:
        """Return the lines collected since the previous call, as one block, and start a new one."""
        block, self._lines = "\n".join(self._lines), []
        return block


def sample_warnings() -> dict[str, str]:
    """Return the README's sample warning blocks, keyed by the placeholder each one fills.

    The samples log to a logger of their own, which collects them and passes them on to nobody: they are the
    README's content, not diagnostics about generating it, and its own level keeps them independent of whatever
    logging the generator configured before it.
    """
    logger = Logger("update-time.readme-samples")
    capture = _Capture()
    logger.log.addHandler(capture)
    logger.log.setLevel(logging.DEBUG)
    logger.log.propagate = False
    try:
        return _blocks(logger, capture)
    finally:
        logger.log.removeHandler(capture)


def _blocks(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log each block's sample records and pair the lines they render as with the block's placeholder."""
    log.digest_drift(
        DriftedPin(Reference("python", "3.14", _ELIDED_DIGEST), _ELIDED_DIGEST, Location(Path("Dockerfile"), 1))
    )
    workflow = Location(Path(".github/workflows/ci.yml"), 17)
    log.tag_drift(DriftedPin(Reference("actions/checkout", "4.1.1", _ELIDED), _ELIDED, workflow))
    location = Location(Path("docs/conf.py"), 4)
    log.hash_mismatch("clipboard", "2.0.11", _ELIDED_INTEGRITY_HASH, _ELIDED_INTEGRITY_HASH, location)
    drift = capture.take()

    published = datetime.now(UTC) - timedelta(days=_STALE_DAYS, hours=1)
    stale = DependencyVersion("4.15.0", newest_published=published)
    log.warn_if_stale("humanize", stale, Location(Path("docs/requirements.txt")))
    staleness = capture.take()

    yanked = DependencyVersion("4.15.0", yank=Yank(yanked=True, reason="accidentally broke Python 3.10 support"))
    log.warn_if_yanked("humanize", yanked, Location(Path("docs/requirements.txt"), 12))
    yank = capture.take()

    log.redundant_yank_scope(
        "python", Marker(ignore_yanked=True, raw="ignore[yanked]"), Location(Path("Dockerfile"), 2)
    )
    return {
        "@@DRIFT_WARNINGS@@": drift,
        "@@STALE_WARNING@@": staleness,
        "@@YANKED_WARNING@@": yank,
        "@@REDUNDANT_MARKER_WARNING@@": capture.take(),
    }

"""Render the sample log lines the README quotes, by logging them through Update-time's own `Logger`.

The README quotes what Update-time logs for a drifted hash pin, a stale dependency, a yanked version, and the
markers it reads. Logging those samples here rather than transcribing them into the template is what keeps them
true: a reworded message rewrites the README on the next `just readme`, instead of leaving it quoting output the
tool no longer produces. The lines are rendered as the plain `LEVEL message` the README's `console` blocks show, so
unlike the screenshot (see `generate_log_svg`) they carry neither colour nor a timestamp.

An elided value stands in where a real one would be noise: a digest, a commit, and an integrity hash say only that
two of them differ, and their full length would wrap the line several times over.
"""

import logging
from pathlib import Path

from tools.log_fixtures import VULNERABILITY, reference, resolved, stale_publication_date
from update_time.domain.dependency import DependencyVersion, FloatingPin, Yank
from update_time.domain.directive import Reason
from update_time.domain.drift import DriftedPin
from update_time.domain.marker import Marker, Scope, Threshold
from update_time.domain.staleness import STALE_AFTER
from update_time.io.log import DEPENDENCY_DELIMITER, LOCATION_DELIMITER, Logger
from update_time.primitives.location import Location

_ELIDED = "…"
_ELIDED_DIGEST = f"sha256:{_ELIDED}"
_ELIDED_INTEGRITY_HASH = f"sha256-{_ELIDED}"


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


def sample_log_lines() -> dict[str, str]:
    """Return the README's sample log blocks, keyed by the placeholder each one fills.

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


def _redundant_directives(
    log: Logger, capture: _Capture, requirements: Location, dockerfile: Location
) -> dict[str, str]:
    """Log a sample per warning about a marker directive that holds nothing back, paired with the block's placeholder.

    A redundancy warning is handed the directive it names, so most samples pass one as text; the three about a
    vulnerability suppression read theirs off a marker, which is what their warnings take.
    """
    log.redundant_vulnerable_scope(reference("django", requirements, "4.2.0"), Marker(ignored_scopes=Scope.VULNERABLE))
    redundant_vulnerable_scope = capture.take()

    suppression = Marker(ignored_advisories=frozenset({"CVE-2022-28346"}))
    log.redundant_vulnerable_advisory(reference("django", requirements, "4.2.0"), suppression)
    redundant_vulnerable_advisory = capture.take()

    level = Marker(vulnerable=Threshold(value="high", directive="ignore[vulnerable<high]"))
    log.redundant_vulnerable_level(reference("django", requirements, "4.2.0"), level, "high")
    redundant_vulnerable_level = capture.take()

    log.redundant_directive(reference("python", dockerfile), "ignore[vulnerable]", Reason.NO_VULNERABILITY_REPORTS)
    redundant_vulnerable_source = capture.take()

    log.redundant_directive(reference("python", dockerfile), "ignore[yanked]", Reason.NO_YANK_CONCEPT)
    redundant_yank_scope = capture.take()

    log.redundant_directive(
        reference("humanize", requirements), "ignore[yanked]", Reason.NO_VERSION_TO_CHECK_FOR_A_YANK
    )
    redundant_yank_without_a_version = capture.take()

    log.redundant_directive(
        reference("humanize", requirements), "ignore[vulnerable]", Reason.NO_VERSION_TO_CHECK_FOR_A_VULNERABILITY
    )
    redundant_vulnerable_without_a_version = capture.take()

    cooldown_reference = reference("python", Location(Path(".python-version"), 2))
    log.redundant_directive(cooldown_reference, "ignore[cooldown<30]", Reason.NO_COOLDOWN_DATES)
    redundant_cooldown_item = capture.take()

    log.redundant_directive(
        reference("ghcr.io/astral-sh/uv", dockerfile), "ignore[stale<90]", Reason.NO_STALENESS_DATES
    )
    redundant_stale_source = capture.take()

    log.redundant_directive(reference("python", dockerfile), "allow[floating-pin]", Reason.NOTHING_FLOATING)
    redundant_floating_pin = capture.take()

    log.redundant_directive(reference("python", dockerfile), "allow[floating-pin]", Reason.UPDATE_HELD_BACK)
    return {
        "@@REDUNDANT_VULNERABLE_SCOPE_WARNING@@": redundant_vulnerable_scope,
        "@@REDUNDANT_VULNERABLE_ADVISORY_WARNING@@": redundant_vulnerable_advisory,
        "@@REDUNDANT_VULNERABLE_LEVEL_WARNING@@": redundant_vulnerable_level,
        "@@REDUNDANT_VULNERABLE_SOURCE_WARNING@@": redundant_vulnerable_source,
        "@@REDUNDANT_YANK_SCOPE_WARNING@@": redundant_yank_scope,
        "@@REDUNDANT_YANK_WITHOUT_A_VERSION_WARNING@@": redundant_yank_without_a_version,
        "@@REDUNDANT_VULNERABLE_WITHOUT_A_VERSION_WARNING@@": redundant_vulnerable_without_a_version,
        "@@REDUNDANT_COOLDOWN_ITEM_WARNING@@": redundant_cooldown_item,
        "@@REDUNDANT_STALE_SOURCE_WARNING@@": redundant_stale_source,
        "@@REDUNDANT_FLOATING_PIN_WARNING@@": redundant_floating_pin,
        "@@REDUNDANT_FROZEN_FLOATING_PIN_WARNING@@": capture.take(),
    }


def _blocks(log: Logger, capture: _Capture) -> dict[str, str]:
    """Log each block's sample records and pair the lines they render as with the block's placeholder."""
    log.digest_drift(
        DriftedPin("python", "3.14", Location(Path("Dockerfile"), 1), _ELIDED_DIGEST, new_sha=_ELIDED_DIGEST)
    )
    workflow = Location(Path(".github/workflows/ci.yml"), 17)
    log.tag_drift(DriftedPin("actions/checkout", "4.1.1", workflow, _ELIDED, new_sha=_ELIDED))
    location = Location(Path("docs/conf.py"), 4)
    log.hash_mismatch("clipboard", "2.0.11", _ELIDED_INTEGRITY_HASH, _ELIDED_INTEGRITY_HASH, location)
    drift = capture.take()

    requirements = Location(Path("docs/requirements.txt"), 12)
    dockerfile = Location(Path("Dockerfile"), 2)

    published = stale_publication_date()
    stale = DependencyVersion("4.15.0", newest_published=published)
    log.warn_if_stale(resolved("humanize", requirements, stale), STALE_AFTER.get())
    staleness = capture.take()

    yanked = DependencyVersion("4.15.0", yank=Yank(yanked=True, reason="accidentally broke Python 3.10 support"))
    log.warn_if_yanked(resolved("humanize", requirements, yanked))
    yank = capture.take()

    log.vulnerable_dependency(reference("django", requirements, "3.2.0"), VULNERABILITY)
    vulnerable = capture.take()

    redundant = _redundant_directives(log, capture, requirements, dockerfile)

    log.invalid_bracket_item("python", "stlae", dockerfile)
    unrecognised = capture.take()

    log.inverted_stale_item(reference("python", dockerfile), "stale>=90")
    inverted = capture.take()

    log.inverted_cooldown_item(reference("python", dockerfile), "cooldown>=30")
    inverted_cooldown = capture.take()

    log.inverted_vulnerable_item(reference("django", requirements), "vulnerable>=high")
    inverted_vulnerable = capture.take()

    pinned_tag = DependencyVersion("3.14.7", sha=_ELIDED_DIGEST)
    log.pinned(reference("python", Location(Path("Dockerfile"), 1)), pinned_tag)
    pinned_floating_tag = capture.take()

    compose = Location(Path("docker-compose.yml"), 7)
    log.unpinned_floating_tag(
        reference("acme/api", compose, "dev"), DependencyVersion("dev"), FloatingPin.NO_VERSION_TAG
    )
    log.unpinned_floating_tag(
        reference("acme/api", compose, "nightly"), DependencyVersion("nightly"), FloatingPin.NOT_LISTED
    )
    ghcr = Location(Path("Dockerfile"), 1)
    log.unpinned_floating_tag(
        reference("ghcr.io/acme/api", ghcr, "latest"), DependencyVersion("latest"), FloatingPin.NO_VERSION_TAG_EXAMINED
    )
    log.unpinned_floating_tag(
        reference("ghcr.io/acme/api", ghcr, "edge"), DependencyVersion("edge"), FloatingPin.NO_MANIFEST
    )
    unpinned_floating_tag = capture.take()

    log.keeping_floating_tag(reference("python", dockerfile, "latest"), pinned_tag, "update-time: allow[floating-pin]")
    kept_floating_tag = capture.take()

    marker = Marker(ignored_scopes=Scope.STALE, raw="ignore[stale]")
    log.recognised_marker("python", marker, dockerfile)
    recognised = capture.take()

    log.report_staleness(resolved("python", dockerfile, stale), marker, STALE_AFTER.get())
    return {
        "@@DRIFT_WARNINGS@@": drift,
        "@@PINNED_FLOATING_TAG@@": pinned_floating_tag,
        "@@UNPINNED_FLOATING_TAG@@": unpinned_floating_tag,
        "@@KEPT_FLOATING_TAG@@": kept_floating_tag,
        "@@STALE_WARNING@@": staleness,
        "@@YANKED_WARNING@@": yank,
        "@@VULNERABILITY_WARNING@@": vulnerable,
        **redundant,
        "@@UNRECOGNISED_ITEM_WARNING@@": unrecognised,
        "@@INVERTED_STALE_WARNING@@": inverted,
        "@@INVERTED_COOLDOWN_WARNING@@": inverted_cooldown,
        "@@INVERTED_VULNERABLE_WARNING@@": inverted_vulnerable,
        "@@RECOGNISED_MARKER@@": recognised,
        "@@HELD_BACK_MARKER@@": capture.take(),
    }

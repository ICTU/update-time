"""Render the log-output screenshot (docs/log-output.svg) shown in the README.

A handful of representative log lines are emitted through Update-time's own `Logger` and rendered by the handler
and theme a run uses, then exported as an SVG, so the README shows exactly how the coloured output looks.
"""

import io
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from rich.console import Console

from update_time.domain.dependency import Changes, DependencyVersion, Release
from update_time.domain.staleness import STALE_AFTER
from update_time.io.console import LOG_THEME
from update_time.io.log import Logger, configure_logging
from update_time.primitives.digest import SHA256_HEX_CHARS
from update_time.primitives.location import Location

from .log_fixtures import VULNERABILITY, reference, stale_publication_date


def generate() -> _LogOutput:
    """Return the sample log output as the SVG screenshot and as the plain text the README falls back to."""
    console = Console(
        record=True,
        width=100,
        force_terminal=True,
        file=io.StringIO(),  # capture the live render; we only want the exported SVG and text, not stdout noise
        theme=LOG_THEME,
    )
    configure_logging(console, "INFO").addFilter(_FixedTime())
    log = Logger("update-time")

    # A representative digest, padded to the exact length of a real one so `_LogHighlighter` recognises and dims it.
    digest = f"sha256:{f'9f2c1e7b{"d4" * SHA256_HEX_CHARS}'[:SHA256_HEX_CHARS]}"
    changelog = Changes(
        "Changed in 4.15.0\n"
        "- :zap: Fantastic new features\n"
        "- A few bugs squashed ([#42](https://github.com/python-humanize/humanize/issues/42))",
        markdown=True,
        url="https://github.com/python-humanize/humanize/releases/tag/4.15.0",
    )
    requirements = Location(Path("docs/requirements.txt"), 12)
    log.new_version(reference("humanize", requirements), DependencyVersion("4.15.0", changelog))
    log.pinned(reference("python", Location(Path("Dockerfile"), 1)), DependencyVersion("3.14.6", sha=digest))
    log.new_version(
        reference("actions/checkout", Location(Path(".github/workflows/ci.yml"), 17)), DependencyVersion("4.3.0")
    )
    stale = Release("1.3.0", stale_publication_date())
    log.stale_dependency(reference("left-pad", Location(Path("package.json"), 24)), stale, STALE_AFTER.get())
    log.vulnerable_dependency(reference("django", requirements, "3.2.0"), VULNERABILITY)

    plain_text = console.export_text(clear=False)  # capture before the export clears the recording
    svg = console.export_svg(title="update-time", unique_id="update-time-log")
    text = "\n".join(line.rstrip() for line in plain_text.splitlines())  # strip the trailing render padding
    return _LogOutput(svg=_portable(svg), text=text)


class _FixedTime(logging.Filter):
    """Pin every record's timestamp so the screenshot never changes just because the clock moved."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Overwrite the record's creation time with a fixed timestamp and keep the record."""
        # Read the time as local time, so it renders as 09:14:03 in every timezone. An isdst of -1 lets mktime decide
        # whether daylight saving time applies on that date.
        record.created = time.mktime((2026, 7, 10, 9, 14, 3, 0, 0, -1))
        return True


@dataclass(frozen=True, slots=True, kw_only=True)
class _LogOutput:
    """The sample log output in the two forms the README embeds: the SVG screenshot and its plain-text fallback."""

    svg: str
    text: str


def _portable(svg: str) -> str:
    """Make Rich's SVG render inline on GitHub, where it is embedded as an `<img>` from raw.githubusercontent.com.

    Two tweaks. The first copies the `viewBox` size onto the `<svg>` element, since Rich emits none and a
    size-less SVG shows as a broken image in Safari and renders tiny elsewhere. The second drops each
    `@font-face`'s external CDN `url(...)` source, which the raw host's `sandbox` CSP blocks and some renderers
    reject outright, leaving only the `local(...)` source so text falls back to a system monospace font.
    """
    match = _MATCH_PATTERN.search(svg)
    if match is None:  # pragma: no cover - Rich always emits a viewBox
        message = "Rich SVG has no viewBox to size the <img> from"
        raise ValueError(message)
    size = f'width="{match["width"]}" height="{match["height"]}"'
    svg = svg.replace('<svg class="rich-terminal"', f'<svg class="rich-terminal" {size}', 1)
    return _PORTABLE_PATTERN.sub("", svg)


_MATCH_PATTERN: Final[re.Pattern[str]] = re.compile(r'viewBox="0 0 (?P<width>\S+) (?P<height>\S+)"')

_PORTABLE_PATTERN: Final[re.Pattern[str]] = re.compile(r",\s*url\([^)]+\)\s*format\([^)]+\)")

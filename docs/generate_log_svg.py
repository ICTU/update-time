"""Render the log-output screenshot (docs/log-output.svg) shown in the README.

A handful of representative log lines are emitted through Update-time's own `Logger` and rendered with its
highlighter and theme, then exported as an SVG, so the README shows exactly how the coloured output looks.
"""

import io
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from update_time.domain.staleness import STALE_AFTER
from update_time.domain.version import SHA256_HEX_CHARS, DependencyVersion
from update_time.io.log import LOG_MESSAGE_FORMAT, LOG_THEME, LOG_TIME_FORMAT, Logger, LogHighlighter
from update_time.primitives.location import Location


@dataclass(frozen=True)
class LogOutput:
    """The sample log output in the two forms the README embeds: the SVG screenshot and its plain-text fallback."""

    svg: str
    text: str


class _FixedTime(logging.Filter):
    """Pin every record's timestamp so the screenshot never changes just because the clock moved."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Overwrite the record's creation time with a fixed timestamp and keep the record."""
        # Use a naive datetime, so it always renders as 09:14:03 regardless of the machine local timezone.
        record.created = datetime(2026, 7, 10, 9, 14, 3).timestamp()  # noqa: DTZ001
        return True


def _portable(svg: str) -> str:
    """Make Rich's SVG render inline on GitHub, where it is embedded as an `<img>` from raw.githubusercontent.com.

    Two tweaks. The first copies the `viewBox` size onto the `<svg>` element, since Rich emits none and a
    size-less SVG shows as a broken image in Safari and renders tiny elsewhere. The second drops each
    `@font-face`'s external CDN `url(...)` source, which the raw host's `sandbox` CSP blocks and some renderers
    reject outright, leaving only the `local(...)` source so text falls back to a system monospace font.
    """
    match = re.search(r'viewBox="0 0 (\S+) (\S+)"', svg)
    if match is None:  # pragma: no cover - Rich always emits a viewBox
        message = "Rich SVG has no viewBox to size the <img> from"
        raise ValueError(message)
    width, height = match.groups()
    svg = svg.replace('<svg class="rich-terminal"', f'<svg class="rich-terminal" width="{width}" height="{height}"', 1)
    return re.sub(r",\s*url\([^)]+\)\s*format\([^)]+\)", "", svg)


def generate() -> LogOutput:
    """Return the sample log output as the SVG screenshot and as the plain text the README falls back to."""
    console = Console(
        record=True,
        width=100,
        force_terminal=True,
        file=io.StringIO(),  # capture the live render; we only want the exported SVG and text, not stdout noise
        theme=LOG_THEME,
    )
    handler = RichHandler(console=console, highlighter=LogHighlighter(), show_path=False)
    handler.addFilter(_FixedTime())
    logging.basicConfig(level="INFO", datefmt=LOG_TIME_FORMAT, format=LOG_MESSAGE_FORMAT, handlers=[handler])
    log = Logger("update-time")

    # A representative digest, padded to the exact length of a real one so `LogHighlighter` recognises and dims it.
    digest = "sha256:" + ("9f2c1e7b" + "d4" * SHA256_HEX_CHARS)[:SHA256_HEX_CHARS]
    changelog = "Changed in 4.15.0\n- Fantastic new features\n- A few bugs squashed"
    log.new_version("humanize", DependencyVersion("4.15.0", changelog), Location(Path("docs/requirements.txt"), 12))
    log.pinned("python", DependencyVersion("3.14.6", sha=digest), Location(Path("Dockerfile"), 1))
    log.new_version("actions/checkout", DependencyVersion("4.3.0"), Location(Path(".github/workflows/ci.yml"), 17))
    # A manifest delegated to npm/pnpm, so no per-dependency line is surfaced: reported file-only, without a number.
    # The publication date is derived from the wall clock so the age it renders — 512 days — stays put as time passes.
    long_ago = datetime.now(UTC) - timedelta(days=512, hours=1)
    stale = DependencyVersion("1.3.0", newest_published=long_ago)
    log.warn_if_stale("left-pad", stale, Location(Path("package.json")), STALE_AFTER.get())

    plain_text = console.export_text(clear=False)  # capture before the export clears the recording
    svg = console.export_svg(title="update-time", unique_id="update-time-log")
    text = "\n".join(line.rstrip() for line in plain_text.splitlines())  # strip the trailing render padding
    return LogOutput(svg=_portable(svg), text=text)

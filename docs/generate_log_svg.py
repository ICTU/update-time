"""Generate the log-output screenshot (docs/log-output.svg) shown in the README.

A handful of representative Update-time log lines are rendered through the tool's own `LogHighlighter` and theme and
exported as an SVG, so the README shows exactly how the coloured output looks — bold dependency names, dim digests,
highlighted versions, and the INFO/WARNING levels. Everything is fixed (the timestamp and the SVG's element ids) so
regenerating produces a byte-identical file unless the sample or the styling actually changes.

`generate` also returns the same lines as plain text; `generate_readme` embeds that as the accessible fallback in the
README (the `<details>` block after the image) for readers whose viewer can't render the SVG, or who use a screen
reader. Run this module directly to print that text; regenerate the whole README (and this SVG) with `just readme`.
"""

import io
import logging
import re
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

from update_time.io.log import _DEPENDENCY_MARKER, Logger, LogHighlighter

_OUTPUT = Path(__file__).with_name("log-output.svg")
# A fixed wall-clock so the rendered timestamp — and therefore the SVG — is identical on every regeneration. The
# naive datetime round-trips through the local timezone, so it always renders as 09:14:03 regardless of the machine.
_FIXED_TIME = datetime(2026, 7, 10, 9, 14, 3).timestamp()  # noqa: DTZ001


class _FixedTime(logging.Filter):
    """Pin every record's timestamp so the screenshot never changes just because the clock moved."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Overwrite the record's creation time with the fixed one and keep the record."""
        record.created = _FIXED_TIME
        return True


def _mark(name: str) -> str:
    """Wrap a dependency name in the marker the highlighter colours, the way `Logger` does before logging."""
    return f"{_DEPENDENCY_MARKER}{name}{_DEPENDENCY_MARKER}"


def _portable(svg: str) -> str:
    """Make Rich's SVG render inline on GitHub, where it is embedded as an `<img>` from raw.githubusercontent.com.

    Two tweaks: copy the `viewBox` size onto the `<svg>` element (Rich emits none, so a size-less SVG shows as a
    broken image in Safari and renders tiny elsewhere), and drop each `@font-face`'s external CDN `url(...)` source
    (loading it is blocked by the raw host's `sandbox` CSP, and some renderers reject an SVG with external resources
    outright), leaving only the `local(...)` source so text falls back to a system monospace font.
    """
    match = re.search(r'viewBox="0 0 (\S+) (\S+)"', svg)
    if match is None:  # pragma: no cover - Rich always emits a viewBox
        message = "Rich SVG has no viewBox to size the <img> from"
        raise ValueError(message)
    width, height = match.groups()
    svg = svg.replace('<svg class="rich-terminal"', f'<svg class="rich-terminal" width="{width}" height="{height}"', 1)
    return re.sub(r",\s*url\([^)]+\)\s*format\([^)]+\)", "", svg)


def generate() -> str:
    """Write docs/log-output.svg and return the same log output as plain text (the README's accessible fallback)."""
    console = Console(
        record=True,
        width=100,
        force_terminal=True,
        file=io.StringIO(),  # capture the live render; we only want the exported SVG and text, not stdout noise
        theme=Theme({"repr.digest": "dim", "repr.dependency": "bold white"}),
    )
    handler = RichHandler(console=console, highlighter=LogHighlighter(), show_path=False)
    handler.addFilter(_FixedTime())
    logging.basicConfig(level="INFO", datefmt="[%X]", format="%(message)s", handlers=[handler])
    log = logging.getLogger("update-time")

    digest = "sha256:" + "9f2c1e7b" + "d4" * 28
    log.info(
        Logger.MESSAGE_NEW_VERSION,
        _mark("humanize"),
        "docs/requirements.txt",
        "4.15.0",
        "See the changelog for what changed",
    )
    log.info(Logger._MESSAGE_PINNED, _mark("python"), "Dockerfile", "3.14.6", digest)  # noqa: SLF001
    log.info(
        Logger.MESSAGE_NEW_VERSION,
        _mark("actions/checkout"),
        ".github/workflows/ci.yml",
        "4.3.0",
        Logger.NO_CHANGELOG,
    )
    log.warning(Logger._MESSAGE_STALE, _mark("left-pad"), "package.json", "1.3.0", 512, 365)  # noqa: SLF001

    plain_text = console.export_text(clear=False)  # capture before the export clears the recording
    svg = console.export_svg(title="update-time", unique_id="update-time-log")
    _OUTPUT.write_text(_portable(svg))
    return "\n".join(line.rstrip() for line in plain_text.splitlines())  # strip the trailing render padding


if __name__ == "__main__":
    print(generate())  # noqa: T201

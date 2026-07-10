"""Generate README.md from the docs/README.md.in template, filling in the generated content.

Two placeholders in the template are substituted so the machine-generated parts of the README never have to be
pasted by hand and can't drift from the real tool:

- `@@HELP_OUTPUT@@` — the output of `update-time -h`, wrapped to 80 columns.
- `@@LOG_OUTPUT@@`  — the sample log output as text, from `generate_log_svg` (which also (re)writes docs/log-output.svg,
  the screenshot embedded above the fallback).

Regenerate the README (and the screenshot) with `just readme`.
"""

import contextlib
import io
import os
import sys
from pathlib import Path

from docs.generate_log_svg import generate as generate_log_output

_TEMPLATE = Path(__file__).with_name("README.md.in")
_README = Path(__file__).parents[1] / "README.md"


def _help_output() -> str:
    """Return the `update-time -h` output, wrapped to 80 columns (argparse renders it two narrower)."""
    os.environ["COLUMNS"] = "80"  # argparse reads this to size its help; must be set before parse_args runs
    from update_time.io.cli import parse_args  # noqa: PLC0415 — imported here so COLUMNS is already set

    original_argv, sys.argv = sys.argv, ["update-time", "-h"]
    buffer = io.StringIO()
    try:
        # `-h` makes argparse print the help to stdout and raise SystemExit; capture the former, swallow the latter.
        with contextlib.redirect_stdout(buffer), contextlib.suppress(SystemExit):
            parse_args()
    finally:
        sys.argv = original_argv
    return buffer.getvalue().rstrip()


def main() -> None:
    """Fill the template's placeholders and write README.md."""
    readme = (
        _TEMPLATE.read_text()
        .replace("@@HELP_OUTPUT@@", _help_output())
        .replace("@@LOG_OUTPUT@@", generate_log_output())
    )
    _README.write_text(readme)


if __name__ == "__main__":
    main()

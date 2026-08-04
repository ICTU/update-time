"""Generate README.md from the docs/README.md.in template, filling in the generated content.

The template's placeholders are substituted so the machine-generated parts of the README:

- `@@HELP_OUTPUT@@` — the output of `update-time -h`, wrapped to 80 columns.
- `@@LOG_OUTPUT@@`  — the sample log output as text, from `generate_log_svg`, which also renders the screenshot
  embedded above that fallback.
- the sample log lines the sections below quote, from `log_samples`, each filling the placeholder it names.

Regenerate the README (and the screenshot) with `just readme`. Run with `--check` to report the generated files that
are out of date.
"""

import contextlib
import io
import os
import sys
from pathlib import Path

from docs.generate_log_svg import generate as generate_log_output
from docs.log_samples import sample_log_lines
from update_time.domain.staleness import STALE_AFTER

_THIS_FILE = Path(__file__)
_ROOT = _THIS_FILE.parents[1]
_README = _ROOT / "README.md"
_TEMPLATE = _THIS_FILE.with_name("README.md.in")
_SCREENSHOT = _THIS_FILE.with_name("log-output.svg")


def _help_output() -> str:
    """Return the `update-time -h` output, wrapped to 80 columns (argparse renders it two narrower)."""
    os.environ["COLUMNS"] = "80"  # argparse reads this to size its help; must be set before parse_args runs
    # ...and this to keep the colour argparse would otherwise add out of the captured help. Without it the README
    # would carry escape sequences whenever it is generated somewhere colour is wanted, such as from a terminal.
    os.environ["PYTHON_COLORS"] = "0"
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


def render() -> dict[Path, str]:
    """Return the content each generated file should have, keyed by the file it belongs in."""
    STALE_AFTER.set(STALE_AFTER.default)  # Pin the threshold the samples report, whatever the environment holds
    log_output = generate_log_output()
    readme = _TEMPLATE.read_text().replace("@@HELP_OUTPUT@@", _help_output()).replace("@@LOG_OUTPUT@@", log_output.text)
    for placeholder, log_lines in sample_log_lines().items():
        readme = readme.replace(placeholder, log_lines)
    return {_README: readme, _SCREENSHOT: log_output.svg}


def _out_of_date(generated: dict[Path, str]) -> list[Path]:
    """Return the generated files whose content on disk is not what regenerating them produces."""
    return [path for path, content in generated.items() if not path.is_file() or path.read_text() != content]


def main() -> None:
    """Write the generated files, or, given `--check`, report the ones that are out of date without writing any."""
    checking = "--check" in sys.argv[1:]
    generated = render()
    if not checking:
        for path, content in generated.items():
            path.write_text(content)
        return
    if stale := _out_of_date(generated):
        names = ", ".join(str(path.relative_to(_ROOT)) for path in sorted(stale))
        sys.exit(f"{names} out of date, run `just readme` to regenerate")


if __name__ == "__main__":
    main()

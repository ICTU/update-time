"""Generate README.md from the docs/README.md.in template, the log-output screenshot, and the index of the log messages.

The template's placeholders are substituted so the machine-generated parts of the README:

- `@@TABLE_OF_CONTENTS@@` — the table of contents chapter, linking to each of the template's chapters.
- `@@HELP_OUTPUT@@` — the output of `update-time -h`, wrapped to 80 columns.
- `@@DOCKER_IMAGE_FILES_TABLE@@` — the files the Docker images type is declared in, and the globs finding them.
- `@@LOG_OUTPUT@@`  — the sample log output as text, from `generate_log_svg`, which also renders the screenshot
  embedded above that fallback.
- the sample log lines the sections below quote, from `log_samples`, each filling the placeholder it names.

The index of the log messages, docs/log-messages.md, takes the sample of each name from them too (see `log_index`).

Regenerate the README, the screenshot, and the index with `just readme`. Run with `--check` to report the generated
files that are out of date.
"""

import contextlib
import io
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tools.generate_log_svg import generate
from tools.generated_files import write_or_check
from tools.log_index import LOG_INDEX, generate_log_index
from tools.log_samples import sample_log_lines
from tools.markdown import anchor, headings
from update_time.domain.dependency_type import DEPENDENCY_TYPES
from update_time.domain.staleness import STALE_AFTER

if TYPE_CHECKING:
    from update_time.domain.file_type import FileType

_ROOT: Final[Path] = Path(__file__).parents[1]
_DOCS: Final[Path] = _ROOT / "docs"
_README: Final[Path] = _ROOT / "README.md"
_TEMPLATE = _DOCS / "README.md.in"
_SCREENSHOT: Final[Path] = _DOCS / "log-output.svg"


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


def _table_of_contents(template: str) -> str:
    """Return the table of contents: its own chapter heading, then a link per heading, in the template's order.

    The heading is generated rather than written in the template, so that scanning the template can't find it and list
    the table of contents in itself.
    """
    toc_depth = 3
    entries = [f"{'  ' * (level - 2)}- [{title}]({anchor(title)})" for level, title in headings(template, 2, toc_depth)]
    return "\n".join(["## ☰ Table of contents", "", *entries])


def _globs(file_type: FileType) -> str:
    """Return the file type's glob patterns, in code."""
    return ", ".join(f"`{pattern}`" for pattern in file_type.patterns)


def _folder(file_type: FileType) -> str:
    """Return the folder the file type is walked in, in code, or the scan root itself where it names none."""
    return f"`{file_type.start}/`" if file_type.start else "the scan root"


def _render() -> dict[Path, str]:
    """Return the content each generated file should have, keyed by the file it belongs in."""
    STALE_AFTER.set(STALE_AFTER.default)  # Pin the threshold the samples report, whatever the environment holds
    log_output = generate()
    template = _TEMPLATE.read_text()
    readme = template.replace("@@TABLE_OF_CONTENTS@@", _table_of_contents(template))
    readme = readme.replace("@@HELP_OUTPUT@@", _help_output()).replace("@@LOG_OUTPUT@@", log_output.text)
    docker_image_files = [  # A row per file Docker images are declared in, saying how Update-time finds it
        f"| {file_type.name} | {_globs(file_type)} | {_folder(file_type)} | {'✅' if file_type.recursive else ''} |"
        for file_type in DEPENDENCY_TYPES.docker_images.file_types
    ]
    header = ["| Files | Globs | Folder | Recursive |", "| :---- | :---- | :----- | :-------: |"]
    readme = readme.replace("@@DOCKER_IMAGE_FILES_TABLE@@", "\n".join([*header, *docker_image_files]))
    samples = sample_log_lines()
    for placeholder, log_lines in samples.blocks.items():
        readme = readme.replace(placeholder, log_lines)
    return {_README: readme, _SCREENSHOT: log_output.svg, LOG_INDEX: generate_log_index(samples.by_name, readme)}


def main() -> None:
    """Write the generated files, or, given `--check`, report the ones that are out of date without writing any."""
    write_or_check(_render(), _ROOT, "just readme")


if __name__ == "__main__":
    main()

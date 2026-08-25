"""Generate README.md from the docs/README.md.in template, filling in the generated content.

The template's placeholders are substituted so the machine-generated parts of the README:

- `@@TABLE_OF_CONTENTS@@` — the table of contents chapter, linking to each of the template's chapters.
- `@@HELP_OUTPUT@@` — the output of `update-time -h`, wrapped to 80 columns.
- `@@DOCKER_IMAGE_FILES_TABLE@@` — the files the Docker images type is declared in, and the globs finding them.
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
from typing import TYPE_CHECKING

from tools.generate_log_svg import generate as generate_log_output
from tools.log_samples import sample_log_lines
from tools.markdown import anchor, headings
from update_time.domain.dependency_type import DEPENDENCY_TYPES
from update_time.domain.staleness import STALE_AFTER

if TYPE_CHECKING:
    from update_time.domain.dependency_type import DependencyType
    from update_time.domain.file_type import FileType

_ROOT = Path(__file__).parents[1]
_DOCS = _ROOT / "docs"
_README = _ROOT / "README.md"
_TEMPLATE = _DOCS / "README.md.in"
_SCREENSHOT = _DOCS / "log-output.svg"


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


def _table_of_contents(template: str, depth: int = 3) -> str:
    """Return the table of contents: its own chapter heading, then a link per heading, in the template's order.

    The depth is the deepest heading level listed, so a depth of 2 lists the chapters (`##`) alone and a depth of
    3 adds the sections within them, indented under the chapter they belong to. The heading is generated rather
    than written in the template, so that scanning the template can't find it and list the table of contents in
    itself.
    """
    entries = [f"{'  ' * (level - 2)}- [{title}]({anchor(title)})" for level, title in headings(template, 2, depth)]
    return "\n".join(["## ☰ Table of contents", "", *entries])


def _globs(file_type: FileType) -> str:
    """Return the file type's glob patterns, in code."""
    return ", ".join(f"`{pattern}`" for pattern in file_type.patterns)


def _folder(file_type: FileType) -> str:
    """Return the folder the file type is walked in, in code, or the scan root itself where it names none."""
    return f"`{file_type.start}/`" if file_type.start else "the scan root"


def _file_types_table(dependency_type: DependencyType) -> str:
    """Return the table naming the files this dependency type is declared in, and how each of them is found."""
    header = ["| Files | Globs | Folder | Recursive |", "| :---- | :---- | :----- | :-------: |"]
    rows = [
        f"| {file_type.name} | {_globs(file_type)} | {_folder(file_type)} | {'✅' if file_type.recursive else ''} |"
        for file_type in dependency_type.file_types
    ]
    return "\n".join(header + rows)


def render() -> dict[Path, str]:
    """Return the content each generated file should have, keyed by the file it belongs in."""
    STALE_AFTER.set(STALE_AFTER.default)  # Pin the threshold the samples report, whatever the environment holds
    log_output = generate_log_output()
    template = _TEMPLATE.read_text()
    readme = template.replace("@@TABLE_OF_CONTENTS@@", _table_of_contents(template))
    readme = readme.replace("@@HELP_OUTPUT@@", _help_output()).replace("@@LOG_OUTPUT@@", log_output.text)
    docker_images = _file_types_table(DEPENDENCY_TYPES.docker_images)
    readme = readme.replace("@@DOCKER_IMAGE_FILES_TABLE@@", docker_images)
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


if __name__ == "__main__":  # pragma: no cover
    main()

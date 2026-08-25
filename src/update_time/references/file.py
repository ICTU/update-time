"""Rewrite the references in files: read a file, transform its lines, and write it back when they changed.

The orchestration around the line-rewrite engine in `rewrite`: `rewrite_file` owns the read/compare/write cycle for
one file, `update_file` runs the engine over one file's lines, and `update_files` does so for every file of a
kind (discovered through `io.filesystem`).
"""

from typing import TYPE_CHECKING

from update_time.domain.line import located_lines
from update_time.io.filesystem import glob_for
from update_time.references.rewrite import update_references_in_lines

if TYPE_CHECKING:
    import re
    from collections.abc import Callable
    from pathlib import Path

    from update_time.domain.bound import NewVersionGetter
    from update_time.domain.file_type import FileType
    from update_time.domain.line import Line
    from update_time.io.log import Logger


def rewrite_file(path: Path, transform: Callable[[list[Line]], list[str]], logger: Logger) -> list[Line]:
    """Read the file, apply `transform` to its located lines, write it back if they changed, and return them.

    The read/compare/write boilerplate shared by every updater that rewrites a file line by line. `update_file`
    supplies a `transform` that runs the shared reference-rewriting engine; an updater whose references span more
    than one line — a pre-commit hook's `repo:` and its `rev:` sit on separate lines — supplies its own stateful
    pass instead. Each line keeps its own ending, so a file's CRLF endings and a missing final newline survive the
    rewrite. The lines returned are the ones the file now holds, so a check on the versions the run settled on reads
    them rather than the file again.
    """
    logger.path(path)
    old_lines = path.read_text().splitlines(keepends=True)
    new_lines = transform(located_lines(path, old_lines))
    if old_lines != new_lines:
        path.write_text("".join(new_lines))
    return located_lines(path, new_lines)


def update_file(
    path: Path,
    *regexps: str | re.Pattern[str],
    get_new_version: NewVersionGetter,
    logger: Logger,
    dependency: str = "",
) -> list[Line]:
    """Update the references in the file, write it back if the new lines differ from the old ones, and return them.

    Multiple regexps are applied in turn to the same content, so a file that pins more than one kind of reference (a
    devcontainer.json's base `image` and its `features`) is read and written once, not once per regexp. A regexp that
    captures no `dependency` group — a `.python-version` entry is a bare version — names it in `dependency`.
    """
    return rewrite_file(
        path,
        lambda lines: update_references_in_lines(
            lines, *regexps, get_new_version=get_new_version, logger=logger, dependency=dependency
        ),
        logger,
    )


def update_files(file_type: FileType, *, regexp: str, get_new_version: NewVersionGetter, logger: Logger) -> None:
    """Update the files of this kind, using the regexp to find the current version and get_new_version the new one."""
    for path in glob_for(file_type):
        update_file(path, regexp, get_new_version=get_new_version, logger=logger)

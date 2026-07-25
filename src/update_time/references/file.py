"""Rewrite the references in files: read a file, transform its lines, and write it back when they changed.

The orchestration around the line-rewrite engine in `rewrite`: `rewrite_file` owns the read/compare/write cycle for
one file, `update_file` runs the engine over one file's lines, and `update_files` does so for every file matching a
set of glob patterns (discovered through `io.filesystem`).
"""

from typing import TYPE_CHECKING

from update_time.io.filesystem import glob
from update_time.references.rewrite import update_references_in_lines

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from update_time.domain.bound import NewVersionGetter
    from update_time.io.log import Logger


def rewrite_file(path: Path, transform: Callable[[list[str]], list[str]], logger: Logger) -> None:
    """Read the file, apply `transform` to its lines, and write it back only if the lines changed.

    The read/compare/write boilerplate shared by every updater that rewrites a file line by line. `update_file`
    supplies a `transform` that runs the shared reference-rewriting engine; an updater whose references span more
    than one line — a pre-commit hook's `repo:` and its `rev:` sit on separate lines — supplies its own stateful
    pass instead.
    """
    logger.path(path)
    old_lines = path.read_text().splitlines()
    new_lines = transform(old_lines)
    if old_lines != new_lines:
        path.write_text("\n".join(new_lines) + "\n")


def update_file(path: Path, *regexps: str, get_new_version: NewVersionGetter, logger: Logger) -> None:
    """Update the references in the file and write it back if the new lines differ from the old lines.

    Multiple regexps are applied in turn to the same content, so a file that pins more than one kind of reference (a
    devcontainer.json's base `image` and its `features`) is read and written once, not once per regexp.
    """
    rewrite_file(
        path,
        lambda lines: update_references_in_lines(
            lines, *regexps, get_new_version=get_new_version, logger=logger, path=path
        ),
        logger,
    )


def update_files(
    *glob_patterns: str,
    regexp: str,
    get_new_version: NewVersionGetter,
    logger: Logger,
    start: Path | None = None,
    case_sensitive: bool | None = None,
) -> None:
    """Update the files using the regexp to find the current version and get_new_version to find new versions."""
    for path in glob(*glob_patterns, start=start, case_sensitive=case_sensitive):
        update_file(path, regexp, get_new_version=get_new_version, logger=logger)

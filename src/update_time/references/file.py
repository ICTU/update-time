"""Rewrite the references in files: read a file, transform its lines, and write it back when they changed.

These functions apply the line-rewrite engine `rewrite.update_references_in_lines` to a single file, or to
every file of a kind that `io.filesystem` discovers.
"""

from typing import TYPE_CHECKING

from update_time.formats import yaml as yaml_format
from update_time.io.filesystem import glob_for
from update_time.primitives.line import located_lines
from update_time.references.rewrite import update_references_in_lines
from update_time.sources.oci import ImagesNotPulled, TagGetter

if TYPE_CHECKING:
    import re
    from collections.abc import Callable
    from pathlib import Path

    from update_time.domain.dependency import ImageNotLookedUp
    from update_time.domain.file_type import FileType
    from update_time.domain.reference import Reference
    from update_time.domain.source import Source
    from update_time.formats.yaml import Document
    from update_time.io.log import Logger
    from update_time.primitives.line import Line


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
    source: Source,
    logger: Logger,
    dependency: str = "",
    declared: Reference | None = None,
) -> list[Line]:
    """Update the references in the file, write it back if the new lines differ from the old ones, and return them.

    Multiple regexps are applied in turn to the same content, so a file that pins more than one kind of reference (a
    devcontainer.json's base `image` and its `features`) is read and written once, not once per regexp. A regexp that
    captures no `dependency` group — a `.python-version` entry is a bare version — names it in `dependency`. Where a
    format cannot hold a comment beside its reference, it names the marker elsewhere and passes the reference in
    `declared`, carrying that marker. The pass then rewrites that reference alone.
    """
    return rewrite_file(
        path,
        lambda lines: update_references_in_lines(
            lines,
            *regexps,
            source=source,
            logger=logger,
            dependency=dependency,
            declared=declared,
        ),
        logger,
    )


def update_yaml_files(
    file_type: FileType,
    *,
    regexp: str | re.Pattern[str],
    find_images: Callable[[Document], set[str]],
    reason: ImageNotLookedUp,
    logger: Logger,
) -> None:
    """Update the images in the files of this kind, leaving the ones `find_images` reads from each file unchanged."""
    for path in glob_for(file_type):
        document = yaml_format.read(path)
        if document is None:
            logger.invalid_file(path, yaml_format.FORMAT)
        else:
            get_new_version = TagGetter(ImagesNotPulled(find_images(document), reason))
            update_file(path, regexp, source=get_new_version.source, logger=logger)

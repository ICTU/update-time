"""Rewrite the references in files with the line-rewrite engine, one file or every file of a kind."""

from typing import TYPE_CHECKING

from update_time.formats.yaml import FORMAT, read
from update_time.io.file import rewrite_file
from update_time.io.filesystem import glob_for
from update_time.sources.oci import ImagesNotPulled, image_source

from .rewrite import update_references_in_lines

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
        if (document := read(path)) is None:
            logger.invalid_file(path, FORMAT)
        else:
            tag_source = image_source(ImagesNotPulled(find_images(document), reason))
            update_file(path, regexp, source=tag_source, logger=logger)


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
    devcontainer.json's base `image` and its `features`) is read and written once, not once per regexp.
    """
    return rewrite_file(
        path,
        lambda lines: update_references_in_lines(
            lines, *regexps, source=source, logger=logger, dependency=dependency, declared=declared
        ),
        logger,
    )

"""Markdown facts shared by the tools that generate the README and check it."""

import re
from typing import TYPE_CHECKING

from update_time.formats import markdown as markdown_format

if TYPE_CHECKING:
    from collections.abc import Iterator


def lines_without_code_blocks(markdown: str) -> Iterator[tuple[int, str]]:
    """Yield each line with its number, empty where the line is a code fence or sits inside a fenced code block.

    What a code block holds is sample content rather than markup, so a line in one that starts with a `#` is a
    comment in the sample, not a heading of the document. The lines come back empty rather than left out, so that
    a line number still points at the line it came from, and so that a block between two tables still parts them.
    """
    in_code_block = False
    for line_number, line in enumerate(markdown.splitlines(), start=1):
        fence = markdown_format.is_fence(line)
        if fence:
            in_code_block = not in_code_block
        yield line_number, "" if in_code_block or fence else line


def without_code_blocks(markdown: str) -> str:
    """Return the markdown with the content of its fenced code blocks removed."""
    return "\n".join(line for _line_number, line in lines_without_code_blocks(markdown))


def headings(markdown: str, min_level: int = 1, max_level: int = 6) -> list[tuple[int, str]]:
    """Return the level and title of each heading between the levels given, in document order.

    Headings in fenced code blocks are left out, so a `#` line in a sample is read as the comment it is.
    """
    marked = (markdown_format.heading(line) for line in without_code_blocks(markdown).splitlines())
    return [(level, title) for level, title in filter(None, marked) if title and min_level <= level <= max_level]


def anchor(heading: str) -> str:
    """Return the anchor GitHub gives the heading.

    The heading is lower-cased, everything that is not a word character, a space, or a hyphen is dropped, and the
    remaining spaces become hyphens. An emoji is dropped but the space after it is not, which is why the anchor of
    a chapter whose heading starts with one begins with a hyphen.
    """
    return "#" + re.sub(r"[^\w\s-]", "", heading).lower().replace(" ", "-")

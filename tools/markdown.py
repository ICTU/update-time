"""Markdown facts shared by the tools that generate the README and check it."""

import re
from typing import TYPE_CHECKING

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
        if line.startswith("```"):
            in_code_block = not in_code_block
        yield line_number, "" if in_code_block or line.startswith("```") else line


def without_code_blocks(markdown: str) -> str:
    """Return the markdown with the content of its fenced code blocks removed."""
    return "\n".join(line for _line_number, line in lines_without_code_blocks(markdown))


def headings(markdown: str, min_level: int = 1, max_level: int = 6) -> list[tuple[int, str]]:
    """Return the level and title of each heading between the levels given, in document order.

    Headings in fenced code blocks are left out, so a `#` line in a sample is read as the comment it is.
    """
    found = re.findall(rf"(?m)^(#{{{min_level},{max_level}}}) (.+)$", without_code_blocks(markdown))
    return [(len(hashes), title) for hashes, title in found]


def anchor(heading: str) -> str:
    """Return the anchor GitHub gives the heading.

    The heading is lower-cased, everything that is not a word character, a space, or a hyphen is dropped, and the
    remaining spaces become hyphens. An emoji is dropped but the space after it is not, which is why the anchor of
    a chapter whose heading starts with one begins with a hyphen.
    """
    return "#" + re.sub(r"[^\w\s-]", "", heading).lower().replace(" ", "-")

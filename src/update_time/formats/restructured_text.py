"""What a reStructuredText file is named, what its markup says about its lines, and the text it shows."""

import re
import string
from itertools import pairwise

# A reStructuredText file carries this extension.
EXTENSION = ".rst"
# The characters reStructuredText allows a heading to be underlined with.
_ADORNMENT_CHARACTERS = frozenset(string.punctuation)
# Docutils reads a run of this length or longer as the underline of a longer title.
_SHORTEST_UNDERLINE = 4
# The target of a hyperlink, in the angle brackets that follow the link's text. A match starts where the whitespace
# before the target starts, so a long run of whitespace is not scanned again from each of its characters.
_LINK_TARGET = re.compile(r"(?<!\s)\s*<[^>]*>(?=`_)")
# An inline literal, its text between double backticks, such as ``abi3``.
_INLINE_LITERAL = re.compile(r"``(.+?)``")
# A label naming the section below it, such as `.. _v1-0:`.
_LABEL = re.compile(r"\.\. _[^:]+:")


def heading_underline(title: str, line: str) -> str:
    """Return the character the line underlines the title above it with, or nothing when it underlines nothing.

    An underline begins in column 1, and a blank line titles nothing. So neither a run indented inside a literal
    block nor a run below a blank line underlines anything. Nor does a run shorter than both its title and four
    characters, such as the `...` closing a paragraph or a code fence.
    """
    underline = line.rstrip()
    character = underline[:1]
    if (
        title.strip()
        and (len(underline) >= len(title) or len(underline) >= _SHORTEST_UNDERLINE)
        and len(underline) > 1
        and character in _ADORNMENT_CHARACTERS
        and underline == character * len(underline)
    ):
        return character
    return ""


def text(markup: str) -> str:
    """Return the text the markup shows: its headings without their underlines, its literals without backticks."""
    lines = ["", *markup.splitlines()]
    shown = "\n".join(line for title, line in pairwise(lines) if not heading_underline(title, line))
    return _INLINE_LITERAL.sub(r"\1", shown)


def is_label(line: str) -> bool:
    """Return whether the line labels the section below it."""
    return _LABEL.fullmatch(line) is not None


def without_link_targets(line: str) -> str:
    """Return the line with the target of each hyperlink left out, keeping the link's text."""
    return _LINK_TARGET.sub("", line)

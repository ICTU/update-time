"""What a reStructuredText file is named, and what its markup says about its lines."""

import re
import string

# A reStructuredText file carries this extension.
EXTENSION = ".rst"
# The characters reStructuredText allows a heading to be underlined with.
_ADORNMENT_CHARACTERS = frozenset(string.punctuation)
# The target of a hyperlink, in the angle brackets that follow the link's text. A match starts where the whitespace
# before the target starts, so a long run of whitespace is not scanned again from each of its characters.
_LINK_TARGET = re.compile(r"(?<!\s)\s*<[^>]*>(?=`_)")


def underline_character(line: str) -> str:
    """Return the character the line underlines the title above it with, or nothing when it underlines nothing.

    An underline begins in column 1. So a run indented inside a literal block underlines nothing.
    """
    underline = line.rstrip()
    character = underline[:1]
    if len(underline) > 1 and character in _ADORNMENT_CHARACTERS and underline == character * len(underline):
        return character
    return ""


def without_link_targets(line: str) -> str:
    """Return the line with the target of each hyperlink left out, keeping the link's text."""
    return _LINK_TARGET.sub("", line)

"""A line of a file, with the context needed to read a reference on it.

A reference is never read from its own line alone. An `# update-time:` marker may sit on the line above it instead of
inline, and a reported reference points at the line it sits on. So a line's text, the text above it, and its location
travel together as one `Line`, produced for a file's lines by `located_lines`.
"""

from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from update_time.domain.location import Location

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class Line:
    """One line of a file being rewritten: its text, the text of the line above it, and where it sits."""

    text: str
    previous_text: str
    location: Location


def located_lines(path: Path, lines: list[str]) -> list[Line]:
    """Return the file's lines, each carrying the text of the line above it and its own 1-based location.

    The first line carries an empty predecessor, since there is no line above it.
    """
    return [
        Line(text, previous_text, Location(path, line_number))
        for line_number, (previous_text, text) in enumerate(pairwise(["", *lines]), start=1)
    ]

"""A line of a file, with the text of the line above it and where it sits."""

from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from .location import Location

if TYPE_CHECKING:
    from pathlib import Path


def located_lines(path: Path, lines: list[str]) -> list[Line]:
    """Return the file's lines, each carrying the text of the line above it and its own 1-based location."""
    return [
        Line(text, previous_text, Location(path, line_number))
        for line_number, (previous_text, text) in enumerate(pairwise(["", *lines]), start=1)
    ]


@dataclass(frozen=True)
class Line:
    """One line of a file: its text, the text of the line above it, and where it sits."""

    text: str
    previous_text: str
    location: Location

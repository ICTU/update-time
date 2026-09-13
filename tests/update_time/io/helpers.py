"""Helpers shared by the tests of what a run logs and of what the console shows for it."""

from pathlib import Path

from update_time.io.console import delimit_dependency, delimit_location
from update_time.primitives.location import Location


def create_location(filename: str, line_number: int | None = None) -> Location:
    """Create a location in the current working directory."""
    return Location(Path.cwd() / filename, line_number)


def dependency(name: str) -> str:
    """Return the dependency name wrapped in its delimiter, as a log message carries it for the highlighter."""
    return delimit_dependency(name)


def at(path_and_line: str) -> str:
    """Return the location wrapped in its delimiter, as a log message carries it for the highlighter."""
    return delimit_location(path_and_line)

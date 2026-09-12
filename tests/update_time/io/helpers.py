"""Helpers shared by the tests of what a run logs and of what the console shows for it."""

from pathlib import Path

from update_time.io.console import DEPENDENCY_DELIMITER, LOCATION_DELIMITER
from update_time.primitives.location import Location


def create_location(filename: str, line_number: int | None = None) -> Location:
    """Create a location in the current working directory."""
    return Location(Path.cwd() / filename, line_number)


def dependency(name: str) -> str:
    """Return the dependency name wrapped in its delimiter, as a log message carries it for the highlighter."""
    return f"{DEPENDENCY_DELIMITER}{name}{DEPENDENCY_DELIMITER}"


def at(path_and_line: str) -> str:
    """Return the location wrapped in its delimiter, as a log message carries it for the highlighter."""
    return f"{LOCATION_DELIMITER}{path_and_line}{LOCATION_DELIMITER}"

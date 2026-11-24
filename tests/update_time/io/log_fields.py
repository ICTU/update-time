"""Build the fields a log message carries: a location, and a dependency or a location wrapped in its delimiter."""

from datetime import datetime
from pathlib import Path

from update_time.domain.dependency import DependencyVersion
from update_time.io.console import delimit_dependency, delimit_location
from update_time.primitives.location import Location
from update_time.primitives.lookup import LookedUp


def create_location(filename: str, line_number: int | None) -> Location:
    """Create a location in the current working directory."""
    return Location(Path.cwd() / filename, line_number)


def dependency(name: str) -> str:
    """Return the dependency name wrapped in its delimiter, as a log message carries it for the highlighter."""
    return delimit_dependency(name)


def at(path_and_line: str) -> str:
    """Return the location wrapped in its delimiter, as a log message carries it for the highlighter."""
    return delimit_location(path_and_line)


def published_version(version: str, published: str | datetime) -> str:
    """Return how the log renders the version published at the timestamp, given as a datetime or in ISO 8601."""
    timestamp = published if isinstance(published, datetime) else datetime.fromisoformat(published)
    return str(DependencyVersion(version, publication=LookedUp(timestamp)))

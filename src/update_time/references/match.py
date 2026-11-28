"""Read the reference a line carries: what the regexp matched, and the `# update-time:` marker steering it."""

import re
from typing import TYPE_CHECKING

from update_time.domain.marker import NO_MARKER
from update_time.domain.reference import Reference
from update_time.markers.parser import parse_marker

if TYPE_CHECKING:
    from collections.abc import Iterator

    from update_time.domain.marker import Marker
    from update_time.primitives.line import Line
    from update_time.primitives.location import Location


def matched_references(lines: list[Line], regexp: str | re.Pattern[str], dependency: str = "") -> Iterator[Reference]:
    """Yield the reference of each line the regexp matches, steered by its marker.

    Shared by the passes that report on lines without rewriting them. A line the regexp doesn't match is skipped,
    since it carries no reference and a marker on it belongs to the line below.
    """
    for line in lines:
        if match := re.search(regexp, line.text):
            yield matched_reference(match, line.location, dependency, marker=parse_marker(line))


def matched_reference(
    match: re.Match[str], location: Location, dependency: str = "", *, marker: Marker = NO_MARKER
) -> Reference:
    """Return the reference the match captured in its `dependency`, `version`, and `sha` named groups, at its line."""
    return Reference(
        dependency or match.group("dependency"),
        match.groupdict().get("version", ""),
        location,
        match.groupdict().get("sha") or "",
        can_hold_a_hash_pin(match),
        marker=marker,
    )


def can_hold_a_hash_pin(match: re.Match[str]) -> bool:
    """Return whether the pattern that matched the reference has a place for a hash pin."""
    return "sha" in match.re.groupindex

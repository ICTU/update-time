"""Read the reference a line carries: what the regexp matched, and the `# update-time:` marker steering it.

Shared by the passes that rewrite a file's lines and by those that only report on them, so both read a line the
same way. Deciding what to do with what is read belongs elsewhere: to `rewrite` for a reference being updated,
and to the reporting pass for one being checked.
"""

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


def _matched_dependency(match: re.Match[str], dependency: str = "") -> str:
    """Return the dependency the match captured in its `dependency` group, or `dependency` when it captures none."""
    return dependency or match.group("dependency")


def matched_reference(
    match: re.Match[str], location: Location, dependency: str = "", *, marker: Marker = NO_MARKER
) -> Reference:
    """Return the reference the match captured in its `dependency`, `version`, and `sha` named groups, at its line.

    The reference carries the marker steering it, or one that holds nothing back where the caller reads none.
    """
    return Reference(
        _matched_dependency(match, dependency),
        match.group("version"),
        location,
        match.groupdict().get("sha") or "",
        can_hold_a_hash_pin(match),
        marker=marker,
    )


def can_hold_a_hash_pin(match: re.Match[str]) -> bool:
    """Return whether the pattern that matched the reference has a place for a hash pin."""
    return "sha" in match.re.groupindex


def reference_matches(lines: list[Line], regexp: str | re.Pattern[str]) -> Iterator[tuple[Line, re.Match[str], Marker]]:
    """Yield each line the regexp matches, with its match and its marker.

    Shared by the passes that report on lines without rewriting them. A line the regexp doesn't match is skipped,
    since it carries no reference and a marker on it belongs to the line below.
    """
    for line in lines:
        match = re.search(regexp, line.text)
        if match is None:
            continue
        yield line, match, parse_marker(line)

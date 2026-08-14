"""Read the reference a line carries: what the regexp matched, and the `# update-time:` marker steering it.

Shared by the passes that rewrite a file's lines and by those that only report on them, so both read a line the
same way. Deciding what to do with what is read belongs elsewhere: to `rewrite` for a reference being updated,
and to the reporting pass for one being checked.
"""

import re
from typing import TYPE_CHECKING

from update_time.domain.marker import parse_marker
from update_time.domain.reference import Reference

if TYPE_CHECKING:
    from collections.abc import Iterator

    from update_time.domain.line import Line
    from update_time.domain.marker import Marker
    from update_time.io.log import Logger
    from update_time.primitives.location import Location


def matched_dependency(match: re.Match[str], dependency: str = "") -> str:
    """Return the dependency the match captured in its `dependency` group, or `dependency` when it captures none."""
    return dependency or match.group("dependency")


def matched_reference(match: re.Match[str], location: Location, dependency: str = "") -> Reference:
    """Return the reference the match captured in its `dependency` and `version` named groups, at its line."""
    return Reference(matched_dependency(match, dependency), match.group("version"), location)


def reference_matches(
    lines: list[Line], regexp: str | re.Pattern[str], logger: Logger | None = None
) -> Iterator[tuple[Line, re.Match[str], Marker]]:
    """Yield each line the regexp matches, with its match and its marker.

    Shared by the passes that report on lines without rewriting them. A line the regexp doesn't match is skipped,
    since it carries no reference and a marker on it belongs to the line below. What a marker holds back is each
    pass's own decision: the vulnerability pass skips a bare `ignore` because OSV is a source, while the
    loose-requirement pass reports a directive that decides nothing whatever the marker holds back, needing no
    source to do so. The pass that is the only one walking these lines passes a `logger`, which reports every
    marker read here as recognised. A pass reading lines a rewrite walked first passes none, since `apply_marker`
    reported their markers as it rewrote them.
    """
    for line in lines:
        match = re.search(regexp, line.text)
        if match is None:
            continue
        marker = parse_marker(line)
        if logger is not None:
            logger.recognised_marker(matched_dependency(match), marker, line.location)
        yield line, match, marker

"""Changelog parsing."""

import string

# The characters reStructuredText allows a heading to be underlined with.
_ADORNMENT_CHARACTERS = frozenset(string.punctuation)
# The level a section is bounded at when the version is named in prose rather than in a heading: the level a
# changelog most often heads its versions with.
_LEVEL_WITHOUT_HEADING = "##"


def _underline_character(lines: list[str], index: int) -> str:
    """Return the character underlining the line at the index, or nothing when the line below underlines nothing.

    reStructuredText has an underline begin in column 1, so an indented run, such as a literal block holds,
    underlines nothing.
    """
    below = lines[index + 1].rstrip() if index + 1 < len(lines) else ""
    character = below[:1]
    if len(below) > 1 and character in _ADORNMENT_CHARACTERS and below == character * len(below):
        return character
    return ""


def _heading_level(lines: list[str], index: int) -> str:
    """Return the run of characters marking the line at the index as a heading, or nothing when it marks none.

    Markdown marks a heading with a run of `#` before the title, reStructuredText by underlining the title with a
    run of one punctuation character. Either run says which level the heading is at, so two headings marked with
    the same run head sections at the same level. A blank line titles nothing, so a run below it heads nothing.
    """
    line = lines[index]
    if not line.strip():
        return ""
    after_hashes = line.lstrip("#")
    hashes = line[: len(line) - len(after_hashes)]
    if hashes and after_hashes.startswith(" "):
        return hashes
    return _underline_character(lines, index)


def _find_version_index(lines: list[str], version: str) -> int | None:
    """Return the index of the line that introduces the version's changes, or None when absent.

    A heading naming the version wins over any other line that names it, so that a prose mention inside another
    version's section (e.g. "shipped in 0.10.0") doesn't anchor parsing in the wrong place. Where no heading
    names the version, the first line that names it anchors parsing.
    """
    fallback = None
    for index, line in enumerate(lines):
        if version in line:
            if _heading_level(lines, index):
                return index
            if fallback is None:
                fallback = index
    return fallback


def get_version_changes_from_changelog(text: str, version: str, max_length: int = 20) -> str:
    """Return the changes for the version from the changelog, or nothing when the changelog does not name it."""
    all_lines = text.splitlines()
    start = _find_version_index(all_lines, version)
    if start is None:
        return ""
    level = _heading_level(all_lines, start) or _LEVEL_WITHOUT_HEADING
    previous_version_found = False
    lines = [all_lines[start]]
    for index, line in enumerate(all_lines[start + 1 :], start + 1):
        if _heading_level(all_lines, index) == level and version not in line:
            previous_version_found = True
            break
        lines.append(line)
    if not lines[-1].strip():
        lines = lines[:-1]  # Remove empty last line
    if len(lines) > max_length and not previous_version_found:
        lines = [*lines[:max_length], "..."]  # Add ellipsis if too many lines
    return "\n".join(lines)

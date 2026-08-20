"""Changelog parsing."""

import re
import string

# The characters reStructuredText allows a heading to be underlined with.
_ADORNMENT_CHARACTERS = frozenset(string.punctuation)
# The version a changelog names at the head of a section.
_VERSION = re.compile(r"\d+(?:\.\d+)+")
# The number of lines a version's changes are cut at where no later version ends them.
_MAX_LENGTH = 30
# The runs of characters Markdown fences a code block with.
_FENCES = ("```", "~~~")


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


def _names_version(line: str, version: str) -> bool:
    """Return whether the line names the version rather than a longer version spelling it.

    `2.9.0.post0` spells `2.9.0` at its start and `11.0` spells `1.0` at its end, and neither of them names the
    shorter version. A version headed `v1.0` is still named, so a letter before it is allowed where a digit is not.
    """
    return re.search(rf"(?<![\d.]){re.escape(version)}(?!\.?\w)", line) is not None


def _underlines_the_line_above(lines: list[str], index: int) -> bool:
    """Return whether the run of characters at the index underlines the line above it, heading that line.

    reStructuredText underlines a heading with a run of one character, backticks and tildes included, which a
    Markdown code fence is spelled with too. A run under a line it heads is an underline rather than a fence.
    """
    return index > 0 and _heading_level(lines, index - 1) == lines[index][:1]


def _fenced_indexes(lines: list[str]) -> frozenset[int]:
    """Return the indexes of the lines a fenced code block holds, its fences included.

    A fenced code block holds code rather than markup, so a line inside one neither names a version nor heads a
    section. Inside a block, a run of fence characters closes it, since code holds no headings to underline.
    """
    fenced = set()
    in_fence = False
    for index, line in enumerate(lines):
        if line.startswith(_FENCES) and (in_fence or not _underlines_the_line_above(lines, index)):
            in_fence = not in_fence
            fenced.add(index)
        elif in_fence:
            fenced.add(index)
    return frozenset(fenced)


def _find_version_index(lines: list[str], version: str) -> int | None:
    """Return the index of the line that introduces the version's changes, or None when absent.

    A heading naming the version wins over any other line that names it, so that a prose mention inside another
    version's section (e.g. "shipped in 0.10.0") doesn't anchor parsing in the wrong place. Where no heading
    names the version, the first line that names it anchors parsing.
    """
    fallback = None
    fenced = _fenced_indexes(lines)
    for index, line in enumerate(lines):
        if index not in fenced and _names_version(line, version):
            if _heading_level(lines, index):
                return index
            if fallback is None:
                fallback = index
    return fallback


def _ends_section(heading_level: str, section_level: str) -> bool:
    """Return whether a heading at the one level ends a section headed at the other.

    A section whose version is named in prose sits under no heading, so any heading ends it. A Markdown heading
    closes the sections under it, so a shallower one ends the section as an equal one does. reStructuredText
    takes a level from the order its adornments appear in rather than from the character it underlines with, so
    there only an equal level ends a section.
    """
    if not section_level:
        return bool(heading_level)
    return heading_level == section_level or (heading_level.startswith("#") and len(heading_level) < len(section_level))


def _heads_another_version(line: str, prefix: str, version: str) -> bool:
    """Return whether the line names another version after the prefix the version's own line carries.

    A changelog without heading markup repeats the text before the version, if any, on each version's line, so a
    line repeating that text and naming another version after it heads the next version's section.
    """
    if not line.startswith(prefix):
        return False
    other_version = _VERSION.match(line, len(prefix))
    return other_version is not None and other_version.group() != version


def _version_line_prefix(lines: list[str], index: int, version: str) -> str | None:
    """Return the text before the version on the line at the index, or None when that line is a heading."""
    line = lines[index]
    return None if _heading_level(lines, index) else line[: line.index(version)]


def _next_version_index(lines: list[str], start: int, version: str) -> int | None:
    """Return the index of the line heading the next version's section, or None when no line heads one.

    A section headed by a heading ends at the next heading naming another version that closes it. One introduced
    by a line that is no heading ends at any heading, or at the next line repeating the text before the version
    with another version.
    """
    level = _heading_level(lines, start)
    prefix = _version_line_prefix(lines, start, version)
    fenced = _fenced_indexes(lines)
    for index, line in enumerate(lines[start + 1 :], start + 1):
        if index in fenced or _names_version(line, version):
            continue
        if _ends_section(_heading_level(lines, index), level) or (
            prefix is not None and _heads_another_version(line, prefix, version)
        ):
            return index
    return None


def get_version_changes_from_changelog(text: str, version: str) -> str:
    """Return the changes for the version from the changelog, or nothing when the changelog does not name it.

    Where no later version ends the changes, they are cut short and a `...` line marks where.
    """
    all_lines = text.splitlines()
    start = _find_version_index(all_lines, version)
    if start is None:
        return ""
    end = _next_version_index(all_lines, start, version)
    lines = all_lines[start:end]
    if not lines[-1].strip():
        lines = lines[:-1]
    if len(lines) > _MAX_LENGTH and end is None:
        lines = [*lines[:_MAX_LENGTH], "..."]
    return "\n".join(lines)

"""Check the README's structure: the types its tables list, the details chapter's sections, and its links.

Every dependency type table answers one question about each type. The tables are held to the declared dependency
types, so a type that is declared but never documented fails the check. The details chapter carries one section
per dependency type, and each of those sections answers the same questions in the same order. A question left out
of one type's section is invisible in prose review, so it is checked here, and so is each internal link, since an
anchor that no heading provides fails no other check and shows up only when a reader clicks it.
"""

import re
import sys
from pathlib import Path

from tools.markdown import anchor, headings, without_code_blocks
from update_time.domain.dependency_type import DEPENDENCY_TYPES

_DETAILS_CHAPTER = "Details per dependency type"

# The table naming each dependency type's files is found by what its second column answers.
_FILES_TABLE = "Files"

# The dependency types every table has to answer for, in the order the tables have to list them in.
_TYPE_NAMES = tuple(dependency_type.name for dependency_type in DEPENDENCY_TYPES)

# The header of a table whose rows name the files a dependency type is declared in.
_FILES_COLUMN = "| Files |"

_ROW_LINK = re.compile(r"^\[(?P<label>[^\]]+)\]\((?P<anchor>#[^)]+)\)$")

_SUBSECTIONS = (
    "What files are updated?",
    "What dependencies are updated?",
    "What versions are updated?",
    "Pinning",
    "Cooldown",
    "Stale dependencies",
    "Yanked dependencies",
    "Vulnerable dependencies",
    "Archived dependencies",
    "Markers",
)


def _details_chapter(markdown: str) -> str:
    """Return the text of the details chapter, or the empty string when the document has none."""
    chapters = re.split(r"(?m)^(?=## )", markdown)
    return "".join(chapter for chapter in chapters if _DETAILS_CHAPTER in chapter.partition("\n")[0])


def _section_bodies(markdown: str) -> dict[str, str]:
    """Return the text of each dependency type section in the details chapter, keyed by its title."""
    parts = re.split(r"(?m)^### (.+)$", _details_chapter(markdown))[1:]  # Title, body, title, body, ...
    return dict(zip(parts[::2], parts[1::2], strict=True))


def _sections(markdown: str) -> dict[str, list[str]]:
    """Return each dependency type section in the details chapter with its subsection titles, in document order."""
    return {title: re.findall(r"(?m)^#### (.+)$", body) for title, body in _section_bodies(markdown).items()}


def _anchors(markdown: str) -> set[str]:
    """Return the anchor that each heading in the document provides."""
    return {anchor(title) for _level, title in headings(markdown)}


def _anchor_problems(markdown: str) -> list[str]:
    """Return a message for each internal link that points at an anchor no heading in the document provides."""
    anchors = _anchors(markdown)
    links = re.findall(r"\]\((#[^)]+)\)", markdown)
    return [f"no heading provides the anchor '{link}'" for link in links if link not in anchors]


def _tables(markdown: str) -> dict[str, list[tuple[str, str]]]:
    """Return each table's rows as a dependency type and the answer given for it, keyed by the column's question."""
    tables: dict[str, list[tuple[str, str]]] = {}
    header = None
    for line in without_code_blocks(markdown).splitlines():
        if line.startswith("| Dependency type |"):
            header = line.split("|")[2].strip()
            tables[header] = []
        elif header is not None and line.startswith("|"):
            cells = line.split("|")
            # The separator row holds nothing but dashes, alignment colons, and spaces, so it is no dependency type.
            if set(row := cells[1].strip()) - set("-: "):
                tables[header].append((row, cells[2].strip()))
        else:
            header = None
    return tables


def _list_problems(found: list[str], expected: list[str], subject: str, item: str) -> list[str]:
    """Return a message for each item the subject leaves out, adds, or carries out of order.

    The order is only reported once the subject has exactly the expected items, since one left out shifts every
    item after it, which would bury the omission under a report per item.
    """
    messages = [f"{subject} is missing the {item} '{name}'" for name in expected if name not in found]
    messages += [f"{subject} has an unexpected {item} '{name}'" for name in found if name not in expected]
    pairs = zip(found, expected, strict=False)  # Not strict: the lengths differ when one is missing.
    misplaced = [(seen, wanted) for seen, wanted in pairs if seen != wanted]
    if not messages and misplaced:
        seen, wanted = misplaced[0]
        messages.append(f"{subject} has the {item} '{seen}' where '{wanted}' was expected")
    return messages


def _row_label(row: str) -> str:
    """Return the dependency type the row names, whether the row links to its section or names it plainly."""
    return match["label"] if (match := _ROW_LINK.match(row)) else row


def _row_labels(rows: list[tuple[str, str]]) -> list[str]:
    """Return the dependency type each of the table's rows names."""
    return [_row_label(row) for row, _answer in rows]


def _files_problems(markdown: str) -> list[str]:
    """Return a message for each file type a dependency type declares that its row in the Files table doesn't name.

    A type with no row at all is left to the table check. Backticks are dropped from the cell before looking, so
    a file type the README spells as code is found.
    """
    cells = {_row_label(row): files.replace("`", "") for row, files in _tables(markdown).get(_FILES_TABLE, [])}
    problems = []
    for dependency_type in DEPENDENCY_TYPES:
        if (cell := cells.get(dependency_type.name)) is None:
            continue
        problems += [
            f"the '{_FILES_TABLE}' table's row '{dependency_type.name}' doesn't name '{file_type.name}'"
            for file_type in dependency_type.file_types
            if file_type.name not in cell
        ]
    return problems


def _table_problems(markdown: str) -> list[str]:
    """Return a message for each way a dependency type table differs from the declared dependency types.

    The tables answer one question each about every dependency type, so a type left out of one of them is an
    answer nobody wrote down, which reads as an omission rather than as a deliberate blank.
    """
    return [
        problem
        for header, rows in _tables(markdown).items()
        for problem in _list_problems(_row_labels(rows), list(_TYPE_NAMES), f"the '{header}' table", "row")
    ]


def _row_link_problems(markdown: str) -> list[str]:
    """Return a message for each table row whose section doesn't name the dependency type the row labels."""
    titles = {anchor(title): title for _level, title in headings(markdown)}
    return [
        f"the '{header}' table's row '{match['label']}' links to '{title}', which doesn't name it"
        for header, rows in _tables(markdown).items()
        for row, _answer in rows
        if (match := _ROW_LINK.match(row))
        and (title := titles.get(match["anchor"]))
        and not _documents(title, match["label"])
    ]


def _documents(title: str, name: str) -> bool:
    """Return whether the section title documents the dependency type.

    The title documents the type when it holds the type's name. A section shared by two types names both, so the
    name has to appear in the title rather than be all of it.
    """
    return name.lower() in title.lower()


def _type_section_problems(markdown: str) -> list[str]:
    """Return a message for each dependency type with no section of its own, and each section documenting none."""
    titles = list(_sections(markdown))
    problems = [
        f"the details chapter has no section for '{name}'"
        for name in _TYPE_NAMES
        if not any(_documents(title, name) for title in titles)
    ]
    problems += [
        f"the details chapter's section '{title}' documents no dependency type"
        for title in titles
        if not any(_documents(title, name) for name in _TYPE_NAMES)
    ]
    return problems


def _file_rows(body: str) -> list[list[str]]:
    """Return the files each table in the section lists, one list per table whose first column names files."""
    tables: list[list[str]] = []
    rows: list[str] | None = None
    for line in without_code_blocks(body).splitlines():
        if line.startswith(_FILES_COLUMN):
            tables.append(rows := [])
        elif rows is not None and line.startswith("|"):
            # The separator row holds nothing but dashes, alignment colons, and spaces, so it names no file.
            if set(file := line.split("|")[1].strip()) - set("-: "):
                rows.append(file)
        else:
            rows = None
    return tables


def _file_type_problems(markdown: str) -> list[str]:
    """Return a message for each table of files that differs from the file types its section's types declare."""
    problems = []
    for title, body in _section_bodies(markdown).items():
        expected = [
            file_type.name
            for dependency_type in DEPENDENCY_TYPES
            if _documents(title, dependency_type.name)
            for file_type in dependency_type.file_types
        ]
        for rows in _file_rows(body):
            problems += _list_problems(rows, expected, f"the files table in '{title}'", "row")
    return problems


def _subsection_problems(title: str, subsections: list[str]) -> list[str]:
    """Return a message for each subsection the type section leaves out, adds, or carries out of order."""
    return _list_problems(subsections, list(_SUBSECTIONS), title, "subsection")


def _problems(markdown: str) -> list[str]:
    """Return a message for each way the document differs from the structure the dependency types call for.

    Renaming the details chapter, or emptying it, leaves every declared dependency type without a section, which
    `_type_section_problems` reports one type at a time.
    """
    subsection_problems = [
        problem
        for title, subsections in _sections(markdown).items()
        for problem in _subsection_problems(title, subsections)
    ]
    return (
        subsection_problems
        + _type_section_problems(markdown)
        + _table_problems(markdown)
        + _files_problems(markdown)
        + _file_type_problems(markdown)
        + _row_link_problems(markdown)
        + _anchor_problems(markdown)
    )


def main() -> int:
    """Report the problems of the documents at the paths given on the command line, and return the exit code."""
    exit_code = 0
    for path in [Path(argument) for argument in sys.argv[1:]]:
        for problem in _problems(path.read_text(encoding="utf-8")):
            sys.stdout.write(f"{path}: {problem}\n")
            exit_code = 1
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

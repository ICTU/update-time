"""Check the README's structure: the details chapter's sections, and the links between sections.

The details chapter carries one section per dependency type, and each of those sections answers the same questions
in the same order. A question left out of one type's section is invisible in prose review, so it is checked here,
and so is each internal link, since an anchor that no heading provides fails no other check and shows up only when
a reader clicks it.
"""

import re
import sys
from pathlib import Path

from tools.markdown import anchor, headings, without_code_blocks

_DETAILS_CHAPTER = "Details per dependency type"

_SUBSECTIONS = (
    "What files are updated?",
    "What dependencies are updated?",
    "What versions are updated?",
    "Pinning",
    "Cooldown",
    "Stale dependencies",
    "Yanked dependencies",
    "Vulnerable dependencies",
    "Markers",
)


def _details_chapter(markdown: str) -> str:
    """Return the text of the details chapter, or the empty string when the document has none."""
    chapters = re.split(r"(?m)^(?=## )", markdown)
    return "".join(chapter for chapter in chapters if _DETAILS_CHAPTER in chapter.partition("\n")[0])


def _sections(markdown: str) -> dict[str, list[str]]:
    """Return each dependency type section in the details chapter with its subsection titles, in document order."""
    parts = re.split(r"(?m)^### (.+)$", _details_chapter(markdown))[1:]  # Title, body, title, body, ...
    return {title: re.findall(r"(?m)^#### (.+)$", body) for title, body in zip(parts[::2], parts[1::2], strict=True)}


def _anchors(markdown: str) -> set[str]:
    """Return the anchor that each heading in the document provides."""
    return {anchor(title) for _level, title in headings(markdown)}


def _anchor_problems(markdown: str) -> list[str]:
    """Return a message for each internal link that points at an anchor no heading in the document provides."""
    anchors = _anchors(markdown)
    links = re.findall(r"\]\((#[^)]+)\)", markdown)
    return [f"no heading provides the anchor '{link}'" for link in links if link not in anchors]


def _tables(markdown: str) -> dict[str, list[str]]:
    """Return the dependency types each table lists, keyed by what that table's second column answers."""
    tables: dict[str, list[str]] = {}
    header = None
    for line in without_code_blocks(markdown).splitlines():
        if line.startswith("| Dependency type |"):
            header = line.split("|")[2].strip()
            tables[header] = []
        elif header is not None and line.startswith("|"):
            # The separator row holds nothing but dashes, alignment colons, and spaces, so it is no dependency type.
            if set(row := line.split("|")[1].strip()) - set("-: "):
                tables[header].append(row)
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


def _table_problems(markdown: str) -> list[str]:
    """Return a message for each way a dependency type table differs from the types the first table lists.

    The tables answer one question each about every dependency type, so a type left out of one of them is an
    answer nobody wrote down, which reads as an omission rather than as a deliberate blank.
    """
    tables = _tables(markdown)
    expected = next(iter(tables.values()), [])
    return [
        problem
        for header, rows in tables.items()
        for problem in _list_problems(rows, expected, f"the '{header}' table", "row")
    ]


_ROW_LINK = re.compile(r"^\[(?P<label>[^\]]+)\]\((?P<anchor>#[^)]+)\)$")


def _row_link_problems(markdown: str) -> list[str]:
    """Return a message for each table row whose section doesn't name the dependency type the row labels.

    A section shared by two types names both of them ("GitHub Actions and pre-commit hooks"), so the row's label
    has to appear in the heading rather than be all of it.
    """
    titles = {anchor(title): title for _level, title in headings(markdown)}
    return [
        f"the '{header}' table's row '{match['label']}' links to '{title}', which doesn't name it"
        for header, rows in _tables(markdown).items()
        for row in rows
        if (match := _ROW_LINK.match(row))
        and (title := titles.get(match["anchor"]))
        and match["label"].lower() not in title.lower()
    ]


def _section_problems(title: str, subsections: list[str]) -> list[str]:
    """Return a message for each subsection the type section leaves out, adds, or carries out of order."""
    return _list_problems(subsections, list(_SUBSECTIONS), title, "subsection")


def _problems(markdown: str) -> list[str]:
    """Return a message for each way the dependency type sections differ from the expected structure.

    A document with no type sections is reported too. Without that, renaming the details chapter, or emptying it,
    would leave the check passing while it has stopped checking anything.
    """
    sections = _sections(markdown)
    if not sections:
        return ["found no dependency type sections to check"]
    section_problems = [
        problem for title, subsections in sections.items() for problem in _section_problems(title, subsections)
    ]
    return section_problems + _table_problems(markdown) + _row_link_problems(markdown) + _anchor_problems(markdown)


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

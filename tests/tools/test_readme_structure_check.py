"""Unit tests for the README structure check."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tools import readme_structure_check as structure_check
from tools.readme_structure_check import (
    _SUBSECTIONS,
    _anchors,
    _problems,
    _row_link_problems,
    _sections,
    _table_problems,
    _tables,
    main,
)

from update_time.domain.dependency_type import DEPENDENCY_TYPES

from tests.mutation import Mutation, kills

# The dependency types the README has to answer for, derived here rather than read off the check, so a fixture
# would still name them all if the check built its own list wrongly.
_TYPE_NAMES = tuple(dependency_type.name for dependency_type in DEPENDENCY_TYPES)


def _chapter_with_files_table(rows: list[str]) -> str:
    """Return a details chapter with a table of files under the Docker images section's first subsection."""
    table = "\n".join(["| Files | Globs |", "| :- | :- |", *rows])
    marker = f"### {DEPENDENCY_TYPES.docker_images.name}\n\n#### {_SUBSECTIONS[0]}"
    return _details_chapter().replace(marker, f"{marker}\n\n{table}")


def _files_table(cells: dict[str, str] | None = None) -> str:
    """Return the Files table, each type's cell naming the files it declares, save the cells given here."""
    answers = {
        dependency_type.name: ", ".join(file_type.name for file_type in dependency_type.file_types)
        for dependency_type in DEPENDENCY_TYPES
    }
    rows = "\n".join(f"| {name} | {answer} |" for name, answer in (answers | (cells or {})).items())
    return f"| Dependency type | Files |\n| --- | --- |\n{rows}\n"


def _details_chapter(*section_titles: str, subsections: tuple[str, ...] = _SUBSECTIONS) -> str:
    """Return a details chapter with a section per title, the first carrying the given subsections.

    Given no titles, the chapter documents every declared dependency type, so `_problems` reports nothing. Only
    the first section takes the given subsections, so a fault put there is reported once rather than once per
    section.
    """
    markdown = ["## 📖 Details per dependency type"]
    for index, title in enumerate(section_titles or _TYPE_NAMES):
        markdown.append(f"### {title}")
        markdown.extend(f"#### {subsection}" for subsection in (subsections if index == 0 else _SUBSECTIONS))
    return "\n\n".join(markdown) + "\n"


def _chapter_without_markers() -> str:
    """Return a details chapter whose first type section leaves the Markers subsection out."""
    return _details_chapter(subsections=_SUBSECTIONS[:-1])


class SectionsTest(unittest.TestCase):
    """Unit tests for the type sections found in the document."""

    def test_each_type_section_with_its_subsections(self):
        """Test that every type section is found, with its subsections in document order."""
        expected = {"Python dependencies": list(_SUBSECTIONS), "Docker images": list(_SUBSECTIONS)}
        self.assertEqual(_sections(_details_chapter("Python dependencies", "Docker images")), expected)


class ProblemsTest(unittest.TestCase):
    """Unit tests for the problems reported for a document."""

    def test_complete_document_has_no_problems(self):
        """Test that a document documenting every type, each section carrying every subsection, is sound."""
        self.assertEqual(_problems(_details_chapter()), [])

    def test_missing_subsection_is_reported(self):
        """Test that a type section without one of the subsections is reported, naming both."""
        expected = ["Python dependencies is missing the subsection 'Markers'"]
        self.assertEqual(_problems(_chapter_without_markers()), expected)

    def test_document_without_type_sections_is_reported(self):
        """Test that a document with nothing to check is reported, naming every type that has no section."""
        documents = {
            "no details chapter at all": "## ⚡ Usage\n\nRun it.\n",
            "a details chapter without type sections": "## 📖 Details per dependency type\n\nComing soon.\n",
        }
        expected = [f"the details chapter has no section for '{name}'" for name in _TYPE_NAMES]
        for description, markdown in documents.items():
            with self.subTest(document=description):
                self.assertEqual(_problems(markdown), expected)

    @kills(
        Mutation(
            structure_check,
            "        + _type_section_problems(markdown)\n",
            "",
            "a declared dependency type the details chapter has no section for goes unreported",
        )
    )
    def test_declared_type_without_a_section_is_reported(self):
        """Test that a dependency type no section in the details chapter names is reported, naming the type."""
        markdown = _details_chapter(*_TYPE_NAMES[:-1])
        expected = [f"the details chapter has no section for '{_TYPE_NAMES[-1]}'"]
        self.assertEqual(_problems(markdown), expected)

    @kills(
        Mutation(
            structure_check,
            """    problems += [
        f"the details chapter's section '{title}' documents no dependency type"
        for title in titles
        if not any(_documents(title, name) for name in _TYPE_NAMES)
    ]
""",
            "",
            "a section of the details chapter that documents no dependency type goes unreported",
        )
    )
    def test_section_documenting_no_declared_type_is_reported(self):
        """Test that a section in the details chapter naming no declared dependency type is reported."""
        markdown = _details_chapter(*_TYPE_NAMES, "Cargo crates")
        expected = ["the details chapter's section 'Cargo crates' documents no dependency type"]
        self.assertEqual(_problems(markdown), expected)

    @kills(
        Mutation(
            structure_check,
            "        + _files_problems(markdown)\n",
            "",
            "a file a dependency type declares that the README's Files table leaves out goes unreported",
        )
    )
    def test_file_a_types_cell_leaves_out_is_reported(self):
        """Test that a file type a dependency type declares that its Files cell doesn't name is reported."""
        markdown = _details_chapter() + "\n" + _files_table({_TYPE_NAMES[-1]: "a file"})
        left_out = list(DEPENDENCY_TYPES)[-1].file_types[0].name
        expected = [f"the 'Files' table's row '{_TYPE_NAMES[-1]}' doesn't name '{left_out}'"]
        self.assertEqual(_problems(markdown), expected)

    def test_files_table_leaving_out_a_declared_file_type_is_reported(self):
        """Test that a table of files leaving out one of its section's declared file types is reported."""
        declared = DEPENDENCY_TYPES.docker_images.file_types
        rows = [f"| {file_type.name} | a glob |" for file_type in declared[:-1]]
        expected = [f"the files table in 'Docker images' is missing the row '{declared[-1].name}'"]
        self.assertEqual(_problems(_chapter_with_files_table(rows)), expected)

    def test_unexpected_subsection_is_reported(self):
        """Test that a type section with a subsection the other types don't have is reported, naming it."""
        markdown = _details_chapter(subsections=(*_SUBSECTIONS, "Notes"))
        self.assertEqual(_problems(markdown), ["Python dependencies has an unexpected subsection 'Notes'"])

    def test_subsections_out_of_order_are_reported(self):
        """Test that a type section carrying the expected subsections in another order is reported."""
        swapped = (*_SUBSECTIONS[:3], _SUBSECTIONS[4], _SUBSECTIONS[3], *_SUBSECTIONS[5:])
        expected = ["Python dependencies has the subsection 'Cooldown' where 'Pinning' was expected"]
        self.assertEqual(_problems(_details_chapter(subsections=swapped)), expected)


# What a table built for these tests answers for each of its dependency types, when the answer is irrelevant.
_ANSWER = "an answer"


def _dependency_type_table(header: str, *types: str) -> str:
    """Return a table of dependency types, with the header naming what its second column answers."""
    rows = "\n".join(f"| {dependency_type} | {_ANSWER} |" for dependency_type in types)
    return f"| Dependency type | {header} |\n| --- | --- |\n{rows}\n"


class TableTest(unittest.TestCase):
    """Unit tests for the dependency types the tables list."""

    def test_tables_listing_the_declared_types_are_sound(self):
        """Test that every table is found with the types it lists, and that tables listing them all pass."""
        markdown = _dependency_type_table("Files", *_TYPE_NAMES)
        markdown += "\n" + _dependency_type_table("Yank check", *_TYPE_NAMES)
        rows = [(name, _ANSWER) for name in _TYPE_NAMES]
        self.assertEqual(_tables(markdown), {"Files": rows, "Yank check": rows})
        self.assertEqual(_table_problems(markdown), [])

    def test_table_in_a_code_block_is_not_read(self):
        """Test that a table in a fenced code block is left out, since it is sample content rather than an answer."""
        markdown = _dependency_type_table("Files", "Python dependencies")
        markdown += "\n```markdown\n" + _dependency_type_table("Yank check", "Docker images") + "```\n"
        self.assertEqual(_tables(markdown), {"Files": [("Python dependencies", _ANSWER)]})

    def test_alignment_colons_in_the_separator_are_not_a_row(self):
        """Test that a separator row is skipped however it is written, so it is not compared as a dependency type."""
        table = _dependency_type_table("Files", *_TYPE_NAMES)
        self.assertEqual(_table_problems(table.replace("| --- | --- |", "| :-------------- | :---- |")), [])

    @kills(
        Mutation(
            structure_check,
            'for problem in _list_problems(_row_labels(rows), list(_TYPE_NAMES), f"the \'{header}\' table", "row")',
            'for problem in _list_problems(_row_labels(rows), _row_labels(rows), f"the \'{header}\' table", "row")',
            "each table is compared with itself, so no table is held to the declared dependency types",
        )
    )
    def test_table_missing_a_declared_type_is_reported(self):
        """Test that a table leaving out a declared dependency type is reported, naming the table and the type."""
        markdown = _dependency_type_table("Files", *_TYPE_NAMES[:-1])
        self.assertEqual(_table_problems(markdown), [f"the 'Files' table is missing the row '{_TYPE_NAMES[-1]}'"])

    def test_unexpected_row_is_reported(self):
        """Test that a table listing a type nobody declared is reported, naming the table and the type."""
        markdown = _dependency_type_table("Yank check", *_TYPE_NAMES, "Notes")
        self.assertEqual(_table_problems(markdown), ["the 'Yank check' table has an unexpected row 'Notes'"])

    def test_rows_out_of_order_are_reported(self):
        """Test that a table listing the declared types in another order is reported, naming the row that moved."""
        swapped = (_TYPE_NAMES[1], _TYPE_NAMES[0], *_TYPE_NAMES[2:])
        expected = [f"the 'Files' table has the row '{_TYPE_NAMES[1]}' where '{_TYPE_NAMES[0]}' was expected"]
        self.assertEqual(_table_problems(_dependency_type_table("Files", *swapped)), expected)


class RowLinkTest(unittest.TestCase):
    """Unit tests for the sections the table rows link to."""

    def document(self, label: str, section: str) -> str:
        """Return a table whose one row carries the label and links to the section, followed by that section."""
        row = f"[{label}](#{section.lower().replace(' ', '-')})"
        return f"| Dependency type | Files |\n| :- | :- |\n| {row} | a file |\n\n### {section}\n"

    def test_rows_linking_to_a_section_that_names_them_pass(self):
        """Test that a row passes when its section names it, including a section two dependency types share."""
        shared = "#github-actions-and-pre-commit-hooks"
        markdown = f"| Dependency type | Files |\n| :- | :- |\n| [GitHub Actions]({shared}) | a workflow |\n"
        markdown += f"| [Pre-commit hooks]({shared}) | a config |\n\n### GitHub Actions and pre-commit hooks\n"
        self.assertEqual(_row_link_problems(markdown), [])

    def test_row_linking_to_a_section_that_does_not_name_it_is_reported(self):
        """Test that a row is reported when the section it links to doesn't name the dependency type it labels."""
        markdown = self.document("jsDelivr npm URLs", "jsDelivr")
        expected = ["the 'Files' table's row 'jsDelivr npm URLs' links to 'jsDelivr', which doesn't name it"]
        self.assertEqual(_row_link_problems(markdown), expected)


class AnchorTest(unittest.TestCase):
    """Unit tests for the internal links checked against the headings they point at."""

    def document(self, link: str) -> str:
        """Return a sound details chapter followed by a paragraph carrying the link."""
        return f"{_details_chapter()}\nSee [the details]({link}).\n"

    def test_comment_in_a_code_block_provides_no_anchor(self):
        """Test that a comment in a fenced code block is not read as a heading, so a link can't resolve against it."""
        markdown = "# Update-time\n\n```dockerfile\n# update-time: ignore\nFROM python:3.12\n```\n"
        self.assertEqual(_anchors(markdown), {"#update-time"})

    def test_link_to_a_missing_anchor_is_reported(self):
        """Test that a link to an anchor no heading provides is reported, naming the anchor."""
        expected = ["no heading provides the anchor '#no-such-heading'"]
        self.assertEqual(_problems(self.document("#no-such-heading")), expected)

    def test_link_to_a_heading_in_the_document_passes(self):
        """Test that a link to an anchor a heading does provide is not reported."""
        self.assertEqual(_problems(self.document("#python-dependencies")), [])

    def test_link_to_the_documents_title_passes(self):
        """Test that a link to the document's title is not reported, since the title provides an anchor too."""
        self.assertEqual(_problems(f"# Update-time\n\n{self.document('#update-time')}"), [])


class MainTest(unittest.TestCase):
    """Unit tests for the command line entry point."""

    def run_main(self, markdown: str) -> tuple[int, str]:
        """Return the exit code and the output of checking a document read from the path given on the command line."""
        stdout = io.StringIO()
        with (
            patch("pathlib.Path.read_text", return_value=markdown),
            patch.object(sys, "argv", ["readme_structure_check.py", "docs/README.md.in"]),
            redirect_stdout(stdout),
        ):
            return main(), stdout.getvalue()

    def test_sound_document_passes_silently(self):
        """Test that a document without problems is not reported and exits zero."""
        self.assertEqual(self.run_main(_details_chapter()), (0, ""))

    def test_problem_is_reported_with_the_path_it_was_found_in(self):
        """Test that a problem is reported with the path of the document it was found in, and exits non-zero."""
        expected = "docs/README.md.in: Python dependencies is missing the subsection 'Markers'\n"
        self.assertEqual(self.run_main(_chapter_without_markers()), (1, expected))

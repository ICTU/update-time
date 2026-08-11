"""Unit tests for the README structure check."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

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


def _details_chapter(*section_titles: str, subsections: tuple[str, ...] = _SUBSECTIONS) -> str:
    """Return a details chapter with the subsections repeated under each of the type sections."""
    markdown = ["## 📖 Details per dependency type"]
    for title in section_titles:
        markdown.append(f"### {title}")
        markdown.extend(f"#### {subsection}" for subsection in subsections)
    return "\n\n".join(markdown) + "\n"


def _chapter_without_markers() -> str:
    """Return a details chapter with one type section that leaves the Markers subsection out."""
    return _details_chapter("Python dependencies", subsections=_SUBSECTIONS[:-1])


class SectionsTest(unittest.TestCase):
    """Unit tests for the type sections found in the document."""

    def test_each_type_section_with_its_subsections(self):
        """Test that every type section is found, with its subsections in document order."""
        expected = {"Python dependencies": list(_SUBSECTIONS), "Docker images": list(_SUBSECTIONS)}
        self.assertEqual(_sections(_details_chapter("Python dependencies", "Docker images")), expected)


class ProblemsTest(unittest.TestCase):
    """Unit tests for the problems reported for a document."""

    def test_complete_document_has_no_problems(self):
        """Test that a document whose type sections all carry the expected subsections is reported as sound."""
        self.assertEqual(_problems(_details_chapter("Python dependencies", "Docker images")), [])

    def test_missing_subsection_is_reported(self):
        """Test that a type section without one of the subsections is reported, naming both."""
        expected = ["Python dependencies is missing the subsection 'Markers'"]
        self.assertEqual(_problems(_chapter_without_markers()), expected)

    def test_document_without_type_sections_is_reported(self):
        """Test that a document with nothing to check is reported, rather than passing because nothing was found."""
        documents = {
            "no details chapter at all": "## ⚡ Usage\n\nRun it.\n",
            "a details chapter without type sections": "## 📖 Details per dependency type\n\nComing soon.\n",
        }
        for description, markdown in documents.items():
            with self.subTest(document=description):
                self.assertEqual(_problems(markdown), ["found no dependency type sections to check"])

    def test_unexpected_subsection_is_reported(self):
        """Test that a type section with a subsection the other types don't have is reported, naming it."""
        markdown = _details_chapter("Python dependencies", subsections=(*_SUBSECTIONS, "Notes"))
        self.assertEqual(_problems(markdown), ["Python dependencies has an unexpected subsection 'Notes'"])

    def test_subsections_out_of_order_are_reported(self):
        """Test that a type section carrying the expected subsections in another order is reported."""
        swapped = (*_SUBSECTIONS[:3], _SUBSECTIONS[4], _SUBSECTIONS[3], *_SUBSECTIONS[5:])
        expected = ["Python dependencies has the subsection 'Cooldown' where 'Pinning' was expected"]
        self.assertEqual(_problems(_details_chapter("Python dependencies", subsections=swapped)), expected)


def _dependency_type_table(header: str, *types: str) -> str:
    """Return a table of dependency types, with the header naming what its second column answers."""
    rows = "\n".join(f"| {dependency_type} | an answer |" for dependency_type in types)
    return f"| Dependency type | {header} |\n| --- | --- |\n{rows}\n"


class TableTest(unittest.TestCase):
    """Unit tests for the dependency types the tables list."""

    def test_tables_listing_the_same_types_are_sound(self):
        """Test that every table is found with the types it lists, and that agreeing tables are reported as sound."""
        types = ("Python dependencies", "Docker images")
        markdown = _dependency_type_table("Files", *types) + "\n" + _dependency_type_table("Yank check", *types)
        self.assertEqual(_tables(markdown), {"Files": list(types), "Yank check": list(types)})
        self.assertEqual(_table_problems(markdown), [])

    def test_table_in_a_code_block_is_not_read(self):
        """Test that a table in a fenced code block is left out, since it is sample content rather than an answer."""
        markdown = _dependency_type_table("Files", "Python dependencies")
        markdown += "\n```markdown\n" + _dependency_type_table("Yank check", "Docker images") + "```\n"
        self.assertEqual(_tables(markdown), {"Files": ["Python dependencies"]})

    def test_alignment_colons_in_the_separator_are_not_a_row(self):
        """Test that a separator row is skipped however it is written, so it is not compared as a dependency type."""
        aligned = "| Dependency type | Files |\n| :-------------- | :---- |\n| Python dependencies | a file |\n"
        markdown = aligned + "\n" + _dependency_type_table("Yank check", "Python dependencies")
        self.assertEqual(_table_problems(markdown), [])

    def test_missing_row_is_reported(self):
        """Test that a table leaving out a type the first table lists is reported, naming the table and the type."""
        markdown = _dependency_type_table("Files", "Python dependencies", "Docker images")
        markdown += "\n" + _dependency_type_table("Yank check", "Python dependencies")
        self.assertEqual(_table_problems(markdown), ["the 'Yank check' table is missing the row 'Docker images'"])

    def test_unexpected_row_is_reported(self):
        """Test that a table listing a type the first table doesn't is reported, naming the table and the type."""
        markdown = _dependency_type_table("Files", "Python dependencies")
        markdown += "\n" + _dependency_type_table("Yank check", "Python dependencies", "Notes")
        self.assertEqual(_table_problems(markdown), ["the 'Yank check' table has an unexpected row 'Notes'"])

    def test_rows_out_of_order_are_reported(self):
        """Test that a table listing the same types in another order is reported, naming the row that moved."""
        types = ("Python dependencies", "Docker images")
        markdown = (
            _dependency_type_table("Files", *types) + "\n" + _dependency_type_table("Yank check", *reversed(types))
        )
        expected = ["the 'Yank check' table has the row 'Docker images' where 'Python dependencies' was expected"]
        self.assertEqual(_table_problems(markdown), expected)


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
        return f"{_details_chapter('Python dependencies')}\nSee [the details]({link}).\n"

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
        self.assertEqual(self.run_main(_details_chapter("Python dependencies")), (0, ""))

    def test_problem_is_reported_with_the_path_it_was_found_in(self):
        """Test that a problem is reported with the path of the document it was found in, and exits non-zero."""
        expected = "docs/README.md.in: Python dependencies is missing the subsection 'Markers'\n"
        self.assertEqual(self.run_main(_chapter_without_markers()), (1, expected))

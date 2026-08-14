"""Unit tests for the Markdown facts the README tools share."""

import unittest

from tools.markdown import anchor, headings, lines_without_code_blocks, without_code_blocks

_DOCUMENT = "# Title\n\n## ⚡ Usage\n\n### Workflow\n\n#### Detail\n"


class HeadingsTest(unittest.TestCase):
    """Unit tests for the headings a document has."""

    def test_level_and_title_in_document_order(self):
        """Test that each heading comes back with the level its hashes give it, in the order the document has them."""
        expected = [(1, "Title"), (2, "⚡ Usage"), (3, "Workflow"), (4, "Detail")]
        self.assertEqual(headings(_DOCUMENT), expected)

    def test_headings_outside_the_levels_asked_for_are_left_out(self):
        """Test that a heading above or below the levels asked for gets no entry."""
        self.assertEqual(headings(_DOCUMENT, 2, 3), [(2, "⚡ Usage"), (3, "Workflow")])

    def test_heading_in_a_code_block_is_left_out(self):
        """Test that a heading inside a fenced code block is sample content rather than a heading of the document."""
        self.assertEqual(headings("## Real\n\n```markdown\n## Sample\n```\n"), [(2, "Real")])


class WithoutCodeBlocksTest(unittest.TestCase):
    """Unit tests for removing the fenced code blocks from a document."""

    def test_the_block_is_blanked_and_the_rest_is_kept(self):
        """Test that the lines of a code block come back empty, and the lines around it come back unchanged."""
        markdown = "Intro\n\n```console\n$ update-time\n```\n\nOutro\n"
        self.assertEqual(without_code_blocks(markdown), "Intro\n\n\n\n\n\nOutro")


class AnchorTest(unittest.TestCase):
    """Unit tests for the anchor GitHub gives a heading."""

    def test_github_slug_rules(self):
        """Test that the slug is lower-cased, with punctuation and emoji dropped and the spaces left as hyphens."""
        anchors = {
            "⏳ Cooldown": "#-cooldown",
            "What files are updated?": "#what-files-are-updated",
            "`pyproject.toml` dependencies": "#pyprojecttoml-dependencies",
        }
        for heading, expected in anchors.items():
            with self.subTest(heading=heading):
                self.assertEqual(anchor(heading), expected)


class LinesWithoutCodeBlocksTest(unittest.TestCase):
    """Unit tests for reading a document's lines with its fenced code blocks left out."""

    def test_code_block_lines_come_back_empty_and_keep_their_number(self):
        """Test that every line keeps its own number, and that the lines of a code block come back empty."""
        markdown = "Intro\n\n```console\n$ update-time\n```\n\nOutro\n"
        expected = [(1, "Intro"), (2, ""), (3, ""), (4, ""), (5, ""), (6, ""), (7, "Outro")]
        self.assertEqual(list(lines_without_code_blocks(markdown)), expected)

"""Unit tests for recognising Markdown and reading its markup."""

import unittest

from mutation_time.mutation import kills
from update_time.formats import markdown


class MarkupTest(unittest.TestCase):
    """Unit tests for what a file's name and a content type say about whether a text is written in Markdown."""

    @kills.delete(
        markdown.is_file, ".lower()", "a file that shouts its extension is read as text, so its Markdown is shown raw"
    )
    def test_an_extension_is_read_whatever_its_case(self):
        """Test that a file shouting its extension is read as Markdown all the same."""
        self.assertTrue(markdown.is_file("CHANGELOG.MD"))

    @kills.delete(
        markdown.is_content_type,
        ".strip()",
        "a content type padded with spaces matches nothing, so its Markdown is shown raw",
        case=" text/markdown ; charset=UTF-8",
    )
    @kills.delete(
        markdown.is_content_type,
        ".lower()",
        "a content type spelled in another case matches nothing, so its Markdown is shown raw",
        case="Text/Markdown",
    )
    def test_a_content_type_is_read_past_its_case_and_spacing(self):
        """Test that a content type is read past the case it is spelled in and the spaces around its parameters."""
        for content_type in ("Text/Markdown", " text/markdown ; charset=UTF-8"):
            with self.subTest(content_type=content_type):
                self.assertTrue(markdown.is_content_type(content_type))
